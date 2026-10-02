"""Anfragekörper begrenzen – auch ohne Content-Length (Transfer-Encoding: chunked).

- `lesen()`: für Routen, die den Körper selbst lesen (Upload-Teile, Umzug, Bilder) – liest gestreamt und bricht ab,
  sobald die Grenze überschritten ist.
- `Grenze`: ASGI-Middleware mit einer Obergrenze für jede Anfrage (Multipart-Formulare werden sonst vor jeder Prüfung
  vollständig zwischengespeichert).
"""
from __future__ import annotations

import json

from fastapi import Request

from app import errors

STANDARD_GRENZE = 64 * 1024 * 1024


async def lesen(request: Request, max_bytes: int, code: str = "payload_too_large", **werte) -> bytes:
    """Rohdaten lesen, aber nie mehr als max_bytes in den Speicher holen."""
    laenge = request.headers.get("content-length")
    if laenge and laenge.isdigit() and int(laenge) > max_bytes:
        raise errors.ApiError(413, code, **werte)
    teile, n = [], 0
    async for teil in request.stream():
        n += len(teil)
        if n > max_bytes:
            raise errors.ApiError(413, code, **werte)
        teile.append(teil)
    return b"".join(teile)


class _ZuGross(Exception):
    pass


class Grenze:
    """Jede Anfrage höchstens max_bytes; darüber 413, ohne den Rest zu lesen."""

    def __init__(self, app, max_bytes: int = STANDARD_GRENZE):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        englisch = False
        for name, wert in scope.get("headers") or []:
            if name == b"accept-language":
                englisch = wert.decode("latin-1").strip().lower().startswith("en")
        for name, wert in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    if int(wert) > self.max_bytes:
                        return await self._ablehnen(send, englisch)
                except ValueError:
                    return await self._ablehnen(send, englisch, 400)
        gelesen = 0
        begonnen = False

        async def zaehlen():
            nonlocal gelesen
            nachricht = await receive()
            if nachricht["type"] == "http.request":
                gelesen += len(nachricht.get("body") or b"")
                if gelesen > self.max_bytes:
                    raise _ZuGross()
            return nachricht

        async def senden(nachricht):
            nonlocal begonnen
            if nachricht["type"] == "http.response.start":
                begonnen = True
            await send(nachricht)

        try:
            await self.app(scope, zaehlen, senden)
        except _ZuGross:
            if not begonnen:
                await self._ablehnen(send, englisch)

    @staticmethod
    async def _ablehnen(send, englisch: bool, status: int = 413):
        code = "payload_too_large" if status == 413 else "validation_error"
        fehler = errors.ApiError(status, code)
        koerper = json.dumps({"code": code, "message": fehler.message("en" if englisch else "de")}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(koerper)).encode())]})
        await send({"type": "http.response.body", "body": koerper})
