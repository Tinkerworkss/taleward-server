"""Downloads für App und Worker, die dieser Server selbst anbietet (siehe app/aktualisierung.py).

Öffentlich ohne Anmeldung – die Dateien sind ohnehin öffentlich (Open Source) und von Android bzw. dem Installer
über die Signatur geprüft. Angeboten wird nur die freigegebene Fassung, keine beliebigen Dateien.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import aktualisierung, errors
from app.db import get_db

router = APIRouter(prefix="/downloads", include_in_schema=False)
TYPEN = {".apk": "application/vnd.android.package-archive", ".exe": "application/vnd.microsoft.portable-executable"}


@router.get("/{art}/{version}/{name}")
def herunterladen(art: str, version: str, name: str, db: Session = Depends(get_db)):
    if art not in aktualisierung.ARTEN:
        raise errors.ApiError(404, "not_found")
    s = aktualisierung.freigegeben(db, art)
    pfad = aktualisierung.datei(art, version, name) if s and s["version"] == version and s.get("datei") == name else None
    if pfad is None:
        raise errors.ApiError(404, "not_found")
    typ = next((v for k, v in TYPEN.items() if name.endswith(k)), "application/octet-stream")
    return FileResponse(pfad, media_type=typ, filename=name, headers={"Cache-Control": "public, max-age=86400"})
