#!/usr/bin/env bash
# shellcheck disable=SC1091,SC1111  # os-release erst zur Laufzeit; deutsche Anführungszeichen sind Absicht
# Taleward-Box bauen: Raspberry Pi OS Lite (64 Bit) + Docker + fertiges Taleward-Server-Paket + Einrichtung beim
# ersten Start. Läuft im GitHub-Ablauf „Taleward-Box“ auf einem ARM-Rechner (ubuntu-24.04-arm), als root.
#
#   sudo bash box/bauen.sh v0.4.9 ausgabe
#
# Ergebnis in <ausgabe>/:
#   Taleward-Box-<fassung>.img.xz   Speicherkarten-Abbild (Raspberry Pi 4/5, 4–8 GB)
#   taleward-box.json               Eintrag für den Raspberry Pi Imager („eigenes Repository“)
#   HINWEISE.md                     Text für das GitHub-Release
#
# Nur für Tests: BOX_OS_URL (anderes Grundabbild), BOX_OHNE_DOCKER=1 (kein Server-Paket), BOX_OHNE_CHROOT=1
# (keine Pakete im Abbild installieren).
set -euo pipefail

FASSUNG=${1:?Fassung fehlt, z. B. v0.4.9}
AUSGABE=$(realpath -m "${2:-ausgabe}")
WURZEL=$(cd "$(dirname "$0")/.." && pwd)
OS_URL=${BOX_OS_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}
REPO=${BOX_REPO:-Tinkerworkss/taleward-server}
ZUSATZ_GB=${BOX_ZUSATZ_GB:-3}   # Platz für Docker, Server-Paket und die ersten Daten
ARBEIT=$(mktemp -d)
MNT="$ARBEIT/mnt"
NAME="Taleward-Box-$FASSUNG"

schritt() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
LOOP1=""; LOOP2=""
aufraeumen() {
  set +e
  for m in "$MNT/boot/firmware" "$MNT/dev/pts" "$MNT/dev" "$MNT/proc" "$MNT/sys" "$MNT/run" "$MNT"; do
    mountpoint -q "$m" && umount -l "$m"
  done
  [ -n "$LOOP1" ] && losetup -d "$LOOP1"
  [ -n "$LOOP2" ] && losetup -d "$LOOP2"
  rm -rf "$ARBEIT"
}
# Partition N als eigenes Loop-Gerät (über Versatz – braucht keine udev-Gerätedateien wie /dev/loop0p2)
partition() {
  local nr=$1 versatz groesse
  read -r versatz groesse < <(parted -s -m "$BILD" unit B print | awk -F: -v n="$nr" '$1==n {sub("B","",$2); sub("B","",$4); print $2, $4}')
  losetup --show -f -o "$versatz" --sizelimit "$groesse" "$BILD"
}
trap aufraeumen EXIT
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo starten."; exit 1; }
mkdir -p "$AUSGABE" "$MNT"

# ------------------------------------------------------------------ 1. Server-Paket für ARM64
if [ -z "${BOX_OHNE_DOCKER:-}" ]; then
  schritt "Server-Paket $FASSUNG bauen (ARM64)"
  [ "$(uname -m)" = "aarch64" ] || { echo "Das Server-Paket muss auf einem ARM64-Rechner gebaut werden."; exit 1; }
  docker build -t "taleward-server:$FASSUNG" "$WURZEL"
  docker save "taleward-server:$FASSUNG" | zstd -T0 -12 -q -o "$ARBEIT/taleward-server.tar.zst"
fi

# ------------------------------------------------------------------ 2. Grundabbild
schritt "Raspberry Pi OS Lite (64 Bit) laden"
curl -fL --retry 3 -o "$ARBEIT/os.img.xz" "$OS_URL"
xz -d -T0 "$ARBEIT/os.img.xz"
BILD="$ARBEIT/$NAME.img"
mv "$ARBEIT/os.img" "$BILD"

schritt "Abbild um ${ZUSATZ_GB} GB vergrößern"
truncate -s "+${ZUSATZ_GB}G" "$BILD"
parted -s "$BILD" resizepart 2 100%
LOOP2=$(partition 2)
e2fsck -pf "$LOOP2" || [ $? -le 1 ]
resize2fs "$LOOP2"
LOOP1=$(partition 1)

mount "$LOOP2" "$MNT"
mkdir -p "$MNT/boot/firmware"
mount "$LOOP1" "$MNT/boot/firmware"
# os-release nur auslesen, nicht einbinden – es setzt sonst Variablen wie NAME und VERSION neu
os_wert() { (. "$MNT/etc/os-release" && eval "printf '%s' \"\${$1:-}\""); }
CODENAME=$(os_wert VERSION_CODENAME); CODENAME=${CODENAME:-bookworm}
GRUNDSYSTEM=$(os_wert PRETTY_NAME)
echo "Grundsystem: ${GRUNDSYSTEM:-?} ($CODENAME)"

# ------------------------------------------------------------------ 3. Docker, zstd, git ins Abbild
if [ -z "${BOX_OHNE_CHROOT:-}" ]; then
  schritt "Docker installieren (im Abbild)"
  for m in dev dev/pts proc sys run; do mount --bind "/$m" "$MNT/$m"; done
  cp "$MNT/etc/resolv.conf" "$ARBEIT/resolv.conf.alt" 2>/dev/null || true
  rm -f "$MNT/etc/resolv.conf" && cp /etc/resolv.conf "$MNT/etc/resolv.conf"
  printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d" && chmod +x "$MNT/usr/sbin/policy-rc.d"  # nichts starten
  chroot "$MNT" /bin/bash -euo pipefail -c "
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q ca-certificates curl gnupg zstd git openssh-client avahi-daemon
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    echo 'deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $CODENAME stable' \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable docker.service containerd.service avahi-daemon.service
    apt-get clean
    rm -rf /var/lib/apt/lists/*
  "
  rm -f "$MNT/usr/sbin/policy-rc.d"
  rm -f "$MNT/etc/resolv.conf"
  if [ -e "$ARBEIT/resolv.conf.alt" ] || [ -L "$ARBEIT/resolv.conf.alt" ]; then cp -P "$ARBEIT/resolv.conf.alt" "$MNT/etc/resolv.conf"; fi
  for m in run sys proc dev/pts dev; do umount -l "$MNT/$m"; done
fi

# ------------------------------------------------------------------ 4. Taleward
schritt "Taleward $FASSUNG einlegen"
BOX="$MNT/opt/taleward-box"
mkdir -p "$BOX/quelle/deploy"
printf '%s\n' "$FASSUNG" > "$BOX/fassung"
install -m 0755 "$WURZEL/box/erststart.sh" "$BOX/erststart.sh"
install -m 0755 "$WURZEL/install.sh" "$BOX/quelle/install.sh"
cp "$WURZEL"/deploy/* "$BOX/quelle/deploy/"
[ -f "$ARBEIT/taleward-server.tar.zst" ] && mv "$ARBEIT/taleward-server.tar.zst" "$BOX/"
install -D -m 0644 "$WURZEL/box/taleward-erststart.service" "$MNT/etc/systemd/system/taleward-erststart.service"
mkdir -p "$MNT/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/taleward-erststart.service \
  "$MNT/etc/systemd/system/multi-user.target.wants/taleward-erststart.service"
cat > "$MNT/boot/firmware/TALEWARD.txt" <<TEXT
Taleward-Box $FASSUNG

Nach dem ersten Start richtet sich die Box selbst ein (einige Minuten). Danach im Browser öffnen:
  http://taleward.local:8000
oder http://<IP-Adresse der Box>:8000  (die IP zeigt z. B. die FRITZ!Box unter Heimnetz → Netzwerk)

Die Ersteinrichtung geht ohne Code, aber nur aus dem eigenen Netz und nur einmal.
Protokoll des ersten Starts auf der Box: /var/log/taleward-erststart.log
TEXT

sync
umount "$MNT/boot/firmware"
umount "$MNT"
losetup -d "$LOOP1" "$LOOP2"
LOOP1=""; LOOP2=""

# ------------------------------------------------------------------ 5. Packen, Prüfsummen, Imager-Eintrag
schritt "Packen"
BILD_GROESSE=$(stat -c %s "$BILD")
BILD_SHA=$(sha256sum "$BILD" | cut -d' ' -f1)
xz -T0 -6 -c "$BILD" > "$AUSGABE/$NAME.img.xz"
XZ_GROESSE=$(stat -c %s "$AUSGABE/$NAME.img.xz")
XZ_SHA=$(sha256sum "$AUSGABE/$NAME.img.xz" | cut -d' ' -f1)
(cd "$AUSGABE" && sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256")
# Trixie-basierte Abbilder nutzen cloud-init für Name, WLAN und Konto aus dem Imager, ältere firstrun.sh
INIT=systemd
[ "$CODENAME" = "bookworm" ] || INIT=cloudinit-rpi
cat > "$AUSGABE/taleward-box.json" <<JSON
{
  "os_list": [
    {
      "name": "Taleward-Box $FASSUNG",
      "description": "Taleward-Server für das Heimnetz (Raspberry Pi 4/5, 64 Bit). Nach dem Start: http://taleward.local:8000",
      "url": "https://github.com/$REPO/releases/download/box-$FASSUNG/$NAME.img.xz",
      "icon": "https://raw.githubusercontent.com/$REPO/main/app/verwaltung/static/marke/icon-192.png",
      "release_date": "$(date -u +%Y-%m-%d)",
      "extract_size": $BILD_GROESSE,
      "extract_sha256": "$BILD_SHA",
      "image_download_size": $XZ_GROESSE,
      "image_download_sha256": "$XZ_SHA",
      "devices": ["pi5-64bit", "pi4-64bit"],
      "init_format": "$INIT"
    }
  ]
}
JSON
cat > "$AUSGABE/HINWEISE.md" <<TEXT
Taleward-Server **$FASSUNG** als fertiges Speicherkarten-Abbild für den Raspberry Pi 4 oder 5 (64 Bit, ab 4 GB).
Grundlage: ${GRUNDSYSTEM:-Raspberry Pi OS Lite}.

1. **Raspberry Pi Imager** öffnen → Gerät wählen → „Betriebssystem“ → ganz unten „Eigenes Abbild“ → \`$NAME.img.xz\`
   (oder in den Imager-Einstellungen das Repository \`https://github.com/$REPO/releases/latest/download/taleward-box.json\`).
2. Bei „Einstellungen anpassen“ als Name \`taleward\` eintragen, dazu Benutzer und Passwort; WLAN nur, wenn die Box
   nicht per Kabel angeschlossen wird.
3. Karte in den Pi, Netz und Strom anschließen, einige Minuten warten.
4. Im Browser \`http://taleward.local:8000\` öffnen und den Assistenten durchgehen – ohne Einrichtungscode, aber nur
   aus dem eigenen Netz.

Prüfsumme (SHA-256): \`$XZ_SHA\`
TEXT
ls -la "$AUSGABE"
