"""Freigabe prüfen: Installiert wird nur, was mit dem Freigabe-Schlüssel unterschrieben ist.

Zu jeder Fassung (Release bzw. Tag bei GitHub) gehören zwei Anhänge, die der Ablauf „Freigabe“ erst nach Bestätigung
durch den Rechteinhaber anlegt:

- ``freigabe.txt``::

      taleward-freigabe 1
      repo Tinkerworkss/taleward-server
      tag v0.4.38
      commit <40 Hex-Zeichen>
      datei <sha256> <Dateiname>      (je Anhang des Release, kann fehlen)

- ``freigabe.txt.sig``: Unterschrift mit ``ssh-keygen -Y sign -n taleward-freigabe`` (Ed25519, Format SSHSIG).

Der öffentliche Schlüssel steht fest hier im Code (und gleichlautend in deploy/aktualisieren.sh und in der
Worker-App). Was GitHub sonst über eine Datei sagt (Digest, Prüfsummen-Dateien), zählt nicht.
"""
from __future__ import annotations

import base64
import hashlib
import re
import struct
from dataclasses import dataclass, field

OEFFENTLICHER_SCHLUESSEL = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP8G+vT3BsQPQ+D5UlN615BaEd1FCXEYcxO1KEf7u7Sd"
NAMENSRAUM = "taleward-freigabe"
DATEI = "freigabe.txt"
SIGNATUR = "freigabe.txt.sig"
MAX_TEXT = 64 * 1024
MAX_SIGNATUR = 4 * 1024

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")


class FreigabeFehler(Exception):
    pass


@dataclass(frozen=True)
class Freigabe:
    repo: str
    tag: str
    commit: str
    dateien: dict[str, str] = field(default_factory=dict)  # Dateiname → SHA-256


def _string(daten: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 4 > len(daten):
        raise FreigabeFehler("Unterschrift beschädigt")
    (n,) = struct.unpack(">I", daten[pos:pos + 4])
    pos += 4
    if n > len(daten) - pos:
        raise FreigabeFehler("Unterschrift beschädigt")
    return daten[pos:pos + n], pos + n


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _schluessel_blob(zeile: str) -> bytes:
    teile = zeile.split()
    if len(teile) < 2 or teile[0] != "ssh-ed25519":
        raise FreigabeFehler("Öffentlicher Schlüssel ungültig")
    blob = base64.b64decode(teile[1], validate=True)
    art, pos = _string(blob, 0)
    roh, pos = _string(blob, pos)
    if art != b"ssh-ed25519" or len(roh) != 32 or pos != len(blob):
        raise FreigabeFehler("Öffentlicher Schlüssel ungültig")
    return blob


def signatur_pruefen(text: bytes, signatur: bytes | str, schluessel: str | None = None) -> None:
    """Prüft eine SSHSIG-Unterschrift (ssh-keygen -Y sign) über text. Wirft FreigabeFehler, wenn sie nicht passt."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if isinstance(signatur, bytes):
        try:
            signatur = signatur.decode("ascii")
        except UnicodeDecodeError:
            raise FreigabeFehler("Unterschrift beschädigt") from None
    if len(signatur) > MAX_SIGNATUR or len(text) > MAX_TEXT:
        raise FreigabeFehler("Freigabe zu groß")
    zeilen = [z.strip() for z in signatur.strip().splitlines()]
    if len(zeilen) < 3 or zeilen[0] != "-----BEGIN SSH SIGNATURE-----" or zeilen[-1] != "-----END SSH SIGNATURE-----":
        raise FreigabeFehler("Unterschrift beschädigt")
    try:
        blob = base64.b64decode("".join(zeilen[1:-1]), validate=True)
    except ValueError:
        raise FreigabeFehler("Unterschrift beschädigt") from None
    if blob[:6] != b"SSHSIG" or blob[6:10] != struct.pack(">I", 1):
        raise FreigabeFehler("Unterschrift beschädigt")
    pos = 10
    schluessel_blob, pos = _string(blob, pos)
    namensraum, pos = _string(blob, pos)
    reserviert, pos = _string(blob, pos)
    verfahren, pos = _string(blob, pos)
    sig_blob, pos = _string(blob, pos)
    if pos != len(blob):
        raise FreigabeFehler("Unterschrift beschädigt")
    erwartet = _schluessel_blob(schluessel or OEFFENTLICHER_SCHLUESSEL)
    if schluessel_blob != erwartet:
        raise FreigabeFehler("Mit einem fremden Schlüssel unterschrieben")
    if namensraum != NAMENSRAUM.encode():
        raise FreigabeFehler("Unterschrift für einen anderen Zweck")
    if verfahren not in (b"sha512", b"sha256"):
        raise FreigabeFehler("Unterschrift beschädigt")
    art, p = _string(sig_blob, 0)
    roh, p = _string(sig_blob, p)
    if art != b"ssh-ed25519" or len(roh) != 64 or p != len(sig_blob):
        raise FreigabeFehler("Unterschrift beschädigt")
    h = hashlib.sha512(text).digest() if verfahren == b"sha512" else hashlib.sha256(text).digest()
    unterschrieben = b"SSHSIG" + _s(namensraum) + _s(reserviert) + _s(verfahren) + _s(h)
    _, pos = _string(erwartet, 0)
    oeffentlich, _ = _string(erwartet, pos)
    try:
        Ed25519PublicKey.from_public_bytes(oeffentlich).verify(roh, unterschrieben)
    except InvalidSignature:
        raise FreigabeFehler("Unterschrift ungültig") from None


def lesen(text: bytes, signatur: bytes | str, repo: str, tag: str) -> Freigabe:
    """Unterschrift prüfen, dann freigabe.txt streng lesen; Repo und Tag müssen zur erwarteten Fassung passen."""
    signatur_pruefen(text, signatur)
    try:
        zeilen = text.decode("utf-8").split("\n")
    except UnicodeDecodeError:
        raise FreigabeFehler("Freigabe beschädigt") from None
    if zeilen and zeilen[-1] == "":
        zeilen.pop()
    if not zeilen or zeilen[0] != "taleward-freigabe 1":
        raise FreigabeFehler("Freigabe in unbekanntem Format")
    werte: dict[str, str] = {}
    dateien: dict[str, str] = {}
    for zeile in zeilen[1:]:
        teile = zeile.split(" ")
        if len(teile) == 2 and teile[0] in ("repo", "tag", "commit") and teile[0] not in werte:
            werte[teile[0]] = teile[1]
        elif len(teile) == 3 and teile[0] == "datei" and _SHA.match(teile[1]) and _NAME.match(teile[2]) \
                and teile[2] not in dateien:
            dateien[teile[2]] = teile[1]
        else:
            raise FreigabeFehler("Freigabe beschädigt")
    if set(werte) != {"repo", "tag", "commit"} or not _COMMIT.match(werte["commit"]):
        raise FreigabeFehler("Freigabe unvollständig")
    if werte["repo"].lower() != repo.lower() or werte["tag"] != tag:
        raise FreigabeFehler("Freigabe gehört zu einer anderen Fassung")
    return Freigabe(werte["repo"], werte["tag"], werte["commit"], dateien)
