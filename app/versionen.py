"""App-Versionen (Schnittstelle 0.3.9): zu alte Apps bekommen 426 app_outdated.

Die App schickt `X-Taleward-App: <Version>`. Ist in der Verwaltung eine Mindestversion gesetzt und die App älter
(oder schickt keinen Header), antwortet der Server auf /api/v1/… mit 426 – außer auf /info und /health, damit die
App den Grund und den Download-Link erfährt. Verwaltung, Einladungsseite und Worker-Protokoll sind nicht betroffen.
"""
from fastapi import Request
from fastapi.responses import JSONResponse

from app import errors
from app.einstellungen import mindestversion, version_tupel

HEADER = "X-Taleward-App"
FREI = ("/api/v1/info", "/api/v1/health")


def veraltet(app_version: str | None, minimum: str | None) -> bool:
    grenze = version_tupel(minimum)
    if grenze is None:
        return False
    eigene = version_tupel(app_version)
    if eigene is None:
        return True
    laenge = max(len(eigene), len(grenze))
    return eigene + (0,) * (laenge - len(eigene)) < grenze + (0,) * (laenge - len(grenze))


async def pruefen(request: Request, call_next):
    pfad = request.url.path
    browser = pfad.startswith("/api/v1/auth/oidc/") and pfad.endswith("/start")  # Systembrowser, ohne App-Header
    if request.method != "OPTIONS" and pfad.startswith("/api/v1/") and pfad not in FREI and not browser:
        from app.db import session_factory

        minimum = mindestversion(session_factory())
        if minimum and veraltet(request.headers.get(HEADER), minimum):
            err = errors.ApiError(426, "app_outdated", version=minimum)
            return JSONResponse(status_code=426, content={"code": "app_outdated",
                                                          "message": err.message(errors.sprache(request))})
    return await call_next(request)
