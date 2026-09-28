#!/usr/bin/env bash
# Taleward Worker unter Linux installieren (für den eigenen Benutzer, ohne root):
#
#   curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/worker-app/install-linux.sh | bash
#
# Installiert uv (falls nötig) und die App mit eigenem Fenster (Qt), legt einen Eintrag im Anwendungsmenü an und
# fragt, ob der Worker mit dem Computer starten soll. Nochmal ausführen = aktualisieren.
# shellcheck disable=SC1111  # deutsche Anführungszeichen sind Absicht
set -euo pipefail

REPO=Tinkerworkss/taleward-server
REF="${TALEWARD_WORKER_REF:-main}"
BIN="$HOME/.local/bin"
DATEN="${XDG_DATA_HOME:-$HOME/.local/share}"
QUELLE="https://raw.githubusercontent.com/$REPO/$REF/worker-app"

gruen() { printf '\033[32m%s\033[0m\n' "$*"; }
schritt() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
frage() { local a; read -r -p "$1 " a </dev/tty || true; printf '%s' "$a"; }

[ "$(id -u)" -ne 0 ] || { echo "Bitte ohne sudo starten – der Worker läuft als dein Benutzer."; exit 1; }
[ "$(uname -m)" = "x86_64" ] || { echo "Nur für 64-Bit-PCs (x86_64)."; exit 1; }

schritt "Grafikkarte"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || true
else
  echo "Kein NVIDIA-Treiber gefunden (nvidia-smi fehlt). Ohne ihn läuft nur der Testmodus."
  echo "Ubuntu: sudo ubuntu-drivers install  – danach neu starten."
fi

schritt "Systempakete für das Fenster"
fehlt=()
for lib in libxcb-cursor.so.0 libEGL.so.1 libxkbcommon-x11.so.0; do
  ldconfig -p 2>/dev/null | grep -q "$lib" || fehlt+=("$lib")
done
if [ ${#fehlt[@]} -gt 0 ]; then
  echo "Es fehlen: ${fehlt[*]}"
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installiere mit sudo (Passwort nötig): libxcb-cursor0 libegl1 libxkbcommon-x11-0"
    sudo apt-get install -y libxcb-cursor0 libegl1 libxkbcommon-x11-0
  else
    echo "Bitte über die Paketverwaltung nachinstallieren (Paketnamen je nach System: xcb-util-cursor, libglvnd, libxkbcommon-x11)."
  fi
else
  echo "Alles da."
fi

schritt "uv"
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$BIN/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV=$(command -v uv || echo "$BIN/uv")
"$UV" --version

schritt "Taleward Worker"
"$UV" tool install --force --python 3.12 \
  "taleward-worker[qt] @ https://github.com/$REPO/archive/refs/heads/$REF.zip#subdirectory=worker-app"
PROGRAMM="$BIN/taleward-worker"

mkdir -p "$DATEN/applications" "$DATEN/icons/hicolor/256x256/apps"
curl -fsSL "$QUELLE/packaging/linux/taleward-worker.png" -o "$DATEN/icons/hicolor/256x256/apps/taleward-worker.png"
cat > "$DATEN/applications/taleward-worker.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Taleward Worker
Comment=Verarbeitet Aufnahmen für deinen Taleward-Server
Exec=$PROGRAMM
Icon=taleward-worker
Categories=AudioVideo;Utility;
Terminal=false
StartupWMClass=taleward-worker
DESKTOP
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATEN/applications" || true

schritt "Autostart"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/taleward-worker.desktop"
antwort=$(frage "Soll der Worker mit dem Computer starten (unsichtbar im Hintergrund)? [J/n]")
if [ "${antwort,,}" != "n" ]; then
  mkdir -p "$(dirname "$AUTOSTART")"
  sed -e "s|^Exec=.*|Exec=$PROGRAMM --hintergrund|" "$DATEN/applications/taleward-worker.desktop" > "$AUTOSTART"
  echo "Autostart eingerichtet (in der App unter Einstellungen jederzeit änderbar)."
else
  rm -f "$AUTOSTART"
fi

gruen "Fertig. Taleward Worker startet jetzt – später findest du ihn im Anwendungsmenü."
nohup "$PROGRAMM" >/dev/null 2>&1 &
