#!/usr/bin/env bash
# Taleward-Server automatisch aktualisieren (Docker-Paket). Läuft stündlich über den systemd-Timer
# taleward-aktualisieren.timer, den install.sh einrichtet; aktualisiert wird nur nachts (3–5 Uhr) oder sofort, wenn
# in der Verwaltung „Jetzt aktualisieren“ gewählt wurde.
#
# Ablauf: neueste Fassung (Git-Tag v…) ermitteln → Sicherung → neue Fassung bauen und starten → Gesundheit prüfen.
# Klappt der Start nicht, geht es zurück auf die alte Fassung und die Sicherung von eben.
# Die Verwaltung schaltet das ein/aus (daten/aktualisierung/server-auto.txt) und zeigt das Ergebnis
# (daten/aktualisierung/server-status.json).
#
#   sudo /opt/taleward/aktualisieren.sh           # wie der Timer
#   sudo /opt/taleward/aktualisieren.sh --jetzt   # sofort, unabhängig von Uhrzeit und Schalter
# shellcheck disable=SC1111  # deutsche Anführungszeichen sind Absicht
set -uo pipefail

ZIEL=${TALEWARD_ZIEL:-/opt/taleward}
REPO=https://github.com/Tinkerworkss/taleward-server.git
ORDNER="$ZIEL/daten/aktualisierung"
STATUS="$ORDNER/server-status.json"
cd "$ZIEL" || exit 1
mkdir -p "$ORDNER"
BAULOG=$(mktemp /tmp/taleward-bau.XXXXXX) || exit 1

jetzt=0
[ "${1:-}" = "--jetzt" ] && jetzt=1
[ -f "$ORDNER/server-jetzt" ] && jetzt=1

if [ "$jetzt" -eq 0 ]; then
  [ "$(cat "$ORDNER/server-auto.txt" 2>/dev/null || echo an)" = "aus" ] && exit 0
  stunde=$(date +%H)
  [ "$stunde" -ge 3 ] && [ "$stunde" -lt 5 ] || exit 0
fi
rm -f "$ORDNER/server-jetzt"

melden() {  # ergebnis, von, nach, meldung
  # Erst in einer neuen Datei im Ordner von root schreiben, dann hinüberschieben: Der Ordner gehört dem Container,
  # root folgt dort keinem Verweis und ändert keine Rechte rekursiv.
  local tmp
  tmp=$(mktemp "$ZIEL/.server-status.XXXXXX") || return 0
  printf '{"zeit": "%s", "ergebnis": "%s", "von": "%s", "nach": "%s", "meldung": "%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" "${4//\"/\'}" > "$tmp"
  chmod 644 "$tmp"
  chown 1000:1000 "$tmp" 2>/dev/null || true
  if [ -d "$ORDNER" ] && [ ! -L "$ORDNER" ]; then mv -f "$tmp" "$STATUS"; else rm -f "$tmp"; fi
  echo "[taleward] $1: $2 → $3 $4"
}

gesund() {
  for _ in $(seq 1 60); do
    if docker compose exec -T server python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

version_setzen() {
  if grep -q '^TALEWARD_VERSION=' .env; then
    sed -i "s/^TALEWARD_VERSION=.*/TALEWARD_VERSION=$1/" .env
  else
    echo "TALEWARD_VERSION=$1" >> .env
  fi
}

ALT=$(sed -n 's/^TALEWARD_VERSION=//p' .env); ALT=${ALT:-main}
NEU=$(git ls-remote --tags --refs "$REPO" 'v*' 2>/dev/null | sed 's#.*refs/tags/##' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
if [ -z "$NEU" ]; then
  melden "fehler" "$ALT" "$ALT" "GitHub nicht erreichbar"
  exit 1
fi
if [ "$ALT" = "$NEU" ]; then
  if [ -t 1 ]; then echo "[taleward] schon aktuell: $NEU"; fi
  exit 0  # nichts zu tun – letztes Ergebnis bleibt für die Verwaltung stehen
fi
# Nie auf eine ältere Fassung wechseln
if [ "$ALT" != "main" ] && [ "$(printf '%s\n%s\n' "$ALT" "$NEU" | sort -V | tail -1)" != "$NEU" ]; then
  exit 0
fi

SICHERUNG=$(docker compose exec -T server chronik sicherung 2>/dev/null | sed -n 's/^Sicherung angelegt: //p' | tr -d '\r')
if [ -z "$SICHERUNG" ]; then
  melden "fehler" "$ALT" "$NEU" "Sicherung vor dem Update fehlgeschlagen – nichts geändert"
  exit 1
fi

version_setzen "$NEU"
# Server mit frischem Grundbild; ein eingebauter Worker (COMPOSE_PROFILES) ohne --pull, damit sein mehrere GB großes
# KI-Paket aus dem Zwischenspeicher kommt, solange sich engine-requirements.txt nicht ändert
# Fertige Bilder (Caddy, Ollama) bei der Gelegenheit auch auffrischen – schlägt das fehl, läuft das alte weiter
if docker compose build --pull server >"$BAULOG" 2>&1 && docker compose build >>"$BAULOG" 2>&1 \
   && { docker compose pull --ignore-buildable --quiet >>"$BAULOG" 2>&1 || true; } \
   && docker compose up -d && gesund; then
  melden "aktualisiert" "$ALT" "$NEU" ""
  # Paketdateien (dieses Skript, docker-compose.yml, Caddyfile) aus der neuen Fassung übernehmen –
  # fehlt eine in der Fassung, bleibt die vorhandene (docker-compose.override.yml wird nie angefasst)
  QUELLE="https://raw.githubusercontent.com/Tinkerworkss/taleward-server/$NEU/deploy"
  compose=docker-compose.yml
  [ "$(sed -n 's/^TALEWARD_MODUS=//p' .env)" = "heimnetz" ] && compose=docker-compose.heimnetz.yml
  for paar in aktualisieren.sh:aktualisieren.sh "$compose":docker-compose.yml Caddyfile:Caddyfile; do
    quelle=${paar%%:*}; datei=${paar##*:}
    if curl -fsL "$QUELLE/$quelle" -o "$datei.neu"; then mv "$datei.neu" "$datei"; else rm -f "$datei.neu"; fi
  done
  [ -f aktualisieren.sh ] && chmod +x aktualisieren.sh
  docker compose up -d >/dev/null 2>&1
  docker image prune -f >/dev/null 2>&1 || true
  exit 0
fi

# Zurück: alte Fassung (Image ist noch da) und Datenbank von vor dem Update
version_setzen "$ALT"
docker compose stop server >/dev/null 2>&1
docker compose run --rm server chronik wiederherstellen "$SICHERUNG" >/dev/null 2>&1
docker compose up -d >/dev/null 2>&1
if gesund; then
  melden "zurueckgenommen" "$ALT" "$NEU" "Die neue Fassung startete nicht; alte Fassung und Daten von vor dem Update sind wieder aktiv"
else
  melden "fehler" "$ALT" "$NEU" "Update und Rücknahme fehlgeschlagen – bitte „sudo docker compose logs server“ ansehen"
fi
exit 1
