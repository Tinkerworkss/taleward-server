"""Nur eine laufende App je Benutzer. Ein zweiter Start (z. B. Doppelklick auf die Verknüpfung) holt das
Fenster der laufenden App nach vorn und beendet sich."""
from __future__ import annotations

import socket
import threading
from typing import Callable

from taleward_worker import pfade

GRUSS = b"taleward-worker:zeigen\n"


def _portdatei():
    return pfade.basis() / "instanz.port"


def andere_wecken() -> bool:
    """True, wenn schon eine App läuft (und sie das Fenster zeigt)."""
    try:
        port = int(_portdatei().read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5) as s:
            s.sendall(GRUSS)
            return s.recv(3) == b"ok\n"
    except OSError:
        return False


def lauschen(zeigen: Callable[[], None]) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    _portdatei().write_text(str(srv.getsockname()[1]))

    def schleife():
        while True:
            try:
                verb, _ = srv.accept()
            except OSError:
                return
            with verb:
                verb.settimeout(2)
                try:
                    if verb.recv(64) == GRUSS:
                        verb.sendall(b"ok\n")
                        zeigen()
                except OSError:
                    pass

    threading.Thread(target=schleife, daemon=True, name="einzeln").start()
    return srv
