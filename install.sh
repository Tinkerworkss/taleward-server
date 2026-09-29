#!/usr/bin/env bash
# shellcheck disable=SC1091,SC1111,SC2016  # os-release zur Laufzeit; deutsche Anführungszeichen und PowerShell-$ sind Absicht
# Taleward-Server einrichten – auf einem gemieteten Server (VPS), einem PC zu Hause/im Verein
# oder unter Windows 11 mit Ubuntu (WSL).
#
#   curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/install.sh | sudo bash
#
# Zwei Arten, den Server zu erreichen:
#   1) Internet mit eigener Domain und HTTPS (VPS, oder zu Hause mit Portfreigabe + DynDNS)
#   2) Nur im Heimnetz, z. B. im WLAN des Vereinsheims: http://<IP>:8000, ohne Domain
# Installiert Docker (falls nötig), legt /opt/taleward an, richtet automatische Updates ein und zeigt am Ende den
# Link zur Ersteinrichtung. Nochmal ausführen ist unschädlich: vorhandene Daten und Einstellungen bleiben.
set -euo pipefail

ZIEL=/opt/taleward
REPO=https://github.com/Tinkerworkss/taleward-server.git
QUELLE="${TALEWARD_QUELLE:-https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main}"

rot() { printf '\033[31m%s\033[0m\n' "$*"; }
gruen() { printf '\033[32m%s\033[0m\n' "$*"; }
schritt() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
abbruch() { rot "Abgebrochen: $*"; exit 1; }

# Eingaben kommen vom Terminal, auch wenn das Skript per „curl | bash“ läuft
frage() {
  local text=$1 vorgabe=${2:-} antwort
  if [ -n "$vorgabe" ]; then text="$text [$vorgabe]"; fi
  read -r -p "$text: " antwort </dev/tty || true
  printf '%s' "${antwort:-$vorgabe}"
}
alt_wert() { [ -f "$ZIEL/.env" ] && sed -n "s/^$1=//p" "$ZIEL/.env" || true; }

[ "$(id -u)" -eq 0 ] || abbruch "bitte mit sudo starten."
[ -r /etc/os-release ] && . /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) abbruch "dieses Skript kennt nur Ubuntu und Debian (gefunden: ${ID:-unbekannt}).";;
esac
WSL=0
grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null && WSL=1

# ------------------------------------------------------------------ Windows (WSL): Vorbereitung
win() { powershell.exe -NoProfile -NonInteractive -Command "$1" 2>/dev/null | tr -d '\r'; }
if [ "$WSL" -eq 1 ]; then
  schritt "Windows mit Ubuntu erkannt"
  BUILD=$(win '[Environment]::OSVersion.Version.Build' || true)
  if [ -n "$BUILD" ] && [ "$BUILD" -lt 22621 ]; then
    abbruch "Taleward als Server unter Windows braucht Windows 11 (ab 22H2). Unter Windows 10 lässt sich das Netz von Ubuntu nicht für andere Geräte öffnen."
  fi
  NEUSTART=0
  # systemd (für Docker und die automatischen Updates)
  if ! grep -qs '^systemd=true' /etc/wsl.conf; then
    printf '[boot]\nsystemd=true\n' >> /etc/wsl.conf
    NEUSTART=1
  fi
  # „Gespiegeltes“ Netz: Ubuntu ist unter der IP des Windows-PCs erreichbar
  PROFIL=$(wslpath -u "$(win '$env:USERPROFILE')")
  WSLCONFIG="$PROFIL/.wslconfig"
  if ! grep -qsi '^networkingMode=mirrored' "$WSLCONFIG"; then
    if grep -qs '^\[wsl2\]' "$WSLCONFIG"; then
      sed -i 's/^\[wsl2\]/[wsl2]\nnetworkingMode=mirrored/' "$WSLCONFIG"
    else
      printf '\n[wsl2]\nnetworkingMode=mirrored\n' >> "$WSLCONFIG"
    fi
    NEUSTART=1
  fi
  if [ "$NEUSTART" -eq 1 ] || ! pidof systemd >/dev/null 2>&1; then
    gruen "Ubuntu ist jetzt vorbereitet und muss einmal neu starten."
    cat <<TEXT

So geht es weiter:
  1. Dieses Fenster schließen.
  2. In der Windows-PowerShell eingeben:   wsl --shutdown
  3. Ubuntu wieder öffnen und denselben Befehl noch einmal ausführen:
     curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/install.sh | sudo bash
TEXT
    exit 0
  fi
fi

schritt "Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker fehlt – wird installiert (dauert 1–2 Minuten) …"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || abbruch "„docker compose“ fehlt. Bitte Docker neu installieren."
command -v git >/dev/null 2>&1 || { apt-get update -q && apt-get install -y -q git; }
systemctl enable --now docker >/dev/null 2>&1 || true
gruen "Docker ist bereit."

# ------------------------------------------------------------------ Art der Erreichbarkeit
schritt "Wie sollen Handys den Server erreichen?"
ALT_MODUS=$(alt_wert TALEWARD_MODUS)
VORGABE=1
[ "$ALT_MODUS" = "heimnetz" ] && VORGABE=2
[ -z "$ALT_MODUS" ] && [ "$WSL" -eq 1 ] && VORGABE=2
cat <<TEXT
  1) Über das Internet mit eigener Domain und HTTPS
     – gemieteter Server (VPS), oder zu Hause/im Verein mit Portfreigabe im Router und DynDNS
  2) Nur im Heimnetz, z. B. im WLAN des Vereinsheims – ohne Domain, kein Dritter beteiligt
     (Anmelden mit Google & Co. und die Web-App im Browser gehen dann nicht)
TEXT
WAHL=$(frage "Auswahl (1 oder 2)" "$VORGABE")
case "$WAHL" in
  1) MODUS=domain ;;
  2) MODUS=heimnetz ;;
  *) abbruch "bitte 1 oder 2 wählen." ;;
esac

LAN_IP=$( { ip -4 route get 1.1.1.1 2>/dev/null || true; } | sed -n 's/.* src \([0-9.]*\).*/\1/p')
[ -n "$LAN_IP" ] || LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
DOMAIN=""; MAIL=""; ADRESSE=""
if [ "$MODUS" = "domain" ]; then
  DOMAIN=$(frage "Adresse des Servers (z. B. taleward.meinverein.de)" "$(alt_wert TALEWARD_DOMAIN)")
  DOMAIN=${DOMAIN#http://}; DOMAIN=${DOMAIN#https://}; DOMAIN=${DOMAIN%%/*}
  [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || abbruch "„$DOMAIN“ ist keine gültige Adresse."
  MAIL=$(frage "E-Mail für das HTTPS-Zertifikat (Let's Encrypt schreibt nur bei Problemen)" "$(alt_wert ACME_EMAIL)")
  [[ "$MAIL" == *@*.* ]] || abbruch "„$MAIL“ ist keine gültige E-Mail-Adresse."
  ADRESSE="https://$DOMAIN"

  # Zeigt die Domain auf diesen Anschluss? Sonst klappt das Zertifikat nicht.
  EIGENE_IP=$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || true)
  DNS_IP=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)
  if [ -n "$EIGENE_IP" ] && [ "$DNS_IP" != "$EIGENE_IP" ]; then
    rot "Achtung: $DOMAIN zeigt auf „${DNS_IP:-nichts}“, dieser Anschluss hat aber $EIGENE_IP."
    echo "Beim Domain-Anbieter einen A-Eintrag „$DOMAIN → $EIGENE_IP“ anlegen (zu Hause: DynDNS, z. B. über die FRITZ!Box)."
    echo "Bis der wirkt (oft Minuten, manchmal Stunden), bekommt der Server kein Zertifikat – er versucht es selbst weiter."
    [ "$(frage "Trotzdem fortfahren? (j/n)" "n")" = "j" ] || abbruch "erst den DNS-Eintrag anlegen, dann nochmal starten."
  fi
  if [ -n "$LAN_IP" ] && [[ "$LAN_IP" =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.) ]]; then
    echo
    echo "Dieser Server steht hinter einem Router. Dort eine Portfreigabe einrichten:"
    echo "  TCP 80 und 443  →  $LAN_IP   (FRITZ!Box: Internet → Freigaben → Portfreigaben)"
    echo "Anschlüsse mit DS-Lite oder Mobilfunk können das oft nicht – dann Auswahl 2 (Heimnetz) nehmen."
  fi
else
  [ -n "$LAN_IP" ] || abbruch "die IP-Adresse dieses Servers im Heimnetz ist unbekannt."
  ADRESSE="http://$LAN_IP:8000"
  echo "Der Server wird erreichbar unter:  $ADRESSE"
  echo "Tipp: Im Router für diesen Server immer dieselbe IP vergeben (FRITZ!Box: Heimnetz → Netzwerk → Gerät bearbeiten)."
fi

# ------------------------------------------------------------------ Worker auf diesem Server?
# Grafikkarte erkennen und auf Wunsch den eingebauten Worker einrichten (Compose-Profil „worker“ bzw. „worker-cpu“).
# NVIDIA: Treiber und nvidia-container-toolkit werden bei Bedarf installiert. AMD/Intel folgen später.
pci_hersteller() {  # Hersteller-IDs aller Grafikkarten (Klasse 0x03…): 0x10de NVIDIA, 0x1002 AMD, 0x8086 Intel
  local d
  for d in /sys/bus/pci/devices/*; do
    case "$(cat "$d/class" 2>/dev/null)" in 0x03*) cat "$d/vendor" 2>/dev/null ;; esac
  done
}
nvidia_smi() {
  if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi "$@"
  elif [ -x /usr/lib/wsl/lib/nvidia-smi ]; then /usr/lib/wsl/lib/nvidia-smi "$@"
  else return 127; fi
}
docker_hat_nvidia() { docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; }
toolkit_installieren() {
  echo "Richte die Grafikkarte für Docker ein (nvidia-container-toolkit) …"
  command -v gpg >/dev/null 2>&1 || apt-get install -y -q gnupg >/dev/null
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -q >/dev/null && apt-get install -y -q nvidia-container-toolkit >/dev/null
  nvidia-ctk runtime configure --runtime=docker >/dev/null
  systemctl restart docker
}

schritt "Worker auf diesem Server?"
ALT_PROFIL=$(alt_wert COMPOSE_PROFILES)
PROFIL=""; WORKER_CPUS=""
HERSTELLER=${TALEWARD_PCI:-$(pci_hersteller | sort -u | tr '\n' ' ')}   # TALEWARD_PCI nur für Tests
NVIDIA=0
if [[ "$HERSTELLER" == *0x10de* ]] || nvidia_smi -L >/dev/null 2>&1; then NVIDIA=1; fi
KERNE=$(nproc 2>/dev/null || echo 1)
RAM_GB=$(awk '/MemTotal/ {printf "%d", $2 / 1024 / 1024 + 0.5}' /proc/meminfo 2>/dev/null || echo 0)

if [ "$NVIDIA" -eq 1 ]; then
  KARTE=$(nvidia_smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1 || true)
  echo "NVIDIA-Grafikkarte gefunden${KARTE:+: $KARTE}."
  echo "Der Worker kann direkt hier mitlaufen und die Aufnahmen umwandeln (einmalig etwa 8 GB, dazu 5 GB Modelle)."
  VORGABE_W=j
  [ -n "$ALT_PROFIL" ] && [ "$ALT_PROFIL" != "worker" ] && VORGABE_W=n
  if [ "$(frage "Worker mit der Grafikkarte hier mitlaufen lassen? (j/n)" "$VORGABE_W")" = "j" ]; then
    if ! nvidia_smi -L >/dev/null 2>&1; then
      if [ "$WSL" -eq 1 ]; then
        abbruch "Ubuntu sieht die Grafikkarte nicht. Unter Windows den aktuellen NVIDIA-Treiber installieren, dann nochmal starten."
      fi
      command -v ubuntu-drivers >/dev/null 2>&1 || apt-get install -y -q ubuntu-drivers-common >/dev/null 2>&1 || true
      command -v ubuntu-drivers >/dev/null 2>&1 \
        || abbruch "Der NVIDIA-Treiber fehlt. Bitte über die Paketverwaltung installieren, neu starten und das Skript nochmal ausführen."
      echo "Der NVIDIA-Treiber fehlt – wird installiert (einige Minuten) …"
      ubuntu-drivers install
      gruen "Treiber installiert. Der Server muss einmal neu starten."
      echo "Danach denselben Befehl noch einmal ausführen (die Fragen kommen dann noch einmal):"
      echo "  curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/install.sh | sudo bash"
      exit 0
    fi
    docker_hat_nvidia || toolkit_installieren
    docker_hat_nvidia || abbruch "Docker sieht die Grafikkarte nicht (nvidia-container-toolkit). Siehe INSTALLATION-VPS.md, „Worker auf dem Server selbst“."
    PROFIL=worker
    gruen "Grafikkarte für Docker bereit."
  fi
else
  case "$HERSTELLER" in
    *0x1002*|*0x8086*)
      echo "Grafikkarte von AMD oder Intel gefunden – dafür gibt es den Worker noch nicht (geplant)."
      echo "Transkribieren geht über einen PC mit NVIDIA-Karte und der Worker-App oder über die Cloud." ;;
  esac
  if [ "$RAM_GB" -ge 8 ] && [ "$KERNE" -ge 4 ]; then
    WORKER_CPUS=$(( KERNE / 2 ))
    echo "Ohne passende Grafikkarte kann der Worker auch mit dem Prozessor rechnen – langsam: grob einen halben bis"
    echo "ganzen Tag für 4 Stunden Aufnahme. Er nutzt dann $WORKER_CPUS von $KERNE Kernen, der Rest bleibt für den Server."
    VORGABE_W=n
    [ "$ALT_PROFIL" = "worker-cpu" ] && VORGABE_W=j
    if [ "$(frage "Worker mit dem Prozessor hier mitlaufen lassen? (j/n)" "$VORGABE_W")" = "j" ]; then
      PROFIL=worker-cpu
    fi
  else
    echo "Keine passende Grafikkarte. Transkribiert wird auf einem PC mit der Worker-App oder über die Cloud."
  fi
fi
if [ -n "$ALT_PROFIL" ] && [ "$ALT_PROFIL" != "$PROFIL" ] && [ -f "$ZIEL/docker-compose.yml" ]; then
  (cd "$ZIEL" && COMPOSE_PROFILES="$ALT_PROFIL" docker compose rm -sf worker worker-cpu >/dev/null 2>&1 || true)
fi

# ------------------------------------------------------------------ Dateien
schritt "Dateien in $ZIEL"
mkdir -p "$ZIEL/daten"
COMPOSE=docker-compose.yml
[ "$MODUS" = "heimnetz" ] && COMPOSE=docker-compose.heimnetz.yml
if [ -f "$ZIEL/docker-compose.yml" ] && [ "$MODUS" != "${ALT_MODUS:-domain}" ]; then
  (cd "$ZIEL" && docker compose down >/dev/null 2>&1 || true)   # Art gewechselt: alte Dienste beenden
fi
curl -fsSL "$QUELLE/deploy/$COMPOSE" -o "$ZIEL/docker-compose.yml"
curl -fsSL "$QUELLE/deploy/Caddyfile" -o "$ZIEL/Caddyfile"
curl -fsSL "$QUELLE/deploy/aktualisieren.sh" -o "$ZIEL/aktualisieren.sh" && chmod +x "$ZIEL/aktualisieren.sh"
# Feste Fassung: neuestes Tag v…; die automatischen Updates gehen von dort weiter
VERSION=$(alt_wert TALEWARD_VERSION)
if [ -z "$VERSION" ]; then
  VERSION=$(git ls-remote --tags --refs "$REPO" 'v*' 2>/dev/null \
    | sed 's#.*refs/tags/##' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
  VERSION=${VERSION:-main}
fi
# Eigene Zusätze in der .env behalten (z. B. COMPOSE_PROFILES=worker für den eingebauten Worker)
EXTRA=""
if [ -f "$ZIEL/.env" ]; then
  EXTRA=$(grep -vE '^(#|$|TALEWARD_MODUS=|TALEWARD_ADRESSE=|TALEWARD_DOMAIN=|ACME_EMAIL=|TALEWARD_VERSION=|COMPOSE_PROFILES=)' "$ZIEL/.env" || true)
fi
umask 077
cat > "$ZIEL/.env" <<ENV
# Einstellungen für docker compose. Nach Änderungen: cd $ZIEL && docker compose up -d
TALEWARD_MODUS=$MODUS
TALEWARD_ADRESSE=$ADRESSE
TALEWARD_DOMAIN=$DOMAIN
ACME_EMAIL=$MAIL
# Version (Tag) des Servers, z. B. v0.4.3 – die automatischen Updates setzen sie selbst weiter
TALEWARD_VERSION=$VERSION
# Eingebauter Worker: worker (Grafikkarte), worker-cpu (Prozessor) oder leer (keiner)
COMPOSE_PROFILES=$PROFIL
ENV
if [ "$PROFIL" = "worker-cpu" ] && ! printf '%s\n' "$EXTRA" | grep -q '^TALEWARD_WORKER_CPUS='; then
  EXTRA=$(printf '%s\nTALEWARD_WORKER_CPUS=%s' "$EXTRA" "$WORKER_CPUS" | sed '/^$/d')
fi
[ -z "$EXTRA" ] || printf '%s\n' "$EXTRA" >> "$ZIEL/.env"
umask 022
chown 1000:1000 "$ZIEL/daten"   # der Server im Container läuft als Nutzer 1000, nicht als root
chmod 700 "$ZIEL/daten"

# Automatische Updates: alle 5 Minuten nachsehen, ob die Verwaltung „Jetzt aktualisieren“ will; sonst nachts
cat > /etc/systemd/system/taleward-aktualisieren.service <<UNIT
[Unit]
Description=Taleward-Server aktualisieren
After=docker.service
[Service]
Type=oneshot
ExecStart=$ZIEL/aktualisieren.sh
UNIT
cat > /etc/systemd/system/taleward-aktualisieren.timer <<UNIT
[Unit]
Description=Taleward-Server: nach Updates sehen
[Timer]
OnCalendar=*:0/5
Persistent=false
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now taleward-aktualisieren.timer >/dev/null 2>&1 || true

PORTS="80 443"
[ "$MODUS" = "heimnetz" ] && PORTS="8000"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  for p in $PORTS; do ufw allow "$p/tcp" >/dev/null; done
  echo "Firewall: Port(s) $PORTS freigegeben."
fi

# ------------------------------------------------------------------ Windows: Zugang aus dem Netz, Dauerbetrieb
if [ "$WSL" -eq 1 ]; then
  schritt "Windows einrichten"
  # Eingehende Verbindungen zu Ubuntu erlauben (Hyper-V-Firewall) – braucht einmal eine Bestätigung als Administrator
  TEMP_WIN=$(win '$env:TEMP')
  SKRIPT="$(wslpath -u "$TEMP_WIN")/taleward-firewall.ps1"
  {
    for p in $PORTS; do
      echo "New-NetFirewallHyperVRule -Name 'Taleward-$p' -DisplayName 'Taleward $p' -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts $p -ErrorAction SilentlyContinue"
    done
  } > "$SKRIPT"
  echo "Windows fragt gleich, ob Änderungen erlaubt werden sollen – bitte mit „Ja“ bestätigen (Firewall für Taleward)."
  if ! win "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','$TEMP_WIN\\taleward-firewall.ps1'" >/dev/null; then
    rot "Die Firewall-Regel ließ sich nicht setzen. Bitte in einer PowerShell „Als Administrator“ ausführen:"
    cat "$SKRIPT"
  fi
  # Mit Windows starten und laufen lassen, auch ohne offenes Ubuntu-Fenster
  STARTORDNER=$(wslpath -u "$(win '[Environment]::GetFolderPath("Startup")')")
  DISTRO=${WSL_DISTRO_NAME:-$(wsl.exe -l -q --running 2>/dev/null | iconv -f utf-16le -t utf-8 2>/dev/null | tr -d '\r' | head -1)}
  printf 'CreateObject("Wscript.Shell").Run "wsl.exe -d %s --exec sleep infinity", 0, False\r\n' "${DISTRO:-Ubuntu}" \
    > "$STARTORDNER/Taleward-Server.vbs"
  win "Start-Process wscript.exe -ArgumentList '\"$(wslpath -w "$STARTORDNER/Taleward-Server.vbs")\"'" >/dev/null || true
  gruen "Taleward startet jetzt mit Windows und läuft im Hintergrund weiter."
  if [ "$(frage "Soll der PC am Netzstrom nicht mehr in den Ruhezustand gehen? (j/n)" "j")" = "j" ]; then
    win "powercfg /change standby-timeout-ac 0" >/dev/null || true
    echo "Ruhezustand am Netzstrom ausgeschaltet (Bildschirm darf weiter ausgehen)."
  else
    echo "Hinweis: Im Ruhezustand ist der Server nicht erreichbar."
  fi
fi

# ------------------------------------------------------------------ Belegte Ports freimachen
# Manche VPS-Vorlagen (z. B. Hostinger „Ubuntu mit Docker“) bringen schon einen Webserver wie Traefik mit.
# Hält er Port 80/443, landet jeder Aufruf dort (404) und Caddy bekommt kein Zertifikat.
port_frei() {
  local p=$1 zeile pid name behaelter einheit
  zeile=$(ss -Htlnp "sport = :$p" 2>/dev/null | head -1 || true)
  [ -n "$zeile" ] || return 0
  pid=$(printf '%s' "$zeile" | sed -n 's/.*pid=\([0-9]*\).*/\1/p')
  name=$(printf '%s' "$zeile" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p')
  behaelter=""
  if [ "$name" = "docker-proxy" ]; then
    behaelter=$(docker ps --filter "publish=$p" --format '{{.Names}}' 2>/dev/null | head -1 || true)
  elif [ -n "$pid" ]; then
    local id
    id=$(grep -o 'docker-[0-9a-f]\{64\}' "/proc/$pid/cgroup" 2>/dev/null | head -1 | sed 's/docker-//' || true)
    [ -z "$id" ] || behaelter=$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##' || true)
  fi
  case "$behaelter" in taleward-*) return 0 ;; esac   # unser eigener Caddy/Server (erneuter Lauf)
  echo
  if [ -n "$behaelter" ]; then
    rot "Port $p ist belegt durch den Docker-Container „$behaelter“ (${name:-?})."
    if [ "$(frage "Diesen Container dauerhaft stoppen, damit Taleward den Port bekommt? (j/n)" "j")" = "j" ]; then
      docker update --restart=no "$behaelter" >/dev/null 2>&1 || true
      docker stop "$behaelter" >/dev/null && gruen "„$behaelter“ gestoppt." && return 0
    fi
  else
    einheit=$(ps -o unit= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    rot "Port $p ist belegt durch „${name:-unbekannt}“${einheit:+ (Dienst $einheit)}."
    if [ -n "$einheit" ] && [ "${einheit%.service}" != "$einheit" ] &&
       [ "$(frage "Diesen Dienst dauerhaft abschalten, damit Taleward den Port bekommt? (j/n)" "j")" = "j" ]; then
      systemctl disable --now "$einheit" >/dev/null 2>&1 && gruen "$einheit abgeschaltet." && return 0
    fi
  fi
  abbruch "Port $p wird gebraucht. Bitte „${behaelter:-$name}“ beenden und das Skript nochmal starten."
}

# ------------------------------------------------------------------ Starten
if [ -n "$PROFIL" ]; then
  schritt "Server und Worker bauen und starten (beim ersten Mal 15–25 Minuten, der Worker ist groß)"
else
  schritt "Server bauen und starten (beim ersten Mal 3–5 Minuten)"
fi
cd "$ZIEL"
for p in $PORTS; do port_frei "$p"; done
docker compose build --pull
docker compose up -d

echo -n "Warte auf den Server "
for _ in $(seq 1 60); do
  if docker compose exec -T server python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)" >/dev/null 2>&1; then
    echo; gruen "Server läuft."
    break
  fi
  echo -n "."; sleep 2
done

# Namensauflösung in den Containern: Auf manchen Servern reicht Docker den lokalen DNS-Dienst (127.0.0.53)
# durch, der im Container ins Leere zeigt – dann gehen Zertifikat, Updates und E-Mails nicht.
# Abhilfe: Quad9 (Schweiz, speichert keine IP-Adressen) in einer Zusatzdatei, die Updates nicht überschreiben.
if ! docker compose exec -T server python -c "import socket; socket.getaddrinfo('github.com', 443)" >/dev/null 2>&1; then
  echo "Namensauflösung im Container klappt nicht – stelle auf Quad9 um."
  {
    echo "# Von install.sh angelegt: DNS für die Container (bleibt bei Updates erhalten)"
    echo "services:"
    echo "  server:"
    echo "    dns: [9.9.9.9, 149.112.112.112]"
    if [ "$MODUS" = "domain" ]; then
      echo "  caddy:"
      echo "    dns: [9.9.9.9, 149.112.112.112]"
    fi
  } > docker-compose.override.yml
  docker compose up -d --force-recreate >/dev/null
  sleep 5
fi

# Von außen erreichbar? (Domain: HTTPS mit gültigem Zertifikat über Caddy)
if [ "$MODUS" = "domain" ]; then
  echo -n "Warte auf das HTTPS-Zertifikat "
  OK=0
  for _ in $(seq 1 45); do
    if curl -fsS --max-time 5 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/api/v1/health" >/dev/null 2>&1; then
      OK=1; break
    fi
    echo -n "."; sleep 4
  done
  echo
  if [ "$OK" -eq 1 ]; then
    gruen "https://$DOMAIN ist erreichbar."
  else
    rot "https://$DOMAIN antwortet noch nicht mit gültigem Zertifikat."
    echo "Meist wirkt der DNS-Eintrag noch nicht oder Port 80/443 ist in der Firewall des Anbieters zu."
    echo "Caddy versucht es selbst weiter. Nachsehen:  cd $ZIEL && sudo docker compose logs --tail=30 caddy"
  fi
fi

if [ -n "$PROFIL" ]; then
  sleep 5
  if docker compose ps --status running --services 2>/dev/null | grep -qx "$PROFIL"; then
    gruen "Worker läuft ($PROFIL). Beim ersten Auftrag lädt er die Sprachmodelle (einmalig etwa 5 GB)."
  else
    rot "Der Worker ist nicht gestartet. Nachsehen:  cd $ZIEL && sudo docker compose logs --tail=40 $PROFIL"
  fi
fi

schritt "Fertig"
docker compose exec -T server chronik einrichtungscode || true
if [ "$MODUS" = "domain" ]; then
  HINWEIS="Falls die Seite noch nicht lädt: siehe Hinweise oben; das Zertifikat holt Caddy selbst, sobald die Domain passt."
else
  HINWEIS="In der App als Server eintragen: $ADRESSE (Handy im selben WLAN)."
fi
cat <<TEXT

Öffne den Link oben im Browser und lege dort das Konto für die Verwaltung an.
$HINWEIS

Nützliche Befehle (in $ZIEL):
  Protokoll ansehen:   sudo docker compose logs -f server
  Update:              automatisch nachts (Verwaltung → Updates), sofort: sudo ./aktualisieren.sh --jetzt
  Neu starten:         sudo docker compose restart
  Daten (sichern!):    $ZIEL/daten
TEXT
if [ -n "$PROFIL" ]; then
  echo "Der Worker auf diesem Server ist schon verbunden. Weitere PCs mit der Worker-App verbindest du in der"
  echo "Verwaltung unter „Transkription“."
else
  echo "Einen Worker (PC mit Grafikkarte) verbindest du in der Verwaltung unter „Transkription“ – das darf auch"
  echo "dieser PC selbst sein."
fi
