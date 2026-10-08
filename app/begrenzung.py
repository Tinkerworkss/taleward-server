"""Gemeinsame Begrenzung von Versuchen (Anmelden, Einladungscodes, Kopplung, Anmeldedienste).

Je Schlüssel (z. B. „login-name:anna“) zählt ein gleitendes Fenster. Abgelaufene Schlüssel fallen weg, und die
Gesamtzahl der Schlüssel ist gedeckelt – der Speicher wächst nicht mit jedem Versuch eines Angreifers.
Gezählt wird im Arbeitsspeicher eines Prozesses; nach einem Neustart beginnt es von vorn (bewusst einfach).

Seit 0.4.62:
- Nur Zählen legt Schlüssel an, Nachsehen (`voll`) nicht.
- Ist die Obergrenze erreicht, fallen zuerst abgelaufene Schlüssel weg, dann solche mit nur einem Versuch, erst
  zuletzt der am längsten unbenutzte. Ein Schlüssel mit vielen Versuchen (eine laufende Sperre) lässt sich so nicht
  mit Massen neuer Namen verdrängen.
- `reservieren` zählt mehrere Schlüssel in fester Reihenfolge auf einmal und vor der eigentlichen Prüfung (z. B. vor
  dem Passwortvergleich); parallele Versuche können die Grenze so nicht überschreiten. Ist ein Schlüssel voll, wird
  keiner gezählt und die folgenden werden gar nicht erst angelegt (Adresse vor Name). Bei Erfolg gibt `freigeben`
  die Zählung zurück.
- `adresse` fasst IPv6-Adressen zu ihrem /64-Netz zusammen (ein Anschluss hat meist ein ganzes /64).
"""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import OrderedDict, deque

MAX_SCHLUESSEL = 20_000


def adresse(request) -> str:
    """Adresse für die Zähler: IPv4 wie sie ist, IPv6 als /64-Netz."""
    host = request.client.host if getattr(request, "client", None) else ""
    return adresse_aus(host)


def adresse_aus(host: str | None) -> str:
    if not host:
        return "?"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class Zaehler:
    def __init__(self) -> None:
        self._daten: OrderedDict[str, deque] = OrderedDict()
        self._fenster: dict[str, float] = {}
        self._sperre = threading.Lock()

    # ------------------------------------------------------------ intern (unter der Sperre)
    def _abgelaufen_entfernen(self, q: deque, fenster: float, jetzt: float) -> None:
        while q and q[0] < jetzt - fenster:
            q.popleft()

    def _platz_schaffen(self, jetzt: float) -> None:
        """Platz für einen neuen Schlüssel schaffen (danach höchstens MAX_SCHLUESSEL - 1)."""
        grenze = MAX_SCHLUESSEL - 1
        if len(self._daten) <= grenze:
            return
        for k in list(self._daten):  # 1. abgelaufene
            q = self._daten[k]
            self._abgelaufen_entfernen(q, self._fenster.get(k, 0.0), jetzt)
            if not q:
                self._weg(k)
        if len(self._daten) <= grenze:
            return
        for k in list(self._daten):  # 2. mit nur einem Versuch, älteste zuerst
            if len(self._daten) <= grenze:
                return
            if len(self._daten[k]) <= 1:
                self._weg(k)
        while len(self._daten) > grenze:  # 3. notfalls der am längsten unbenutzte
            self._weg(next(iter(self._daten)))

    def _weg(self, k: str) -> None:
        self._daten.pop(k, None)
        self._fenster.pop(k, None)

    def _holen(self, schluessel: str, fenster: float, jetzt: float) -> deque:
        q = self._daten.get(schluessel)
        if q is None:
            self._platz_schaffen(jetzt)
            q = deque()
            self._daten[schluessel] = q
            self._fenster[schluessel] = fenster
        else:
            self._daten.move_to_end(schluessel)
            self._fenster[schluessel] = max(fenster, self._fenster.get(schluessel, 0.0))
        self._abgelaufen_entfernen(q, fenster, jetzt)
        return q

    def _anzahl(self, schluessel: str, fenster: float, jetzt: float) -> int:
        q = self._daten.get(schluessel)
        if q is None:
            return 0
        return sum(1 for t in q if t >= jetzt - fenster)

    # ------------------------------------------------------------ öffentlich
    def voll(self, schluessel: str, anzahl: int, fenster: float) -> bool:
        """Sind im Fenster schon anzahl Versuche gezählt? (zählt selbst nicht, legt nichts an)"""
        with self._sperre:
            return self._anzahl(schluessel, fenster, time.monotonic()) >= anzahl

    def zaehlen(self, schluessel: str, fenster: float) -> None:
        with self._sperre:
            jetzt = time.monotonic()
            self._holen(schluessel, fenster, jetzt).append(jetzt)

    def versuch(self, schluessel: str, anzahl: int, fenster: float) -> bool:
        """Zählt einen Versuch. False, wenn das Fenster schon voll ist (dann wird nicht gezählt)."""
        return self.reservieren([(schluessel, anzahl, fenster)]) is not None

    def reservieren(self, posten: list[tuple[str, int, float]]) -> list[tuple[str, float]] | None:
        """Mehrere Schlüssel in dieser Reihenfolge zählen – alle oder keiner. None: einer war voll."""
        with self._sperre:
            jetzt = time.monotonic()
            for schluessel, anzahl, fenster in posten:  # erst nachsehen, nichts anlegen
                if self._anzahl(schluessel, fenster, jetzt) >= anzahl:
                    return None
            for schluessel, _anzahl, fenster in posten:
                self._holen(schluessel, fenster, jetzt).append(jetzt)
            return [(s, jetzt) for s, _a, _f in posten]

    def freigeben(self, reservierung: list[tuple[str, float]] | None) -> None:
        """Gezählte Versuche zurücknehmen (der Versuch war erfolgreich)."""
        if not reservierung:
            return
        with self._sperre:
            for schluessel, t in reservierung:
                q = self._daten.get(schluessel)
                if q is not None:
                    try:
                        q.remove(t)
                    except ValueError:
                        pass

    def loeschen(self, schluessel: str) -> None:
        with self._sperre:
            self._weg(schluessel)

    def vergessen(self, praefix: str = "") -> None:
        with self._sperre:
            for k in [k for k in self._daten if k.startswith(praefix)]:
                self._weg(k)


ZAEHLER = Zaehler()
