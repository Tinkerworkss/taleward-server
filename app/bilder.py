"""Titel- und Charakterbilder.

Jedes Bild wird neu kodiert: Das entfernt Metadaten (GPS, Kameradaten, Vorschaubilder) und alles, was kein Bild ist.
Die EXIF-Drehung wird vorher angewendet, damit das Bild so aussieht, wie es die App angezeigt hat.
Bilder ohne Transparenz werden JPEG, Bilder mit Transparenz WebP (damit freigestellte Porträts frei bleiben).

Ablage unter data/bilder/ – nicht in der Datenbank, aber in der Sicherung der Verwaltung nicht enthalten
(sie sichert nur die Datenbank); verlorene Bilder lassen sich neu hochladen.
"""
from __future__ import annotations

import io
import shutil
from dataclasses import dataclass
from pathlib import Path

from app import errors, storage
from app.config import get_settings

ERLAUBT = {"image/jpeg", "image/png", "image/webp"}
MAX_PIXEL = 40_000_000  # Schutz gegen „Dekompressionsbomben“
COVER_MAX_BYTES, COVER_KANTE = 5 * 1024 * 1024, 1600
PORTRAIT_MAX_BYTES, PORTRAIT_KANTE, THUMB = 3 * 1024 * 1024, 1024, 256
MEDIENTYP = {"jpg": "image/jpeg", "webp": "image/webp"}


def wurzel() -> Path:
    return get_settings().data_dir / "bilder"


def cover_ordner(campaign_id: str) -> Path:
    return wurzel() / "kampagnen" / campaign_id


def portrait_ordner(member_id: str) -> Path:
    return wurzel() / "mitglieder" / member_id


def finden(ordner: Path, name: str) -> Path | None:
    for endung in ("jpg", "webp"):
        p = ordner / f"{name}.{endung}"
        if p.exists():
            return p
    return None


def loeschen(ordner: Path) -> None:
    shutil.rmtree(ordner, ignore_errors=True)


@dataclass
class Geladen:
    bild: object  # PIL.Image.Image
    alpha: bool


def laden(daten: bytes, content_type: str | None, max_bytes: int) -> Geladen:
    from PIL import Image, ImageOps

    typ = (content_type or "").split(";")[0].strip().lower()
    if typ not in ERLAUBT:
        raise errors.bad_request("unsupported_image")
    if len(daten) > max_bytes:
        raise errors.ApiError(413, "image_too_large", mb=max_bytes // (1024 * 1024))
    Image.MAX_IMAGE_PIXELS = MAX_PIXEL
    try:
        bild = Image.open(io.BytesIO(daten))
        if bild.format not in ("JPEG", "PNG", "WEBP"):
            raise errors.bad_request("unsupported_image")
        bild.load()
        bild = ImageOps.exif_transpose(bild)
    except errors.ApiError:
        raise
    except Exception:  # kaputt, kein Bild, zu viele Pixel …
        raise errors.bad_request("unsupported_image") from None
    alpha = bild.mode in ("RGBA", "LA", "PA") or (bild.mode == "P" and "transparency" in bild.info)
    bild = bild.convert("RGBA" if alpha else "RGB")
    if alpha and bild.getchannel("A").getextrema() == (255, 255):
        bild, alpha = bild.convert("RGB"), False  # Alphakanal ohne echte Transparenz
    return Geladen(bild, alpha)


def _kodieren(bild, alpha: bool) -> tuple[bytes, str]:
    puffer = io.BytesIO()
    if alpha:
        bild.save(puffer, "WEBP", quality=85, method=4)
        return puffer.getvalue(), "webp"
    bild.save(puffer, "JPEG", quality=85, optimize=True, progressive=True)
    return puffer.getvalue(), "jpg"


def _verkleinern(bild, kante: int):
    from PIL import Image

    if max(bild.size) <= kante:
        return bild
    b = bild.copy()
    b.thumbnail((kante, kante), Image.Resampling.LANCZOS)
    return b


def _speichern(ordner: Path, name: str, bild, alpha: bool) -> None:
    daten, endung = _kodieren(bild, alpha)
    for alt in ("jpg", "webp"):
        if alt != endung:
            (ordner / f"{name}.{alt}").unlink(missing_ok=True)
    storage.write_atomic(ordner / f"{name}.{endung}", daten)


def cover_speichern(campaign_id: str, daten: bytes, content_type: str | None) -> None:
    g = laden(daten, content_type, COVER_MAX_BYTES)
    _speichern(cover_ordner(campaign_id), "cover", _verkleinern(g.bild, COVER_KANTE), g.alpha)


def portrait_speichern(member_id: str, daten: bytes, content_type: str | None,
                       x: int | None, y: int | None, groesse: int | None) -> None:
    """full = ganzes Bild (≤ 1024 px), thumb = 256×256 aus dem Ausschnitt (in Pixeln des hochgeladenen Bildes)."""
    from PIL import Image

    g = laden(daten, content_type, PORTRAIT_MAX_BYTES)
    b = g.bild
    breite, hoehe = b.size
    if groesse is None:
        if x is not None or y is not None:
            raise errors.bad_request("invalid_crop")
        groesse = min(breite, hoehe)
        x, y = (breite - groesse) // 2, (hoehe - groesse) // 2
    else:
        x, y = x or 0, y or 0
    if groesse < 1 or x < 0 or y < 0 or x + groesse > breite or y + groesse > hoehe:
        raise errors.bad_request("invalid_crop")
    ordner = portrait_ordner(member_id)
    thumb = b.crop((x, y, x + groesse, y + groesse)).resize((THUMB, THUMB), Image.Resampling.LANCZOS)
    _speichern(ordner, "full", _verkleinern(b, PORTRAIT_KANTE), g.alpha)
    _speichern(ordner, "thumb", thumb, g.alpha)
