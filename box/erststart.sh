#!/usr/bin/env bash
# Taleward-Box: richtet beim ersten Start einmalig alles ein (läuft als systemd-Dienst taleward-erststart).
# Schlägt etwas fehl (z. B. noch kein Netz), versucht es der Dienst beim nächsten Start erneut.
set -euo pipefail
BOX=/opt/taleward-box
ZIEL=/opt/taleward
exec >>/var/log/taleward-erststart.log 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M') – erster Start der Taleward-Box"
FASSUNG=$(cat "$BOX/fassung")

# Name im Netz: taleward.local – nur wenn beim Aufspielen kein eigener Name gewählt wurde
if [ "$(hostname)" = "raspberrypi" ]; then
  hostnamectl set-hostname taleward
  sed -i 's/\braspberrypi\b/taleward/g' /etc/hosts
  systemctl restart avahi-daemon 2>/dev/null || true
fi

# Auf eine Adresse im Heimnetz warten (bis zu 3 Minuten) – Internet ist nicht nötig
for _ in $(seq 1 90); do
  [ -n "$(hostname -I 2>/dev/null | tr -d ' ')" ] && break
  sleep 2
done
[ -n "$(hostname -I 2>/dev/null | tr -d ' ')" ] || { echo "Noch keine Netzwerkadresse – nächster Versuch beim nächsten Start."; exit 1; }

systemctl start docker
if [ -f "$BOX/taleward-server.tar.zst" ]; then
  echo "Server-Paket $FASSUNG laden …"
  zstd -dc "$BOX/taleward-server.tar.zst" | docker load
fi

mkdir -p "$ZIEL"
if [ ! -f "$ZIEL/.env" ]; then
  printf 'TALEWARD_MODUS=heimnetz\nTALEWARD_VERSION=%s\nTALEWARD_EINRICHTUNG_IM_HEIMNETZ=true\n' "$FASSUNG" > "$ZIEL/.env"
fi
TALEWARD_OHNE_FRAGEN=1 TALEWARD_OHNE_BAU=1 TALEWARD_QUELLE="file://$BOX/quelle" bash "$BOX/quelle/install.sh"

rm -f "$BOX/taleward-server.tar.zst"   # liegt jetzt in Docker – Platz sparen
touch "$BOX/eingerichtet"
systemctl disable taleward-erststart.service 2>/dev/null || true
echo "Fertig. Im Browser: http://$(hostname).local:8000/verwaltung  (oder http://$(hostname -I | awk '{print $1}'):8000/verwaltung)"
