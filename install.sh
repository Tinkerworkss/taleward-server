#!/usr/bin/env bash
# shellcheck disable=SC1091,SC1111  # os-release zur Laufzeit; deutsche Anführungszeichen sind Absicht
# Taleward-Server auf einem eigenen Server (VPS mit Ubuntu oder Debian) einrichten.
#
#   curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/install.sh | sudo bash
#
# Fragt nach Domain und E-Mail, installiert Docker (falls nötig), legt /opt/taleward an,
# startet Server + HTTPS (Caddy) und zeigt am Ende den Link zur Ersteinrichtung.
# Nochmal ausführen ist unschädlich: vorhandene Daten und Einstellungen bleiben.
set -euo pipefail

ZIEL=/opt/taleward
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

[ "$(id -u)" -eq 0 ] || abbruch "bitte mit sudo starten."
[ -r /etc/os-release ] && . /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) abbruch "dieses Skript kennt nur Ubuntu und Debian (gefunden: ${ID:-unbekannt}).";;
esac

schritt "Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker fehlt – wird installiert (dauert 1–2 Minuten) …"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || abbruch "„docker compose“ fehlt. Bitte Docker neu installieren."
command -v git >/dev/null 2>&1 || { apt-get update -q && apt-get install -y -q git; }
gruen "Docker ist bereit."

schritt "Angaben"
ALT_DOMAIN=""; ALT_MAIL=""
if [ -f "$ZIEL/.env" ]; then
  ALT_DOMAIN=$(sed -n 's/^TALEWARD_DOMAIN=//p' "$ZIEL/.env")
  ALT_MAIL=$(sed -n 's/^ACME_EMAIL=//p' "$ZIEL/.env")
fi
DOMAIN=$(frage "Adresse des Servers (z. B. taleward.meinverein.de)" "$ALT_DOMAIN")
DOMAIN=${DOMAIN#http://}; DOMAIN=${DOMAIN#https://}; DOMAIN=${DOMAIN%%/*}
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || abbruch "„$DOMAIN“ ist keine gültige Adresse."
MAIL=$(frage "E-Mail für das HTTPS-Zertifikat (Let's Encrypt schreibt nur bei Problemen)" "$ALT_MAIL")
[[ "$MAIL" == *@*.* ]] || abbruch "„$MAIL“ ist keine gültige E-Mail-Adresse."

# Zeigt die Domain schon auf diesen Server? Sonst klappt das Zertifikat nicht.
EIGENE_IP=$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || true)
DNS_IP=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)
if [ -n "$EIGENE_IP" ] && [ "$DNS_IP" != "$EIGENE_IP" ]; then
  rot "Achtung: $DOMAIN zeigt auf „${DNS_IP:-nichts}“, dieser Server hat aber $EIGENE_IP."
  echo "Lege beim Domain-Anbieter einen A-Eintrag „$DOMAIN → $EIGENE_IP“ an (bei Hostinger: Domains → DNS-Einträge)."
  echo "Bis der wirkt (oft Minuten, manchmal Stunden), bekommt Caddy kein Zertifikat – es versucht es aber selbst weiter."
  [ "$(frage "Trotzdem fortfahren? (j/n)" "n")" = "j" ] || abbruch "erst den DNS-Eintrag anlegen, dann nochmal starten."
fi

schritt "Dateien in $ZIEL"
mkdir -p "$ZIEL/daten"
curl -fsSL "$QUELLE/deploy/docker-compose.yml" -o "$ZIEL/docker-compose.yml"
curl -fsSL "$QUELLE/deploy/Caddyfile" -o "$ZIEL/Caddyfile"
curl -fsSL "$QUELLE/deploy/aktualisieren.sh" -o "$ZIEL/aktualisieren.sh" && chmod +x "$ZIEL/aktualisieren.sh"
# Feste Fassung: neuestes Tag v…; die automatischen Updates gehen von dort weiter
VERSION=$(git ls-remote --tags --refs https://github.com/Tinkerworkss/taleward-server.git 'v*' 2>/dev/null \
  | sed 's#.*refs/tags/##' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
VERSION=${VERSION:-main}
if [ -f "$ZIEL/.env" ]; then ALT=$(sed -n 's/^TALEWARD_VERSION=//p' "$ZIEL/.env"); VERSION=${ALT:-$VERSION}; fi
umask 077
cat > "$ZIEL/.env" <<ENV
# Einstellungen für docker compose. Nach Änderungen: cd $ZIEL && docker compose up -d
TALEWARD_DOMAIN=$DOMAIN
ACME_EMAIL=$MAIL
# Branch oder Version (Tag) des Servers, z. B. v0.4.0
TALEWARD_VERSION=$VERSION
ENV
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

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
  echo "Firewall: Ports 80 und 443 freigegeben."
fi

schritt "Server bauen und starten (beim ersten Mal 3–5 Minuten)"
cd "$ZIEL"
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

schritt "Fertig"
docker compose exec -T server chronik einrichtungscode || true
cat <<TEXT

Öffne den Link oben im Browser und lege dort das Konto für die Verwaltung an.
Falls die Seite noch nicht lädt: Das HTTPS-Zertifikat kann ein, zwei Minuten brauchen.

Nützliche Befehle (in $ZIEL):
  Protokoll ansehen:   sudo docker compose logs -f server
  Update:              automatisch nachts (Verwaltung → Updates), sofort: sudo ./aktualisieren.sh --jetzt
  Neu starten:         sudo docker compose restart
  Daten (sichern!):    $ZIEL/daten

Einen Worker (PC mit Grafikkarte) verbindest du in der Verwaltung unter „Worker“.
TEXT
