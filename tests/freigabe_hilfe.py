"""Test-Schlüssel und Freigaben wie vom Ablauf „Freigabe“ (ssh-keygen -Y sign), nur mit einem Schlüssel für Tests."""
import base64
import hashlib
import struct

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


SCHLUESSEL = Ed25519PrivateKey.generate()
_ROH = SCHLUESSEL.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
_BLOB = _s(b"ssh-ed25519") + _s(_ROH)
OEFFENTLICH = "ssh-ed25519 " + base64.b64encode(_BLOB).decode()


def unterschreiben(text: bytes, namensraum: bytes = b"taleward-freigabe", schluessel=None) -> str:
    schluessel = schluessel or SCHLUESSEL
    roh = schluessel.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    blob_schluessel = _s(b"ssh-ed25519") + _s(roh)
    daten = b"SSHSIG" + _s(namensraum) + _s(b"") + _s(b"sha512") + _s(hashlib.sha512(text).digest())
    sig = schluessel.sign(daten)
    blob = (b"SSHSIG" + struct.pack(">I", 1) + _s(blob_schluessel) + _s(namensraum) + _s(b"") + _s(b"sha512")
            + _s(_s(b"ssh-ed25519") + _s(sig)))
    b64 = base64.b64encode(blob).decode()
    return "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(b64[i:i + 70] for i in range(0, len(b64), 70)) + \
        "\n-----END SSH SIGNATURE-----\n"


def freigabe(repo: str, tag: str, dateien: dict[str, bytes] | None = None, commit: str = "a" * 40) -> tuple[bytes, str]:
    zeilen = ["taleward-freigabe 1", f"repo {repo}", f"tag {tag}", f"commit {commit}"]
    zeilen += [f"datei {hashlib.sha256(inhalt).hexdigest()} {name}" for name, inhalt in (dateien or {}).items()]
    text = ("\n".join(zeilen) + "\n").encode()
    return text, unterschreiben(text)
