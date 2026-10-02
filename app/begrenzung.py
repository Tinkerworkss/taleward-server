"""Gemeinsame Begrenzung von Versuchen (Anmelden, Einladungscodes, Kopplung, Anmeldedienste).

Je Schlüssel (z. B. „login-name:anna“) zählt ein gleitendes Fenster. Abgelaufene Schlüssel fallen weg, und die
Gesamtzahl der Schlüssel ist gedeckelt – der Speicher wächst nicht mit jedem Versuch eines Angreifers.
Gezählt wird im Arbeitsspeicher eines Prozesses; nach einem Neustart beginnt es von vorn (bewusst einfach).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

MAX_SCHLUESSEL = 20_000


class Zaehler:
    def __init__(self) -> None:
        self._daten: OrderedDict[str, deque] = OrderedDict()
        self._sperre = threading.Lock()

    def _bereinigt(self, schluessel: str, fenster: float, jetzt: float) -> deque:
        q = self._daten.get(schluessel)
        if q is None:
            q = deque()
            self._daten[schluessel] = q
            while len(self._daten) > MAX_SCHLUESSEL:
                self._daten.popitem(last=False)  # ältester Schlüssel fällt weg
        else:
            self._daten.move_to_end(schluessel)
        while q and q[0] < jetzt - fenster:
            q.popleft()
        return q

    def voll(self, schluessel: str, anzahl: int, fenster: float) -> bool:
        """Sind im Fenster schon anzahl Versuche gezählt? (zählt selbst nicht)"""
        with self._sperre:
            return len(self._bereinigt(schluessel, fenster, time.monotonic())) >= anzahl

    def zaehlen(self, schluessel: str, fenster: float) -> None:
        with self._sperre:
            jetzt = time.monotonic()
            self._bereinigt(schluessel, fenster, jetzt).append(jetzt)

    def versuch(self, schluessel: str, anzahl: int, fenster: float) -> bool:
        """Zählt einen Versuch. False, wenn das Fenster schon voll ist (dann wird nicht gezählt)."""
        with self._sperre:
            jetzt = time.monotonic()
            q = self._bereinigt(schluessel, fenster, jetzt)
            if len(q) >= anzahl:
                return False
            q.append(jetzt)
            return True

    def loeschen(self, schluessel: str) -> None:
        with self._sperre:
            self._daten.pop(schluessel, None)

    def vergessen(self, praefix: str = "") -> None:
        with self._sperre:
            for k in [k for k in self._daten if k.startswith(praefix)]:
                del self._daten[k]


ZAEHLER = Zaehler()
