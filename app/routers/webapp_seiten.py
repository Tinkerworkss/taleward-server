"""Web-App unter /app/ direkt von diesem Server (optional, Schnittstelle 0.4.2).

Die Dateien kommen aus dem App-Release (taleward-web-<version>.zip), geholt und geprüft wie die APK
(app/aktualisierung.py). index.html und alles außer /app/assets/ ohne Zwischenspeicher, damit ein Update sofort wirkt;
/app/assets/ enthält Dateinamen mit Prüfsumme und darf ein Jahr gecacht werden.
"""
import mimetypes

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import aktualisierung
from app.db import get_db

router = APIRouter(include_in_schema=False)
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("font/woff2", ".woff2")

FEHLT = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
         '<title>Taleward</title><p style="font-family:sans-serif;padding:2rem">Die Web-App liegt auf diesem Server '
         '(noch) nicht vor. / The web app is not available on this server (yet).</p>')


@router.get("/app")
def ohne_schraegstrich():
    return RedirectResponse("/app/", status_code=308)


@router.get("/app/{pfad:path}")
def datei(pfad: str, db: Session = Depends(get_db)):
    ordner = aktualisierung.web_ordner(db)
    if ordner is None:
        return HTMLResponse(FEHLT, status_code=404)
    basis = ordner.resolve()
    ziel = (basis / pfad).resolve() if pfad else basis / "index.html"
    if ziel.is_dir():
        ziel = ziel / "index.html"
    if basis not in ziel.parents and ziel != basis / "index.html" or not ziel.is_file():
        return HTMLResponse(FEHLT.replace("(noch) nicht vor", "nicht"), status_code=404)
    cache = "public, max-age=31536000, immutable" if pfad.startswith("assets/") else "no-cache"
    return FileResponse(ziel, headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff",
                                       "Referrer-Policy": "no-referrer"})
