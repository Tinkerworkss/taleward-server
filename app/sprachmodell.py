"""Sprachmodell für Recap und Bibel-Vorschläge.

Läuft in der Zentrale (API) oder im Worker (Ollama auf dem lokalen Server). Kennt keine Datenbank: Die
Zentrale baut die Eingaben (`zusammenfassung.eingabe_bauen`), dieses Modul macht daraus Aufrufe und liefert ein
geprüftes Ergebnis als Wörterbuch zurück.

Spoilerschutz – verbindlich:
- Recap und Vorschläge sind zwei getrennte Aufrufe. Der Recap-Aufruf bekommt ausschließlich `recap` (öffentliche
  Einträge ohne gmNotes, keine Namen geheimer Einträge). Nur der Vorschlags-Aufruf bekommt `vorschlaege`
  (ganze Bibel inkl. gmNotes, wie in der Schnittstelle festgelegt).
- Lange Transkripte werden vorab in Stücken zu Szenennotizen verdichtet. Dieser Schritt sieht nur Transkript und
  Personen – nie die Bibel.
- SL-Notizen der Sessions und Charakter-Hintergründe kommen in keiner Eingabe vor (dafür sorgt die Zentrale).
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

log = logging.getLogger("worker")

ENTRY_TYPES = ("npc", "location", "quest", "item", "faction", "other")
FLAGS = ("joke_suspected", "low_confidence", "contradicts_bible")
MAX_VORSCHLAEGE = 15  # so viele sieht die Spielleitung höchstens
MAX_VORSCHLAEGE_ROH = 25  # so viele darf das Modell liefern; der Server wählt die gewichtigsten aus
ANTWORT_HOECHSTENS = 4096  # Tokens je Antwort eines lokalen Modells
NOTIZ_STUECK = 6000  # höchstens so viele Token Transkript je Aufruf für Szenennotizen
BISHER = 8  # so viele Notizen des vorigen Abschnitts gehen als Vorgeschichte mit
NOTIZEN_HOECHSTENS = 15  # je Abschnitt; mehr sprengt die Antwortlänge, und das Ende des Abschnitts geht verloren
TEIL_TOKEN = 2000  # lange Runden: höchstens so viele Token Notizen je Teil, der vor dem Recap eigens zusammengefasst wird
TEIL_MINUTEN = 30  # … und Teile nach Spielzeit geschnitten, damit jeder Teil gleich viel Platz im Kapitel bekommt
ZEICHEN_PRO_TOKEN = 3.2  # grobe Schätzung für deutsche und englische Texte

# Cent je 1 Mio. Tokens (ein, aus) – Stand 09/2026, Dollarpreise ≈ Euro. Nur für die Verbrauchsanzeige.
PREISE = {
    "mistral-large-latest": (50, 150), "mistral-large-2512": (50, 150),
    "mistral-medium-latest": (150, 750), "mistral-small-latest": (15, 60),
    "ministral-8b-latest": (15, 15), "ministral-14b-latest": (20, 20),
}


class SprachmodellFehler(Exception):
    def __init__(self, text: str, erneut: bool = True):
        super().__init__(text)
        self.erneut = erneut


class AntwortFehler(SprachmodellFehler):
    """Das Modell hat geantwortet, aber unbrauchbar (kein JSON, Schleife) – ein kleinerer Abschnitt hilft oft."""


# ---------------------------------------------------------------- Anbindungen
@dataclass
class Antwort:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


class Klient(Protocol):
    modell: str

    def chat(self, system: str, nutzer: str) -> Antwort: ...


def anbieter_meldung(r: httpx.Response) -> str:
    """Wortlaut, mit dem der Anbieter einen Aufruf ablehnt (z. B. „Requests rate limit exceeded“) – kurz und ohne
    Zeilenumbrüche. Die Anbieter verpacken ihn verschieden: message, error.message, detail oder nur Text."""
    text = ""
    try:
        d = r.json()
        if isinstance(d, dict):
            fehler = d.get("error")
            text = (d.get("message") or (fehler.get("message") if isinstance(fehler, dict) else fehler)
                    or d.get("detail") or "")
            if isinstance(text, (list, dict)):
                text = json.dumps(text, ensure_ascii=False)
    except ValueError:
        text = r.text
    return " ".join(str(text or "").split())[:200]


VERSUCHE = 4  # je Aufruf beim Cloud-Anbieter


def wartezeit(r: httpx.Response, versuch: int) -> float:
    """Wie lange vor dem nächsten Versuch warten: „Retry-After“ des Anbieters (Sekunden), sonst 15, 30, 60 s – bei
    429 begrenzt der Anbieter Token je Minute, wenige Sekunden reichen dann nicht. Höchstens 2 min."""
    try:
        sek = float(r.headers.get("retry-after") or "")
    except ValueError:
        sek = 15.0 * 2 ** versuch if r.status_code == 429 else 5.0 * (versuch + 1)
    return max(1.0, min(sek, 120.0))


class OpenAIKlient:
    """OpenAI-kompatible Schnittstelle (Mistral, OpenAI, viele andere): POST {url}/chat/completions."""

    def __init__(self, url: str, api_key: str, modell: str, client: httpx.Client | None = None,
                 cent_pro_mio: tuple[float, float] | None = None):
        self.url, self.modell = url.rstrip("/"), modell
        # Lesegrenze 5 min je Aufruf (Antworten kommen ohne Streaming am Stück; selbst lange Kapitel dauern bei den
        # Anbietern unter 2 min). Ein Anbieter, der gar nicht antwortet, kostet so höchstens 5 min statt 10.
        self.client = client or httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0))
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.cent_pro_mio = cent_pro_mio or PREISE.get(modell, (0, 0))

    def chat(self, system: str, nutzer: str) -> Antwort:
        body = {"model": self.modell, "temperature": 0.3, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": nutzer}]}
        for versuch in range(VERSUCHE):
            letzter = versuch == VERSUCHE - 1
            try:
                r = self.client.post(f"{self.url}/chat/completions", json=body, headers=self.headers)
            except httpx.TimeoutException as e:
                if letzter:
                    raise SprachmodellFehler("Der Anbieter hat nicht rechtzeitig geantwortet.") from e
                time.sleep(5)
                continue
            except httpx.HTTPError as e:
                if letzter:
                    raise SprachmodellFehler(f"Sprachmodell nicht erreichbar ({type(e).__name__}).") from e
                time.sleep(2 * (versuch + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if letzter:
                    grund = anbieter_meldung(r)
                    raise SprachmodellFehler(f"Der Anbieter antwortet mit {r.status_code}" + (f": {grund}" if grund
                                                                                              else "."))
                time.sleep(wartezeit(r, versuch))
                continue
            if r.status_code in (401, 403):
                grund = anbieter_meldung(r)
                raise SprachmodellFehler(f"Der Anbieter lehnt den API-Schlüssel ab ({r.status_code}"
                                         + (f": {grund})" if grund else ")."), erneut=False)
            if r.status_code >= 400:
                raise SprachmodellFehler(f"Der Anbieter lehnt die Anfrage ab ({r.status_code}): {anbieter_meldung(r)}",
                                         erneut=False)
            d = r.json()
            u = d.get("usage") or {}
            return Antwort(d["choices"][0]["message"]["content"] or "", int(u.get("prompt_tokens") or 0),
                           int(u.get("completion_tokens") or 0))
        raise SprachmodellFehler("Sprachmodell nicht erreichbar.")

    def kosten_cent(self, tokens_in: int, tokens_out: int) -> int:
        ein, aus = self.cent_pro_mio
        return round((tokens_in * ein + tokens_out * aus) / 1_000_000)

    def modelle(self) -> list[str] | None:
        """GET {url}/models – welche Modelle der Schlüssel nutzen darf. None, wenn der Anbieter das nicht anbietet."""
        try:
            r = self.client.get(f"{self.url}/models", headers=self.headers, timeout=30.0)
        except httpx.HTTPError:
            return None
        if r.status_code in (401, 403):
            raise SprachmodellFehler("Der Anbieter lehnt den API-Schlüssel ab.", erneut=False)
        if r.status_code >= 400:
            return None
        try:
            daten = r.json().get("data") or []
            namen = sorted(str(m.get("id")) for m in daten if isinstance(m, dict) and m.get("id"))
        except (ValueError, AttributeError):
            return None
        return namen

    def pruefen(self) -> dict:
        """Kleiner Probeaufruf: erreichbar, Schlüssel gültig, Modell vorhanden, JSON kommt an.

        Liefert {"json": bool, "tokens": int, "modelle": list | None}; Fehler als SprachmodellFehler."""
        modelle = self.modelle()
        try:
            a = self.chat('Antworte nur mit dem JSON-Objekt {"ok": true}.', 'Bitte {"ok": true} zurückgeben.')
        except SprachmodellFehler as e:
            if modelle and self.modell not in modelle:
                # Der Schlüssel hat die Modellliste geholt, ist also gültig – nur dieses Modell darf er nicht nutzen.
                raise SprachmodellFehler(f"Der Schlüssel ist gültig, aber das Modell {self.modell} ist für ihn nicht "
                                         f"freigeschaltet. Bitte eines aus der Liste wählen. ({e})",
                                         erneut=False) from e
            raise
        try:
            json_ok = bool(json.loads(a.text).get("ok"))
        except (ValueError, AttributeError):
            json_ok = False
        return {"json": json_ok, "tokens": a.tokens_in + a.tokens_out, "modelle": modelle}


class OllamaKlient:
    """Ollama auf dem lokalen Server (native Schnittstelle, weil nur sie die Kontextgröße einstellen lässt).
    Das Modell bleibt nur kurz geladen, damit die Grafikkarte für die Transkription frei wird."""

    STILLSTAND_S = 180.0   # so lange darf zwischen zwei Antwortstücken höchstens vergehen (Modell laden: Minuten)
    LANGSAM_TOKEN_S = 2.0  # darunter ist der Rechner für Recaps zu schwach (Prozessor statt Grafikkarte)

    def __init__(self, url: str, modell: str, kontext: int = 12288, client: httpx.Client | None = None):
        self.url, self.modell, self.kontext = url.rstrip("/"), modell, kontext
        self.client = client or httpx.Client(timeout=httpx.Timeout(self.STILLSTAND_S, connect=10.0))
        self.token_s: float | None = None  # gemessene Geschwindigkeit des letzten Aufrufs
        self._denkt: bool | None = None  # Modell mit Denkmodus (Qwen3 u. a.)? Einmal bei Ollama nachgefragt
        self.temperatur: float | None = None  # Testoption: feste Temperatur für den ersten Versuch statt 0.3

    def denkmodus(self) -> bool:
        """Kann das Modell „laut denken“? Dann schalten wir es ab: Für Recaps bringt es nichts außer langer Laufzeit
        und vielen Tokens. Ältere Ollama-Fassungen kennen „capabilities“ nicht – dann bleibt alles wie bisher."""
        if self._denkt is None:
            try:
                r = self.client.post(f"{self.url}/api/show", json={"model": self.modell}, timeout=15.0)
                self._denkt = r.status_code == 200 and "thinking" in (r.json().get("capabilities") or [])
            except Exception:  # noqa: BLE001 – die Nachfrage darf nie einen Aufruf kosten
                self._denkt = False
        return self._denkt

    def version(self) -> str | None:
        try:
            r = self.client.get(f"{self.url}/api/version", timeout=3.0)
            return r.json().get("version") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def bereitstellen(self, melden: Callable[[str], None] = lambda _t: None) -> str:
        """Modell holen, falls es noch nicht auf dem Worker liegt. Liefert die Kennung (digest, 12 Zeichen)."""
        def suchen():
            r = self.client.get(f"{self.url}/api/tags")
            r.raise_for_status()
            for m in r.json().get("models", []):
                if m.get("name") == self.modell or m.get("model") == self.modell or \
                        m.get("name") == f"{self.modell}:latest":
                    return (m.get("digest") or "")[:12] or "?"
            return None

        try:
            kennung = suchen()
            if kennung is None:
                melden(f"Lade Sprachmodell {self.modell} (einmalig, einige GB) …")
                self._ziehen(melden)
                kennung = suchen() or "?"
            return kennung
        except httpx.HTTPError as e:
            raise SprachmodellFehler(f"Ollama ist nicht erreichbar ({type(e).__name__}).") from e

    def _ziehen(self, melden: Callable[[str], None]) -> None:
        """Modell laden und alle 10 % eine Zeile melden (sonst bleibt das Protokoll bei ~6 GB lange stumm).
        Kein Gesamtzeitlimit: Solange Daten kommen, läuft es weiter – nur eine lange Pause bricht ab."""
        import json as _json

        gemeldet = -10
        with self.client.stream("POST", f"{self.url}/api/pull", json={"model": self.modell, "stream": True},
                                timeout=httpx.Timeout(10.0, read=600.0)) as r:
            if r.status_code >= 400:
                raise SprachmodellFehler(f"Ollama kann {self.modell} nicht laden: {r.read().decode(errors='replace')[:200]}",
                                         erneut=False)
            for zeile in r.iter_lines():
                if not zeile.strip():
                    continue
                try:
                    d = _json.loads(zeile)
                except ValueError:
                    continue
                if d.get("error"):
                    raise SprachmodellFehler(f"Ollama kann {self.modell} nicht laden: {str(d['error'])[:200]}",
                                             erneut=False)
                gesamt, fertig = d.get("total") or 0, d.get("completed") or 0
                if gesamt > 2 ** 27:  # nur die großen Teile (die Gewichte), nicht Vorlage/Lizenz
                    prozent = int(fertig * 100 / gesamt)
                    if prozent >= gemeldet + 10:
                        gemeldet = prozent - prozent % 10
                        melden(f"Sprachmodell {self.modell}: {gemeldet} % von {gesamt / 2 ** 30:.1f} GB")

    # (Temperatur, Wiederholungsstrafe, JSON-Grammatik) je Versuch. Der dritte läuft ohne die erzwungene
    # JSON-Grammatik: sie verbietet dem Modell das Aufhören mitten in der Struktur, und genau dort drehen kleine,
    # quantisierte Modelle ihre Schleifen – frei formuliert liefern sie meist doch noch JSON, das _json() herauslöst.
    VERSUCHE = ((0.3, 1.1, True), (0.6, 1.2, True), (0.8, 1.3, False))

    def chat(self, system: str, nutzer: str) -> Antwort:
        # Kleine Modelle geraten im JSON-Modus gern in eine Schleife (Ollama bricht dann mit „token repeat limit
        # reached“ ab). Daher eine leichte Wiederholungsstrafe und eine Obergrenze für die Antwortlänge – und bei einem
        # Abbruch weitere Versuche mit mehr Streuung, der letzte ohne JSON-Grammatik.
        # Streamend, mit Stillstands-Zeitgrenze statt Gesamtzeit: auf einem schwachen Rechner dauert eine Antwort
        # auch mal 20 Minuten – solange Stücke kommen, ist alles gut; kommt drei Minuten nichts, hängt Ollama.
        status, fehler, d = 0, "", {}
        for versuch, (temperatur, strafe, grammatik) in enumerate(self.VERSUCHE):
            if versuch == 0 and self.temperatur is not None:
                temperatur = self.temperatur
            optionen = {"num_ctx": self.kontext, "temperature": temperatur, "repeat_penalty": strafe,
                        "repeat_last_n": 256, "num_predict": ANTWORT_HOECHSTENS}
            if versuch:
                optionen["frequency_penalty"] = 0.2 * versuch
            body = {"model": self.modell, "stream": True, "keep_alive": "2m", "options": optionen,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": nutzer}]}
            if grammatik:
                body["format"] = "json"
            if self.denkmodus():
                body["think"] = False
            try:
                status, fehler, d = self._streamen(body)
            except httpx.ReadTimeout as e:
                raise SprachmodellFehler(f"Ollama antwortet seit {int(self.STILLSTAND_S)} s nicht mehr – "
                                         "Modell hängt oder der Rechner ist überlastet.") from e
            except httpx.HTTPError as e:
                raise SprachmodellFehler(f"Ollama ist nicht erreichbar ({type(e).__name__}).") from e
            if (status >= 400 or fehler) and "repeat" in fehler and versuch + 1 < len(self.VERSUCHE):
                log.info("Sprachmodell hat sich wiederholt – %s Versuch mit mehr Streuung%s",
                         ("zweiter", "dritter")[versuch], "" if self.VERSUCHE[versuch + 1][2] else ", ohne JSON-Grammatik")
                continue
            break
        if (status >= 400 or fehler) and "repeat" in fehler:
            raise AntwortFehler(f"Das Sprachmodell hat sich in {len(self.VERSUCHE)} Versuchen festgefahren "
                                     f"(Wiederholungsschleife; Ollama: {fehler[:120]}). Ein anderes oder größeres "
                                     "Modell hilft – Verwaltung → Transkription → Lokales Sprachmodell.")
        if status >= 400 or fehler:
            raise SprachmodellFehler(f"Ollama meldet {status}: {fehler[:200]}")
        tokens_aus, dauer_ns = int(d.get("eval_count") or 0), int(d.get("eval_duration") or 0)
        if tokens_aus >= 50 and dauer_ns > 0:
            self.token_s = tokens_aus / (dauer_ns / 1e9)
            if self.token_s < self.LANGSAM_TOKEN_S:
                log.warning("Sprachmodell sehr langsam (%.1f Token/s) – dieser Rechner sollte Recaps dem Server "
                            "überlassen (Worker-App: „Recaps hier schreiben“ abschalten)", self.token_s)
        return Antwort(d.get("inhalt", ""), int(d.get("prompt_eval_count") or 0), tokens_aus)

    def _streamen(self, body: dict) -> tuple[int, str, dict]:
        """(Status, Fehlertext, Daten) – Daten enthalten den zusammengesetzten Text unter „inhalt“."""
        import json as _json

        with self.client.stream("POST", f"{self.url}/api/chat", json=body) as r:
            if r.status_code >= 400:
                text = r.read().decode("utf-8", "replace")
                try:
                    fehler = str(_json.loads(text).get("error") or text)
                except ValueError:
                    fehler = text
                return r.status_code, fehler, {}
            teile: list[str] = []
            letztes: dict = {}
            for zeile in r.iter_lines():
                if not zeile.strip():
                    continue
                try:
                    d = _json.loads(zeile)
                except ValueError:
                    continue
                if d.get("error"):
                    return 500, str(d["error"]), {}
                teile.append((d.get("message") or {}).get("content") or "")
                if d.get("done"):
                    letztes = d
        letztes["inhalt"] = "".join(teile)
        return 200, "", letztes

    def entladen(self, warten_s: float = 20.0) -> None:
        """Grafikspeicher sofort freigeben (der nächste Auftrag kann eine Transkription sein) – und kurz warten,
        bis das Modell wirklich weg ist, sonst lädt Whisper in einen noch halb belegten Speicher."""
        try:
            self.client.post(f"{self.url}/api/generate", json={"model": self.modell, "keep_alive": 0}, timeout=30)
        except httpx.HTTPError:
            return
        ende = time.monotonic() + warten_s
        while time.monotonic() < ende:
            try:
                r = self.client.get(f"{self.url}/api/ps", timeout=5)
                geladen = [m.get("name") or m.get("model") for m in r.json().get("models", [])]
            except (httpx.HTTPError, ValueError):
                return
            if not geladen:
                return
            time.sleep(0.5)

    def kosten_cent(self, tokens_in: int, tokens_out: int) -> int:
        return 0


# ---------------------------------------------------------------- Texte für das Modell
def _zeit(sek: float) -> str:
    sek = int(sek)
    return f"{sek // 3600}:{sek % 3600 // 60:02d}:{sek % 60:02d}" if sek >= 3600 else f"{sek // 60}:{sek % 60:02d}"


def zeit_lesen(wert) -> float | None:
    """„1:02:03“, „12:34“ oder Sekunden → Sekunden."""
    if isinstance(wert, (int, float)):
        return float(wert) if wert >= 0 else None
    if isinstance(wert, str):
        teile = wert.strip().strip("[]").split(":")
        try:
            zahlen = [float(t) for t in teile]
        except ValueError:
            return None
        s = 0.0
        for z in zahlen:
            s = s * 60 + z
        return s
    return None


def _wort(w: str) -> str:
    return w.casefold().strip('",.!?;:…„“')


def entdoppeln(text: str, mindestens: int = 4) -> str:
    """Eine Wortfolge (1–6 Wörter), die sich vier- oder mehrmals hintereinander wiederholt, bleibt einmal stehen –
    Spracherkennung halluziniert bei Stille oder Musik gern „Danke. Danke. Danke. …“, und ein kleines Sprachmodell
    schreibt so etwas gern weiter, bis Ollama abbricht."""
    woerter = text.split()
    out: list[str] = []
    i = 0
    while i < len(woerter):
        gefunden = 0
        for n in range(1, 7):
            if i + n * mindestens > len(woerter):
                break
            muster = [_wort(w) for w in woerter[i:i + n]]
            if not all(muster):
                continue
            k = 1
            while [_wort(w) for w in woerter[i + k * n:i + (k + 1) * n]] == muster:
                k += 1
            if k >= mindestens:
                out += woerter[i:i + n] + ["…"]
                gefunden = k * n
                break
        if gefunden:
            i += gefunden
        else:
            out.append(woerter[i])
            i += 1
    return " ".join(out)


def transkript_zeilen(transkript: list[dict]) -> list[str]:
    """Zeilen „[m:ss] Sprecher: Text“; aufeinanderfolgende Zeilen derselben Person werden zusammengelegt.
    Aufeinanderfolgende gleichlautende Stücke (Halluzination der Spracherkennung) stehen nur einmal."""
    out: list[str] = []
    letzter, puffer, start, zuletzt = None, [], 0.0, ""
    for z in transkript:
        text = entdoppeln(z["text"].strip())
        if z["sprecher"] != letzter or sum(len(p) for p in puffer) > 1500:
            if puffer:
                out.append(f"[{_zeit(start)}] {letzter}: {' '.join(puffer)}")
            letzter, puffer, start, zuletzt = z["sprecher"], [], z["start"], ""
        if text.casefold() == zuletzt and len(text) > 1:
            continue
        zuletzt = text.casefold()
        puffer.append(text)
    if puffer:
        out.append(f"[{_zeit(start)}] {letzter}: {' '.join(puffer)}")
    return out


def tokens(text: str) -> int:
    return int(len(text) / ZEICHEN_PRO_TOKEN) + 1


def stuecke(zeilen: list[str], max_tokens: int) -> list[str]:
    teile, aktuell, n = [], [], 0
    for z in zeilen:
        t = tokens(z)
        if aktuell and n + t > max_tokens:
            teile.append("\n".join(aktuell))
            aktuell, n = [], 0
        aktuell.append(z)
        n += t
    if aktuell:
        teile.append("\n".join(aktuell))
    return teile


from app.artefakte import ZAHLWORT_PUNKTE as _ZAHLWORT_PUNKTE  # noqa: E402  (0.4.76: Zahlwörter wie Ziffern)

_ZEIT_VORN = re.compile(r"^\s*\[?(\d{1,2}(?::\d{2}){1,2})\]\s*")  # „[12:34] “, zur Not auch „12:34] “
_REGELN = re.compile(r"\b(?:Würfel\w*|würfel\w*|\w*[Pp]robe\b|\w*[Pp]roben\b|\w*attacke\b|Kampfrunde\w*|Qualitätsstufe"
                     r"|QS\s?\d|Lebenspunkt\w*|Karmapunkt\w*|Astralpunkt\w*|Zauberpunkt\w*|Schadenspunkt\w*|LeP|KaP|AsP|ASP"
                     r"|Initiative|erleichtert\w*|erschwert\w*|kritisch\w* (?:erfolgreich|Erfolg\w*|Patzer|Wurf\w*|Treffer|bei)|Patzer"
                     r"|Wurf\b|Wurfs\b|Würfe\w*|[bB]20\b|\d+ Schaden\b|Schaden(?:spunkte)?\b|\d+ ?\w*[Pp]unkte?\b|" + _ZAHLWORT_PUNKTE +
                     r"|\d+\s?[wW]\d+|[wW]20\b|\d-\d{1,2}-\d{1,2}|Schicksalsmarker"
                     r"|Spielleitung (?:verlangt|fordert|erlaubt|bittet|kündigt|informiert|bestätigt|stellt fest|teilt mit)"
                     r"|Wiederholung aus Bisher|Gewinnspiel|Pause)\b")
_SPIELLEITUNG = re.compile(r"\b(?:Spielleitung|Spielleiter\w*|game master|GM)\b", re.I)
# 0.4.73: Regelreste in Klammern – systemneutral über Zahlen und Ausgangswörter („(39 von 50, Erfolg)“, „(4 Trefferpunkte)“,
# „(Schwimmwurf: 46 von 20, gescheitert)“). Die Klammer fällt weg, die Notiz bleibt.
_REGELKLAMMER = re.compile(
    r"\s*\((?=[^()]*(?:\b\d+\s*(?:von|/|auf|gegen|vs\.?)\s*\d+|\b\d+ ?\w*[Pp]unkte?\b|\b\d+ [Ss]chaden\b|\w+[Ww]urf\b"
    r"|\b" + _ZAHLWORT_PUNKTE +
    r"|\w*[Pp]robe\b))[^()]*\)")
UNKLAR = "(unklar, wer)"  # 0.4.73: Marke der Notizen für Stellen, an denen die Abschrift nicht eindeutig ist
_UNKLAR = re.compile(r"\s*\((?:unklar|unsicher|unclear|uncertain)\b[^)]*\)", re.I)


def regelreste(text: str) -> str:
    """Regelreste in Klammern entfernen und die Satzzeichen glätten."""
    t = _REGELKLAMMER.sub("", text or "")
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def unklar_stellen(notizen: str) -> list[dict]:
    """Notizen mit der Marke „(unklar …)“ → [{zeit, notiz}]: Stellen, an denen das Modell nicht raten wollte. Für die
    Spielleitung die ehrlichste Prüfhilfe – genau hier lohnt der Blick in die Abschrift."""
    aus = []
    for n in (notizen or "").split("\n"):
        if _UNKLAR.search(n):
            zeit = _zeit_vorn(n)
            aus.append({"zeit": zeit, "zeit_text": _zeit(zeit) if zeit is not None else "", "notiz": _ZEIT_VORN.sub("", n).strip()})
    return aus
ABDECKUNG = 0.75  # so weit (zeitlich) müssen die Notizen in den Abschnitt hineinreichen, sonst wird der Rest nachgeholt
ZEIT_ANTEIL = 0.5  # weniger Notizen mit Zeitstempel: einmal neu anfordern – ohne Zeiten greift keine Prüfung
ERINNERUNG_ZEIT = ("\n\nWichtig: Jede Notiz beginnt mit dem Zeitstempel der Zeile, aus der sie stammt, in eckigen "
                   "Klammern, z. B. „[1:09:07] …“. Notizen ohne Zeitstempel sind unbrauchbar.\n")


def _notiz_normieren(n: str) -> str:
    """„12:34] Text“ oder „[12:34]Text“ → „[12:34] Text“. Ohne Zeitstempel unverändert."""
    m = _ZEIT_VORN.match(n)
    return f"[{m.group(1)}] {n[m.end():].strip()}" if m else n.strip()


def _bruchstueck(n: str) -> bool:
    """Weniger als drei Wörter nach dem Zeitstempel: abgeschnitten oder leer („[2:22:14] Die“)."""
    m = _ZEIT_VORN.match(n)
    return len((n[m.end():] if m else n).split()) < 3


def _zeit_vorn(zeile: str) -> float | None:
    m = _ZEIT_VORN.match(zeile)
    return zeit_lesen(m.group(1)) if m else None


def _minuten(ein: dict) -> float:
    zeilen = ein.get("transkript") or []
    return max((float(z.get("start") or 0) for z in zeilen), default=0.0) / 60


def woerter(ein: dict, lang: bool = False) -> str:
    """Länge des Recaps nach Länge der Runde: Ein langer Abend hat mehr Wendepunkte als eine kurze Szene.
    lang (0.4.63, Weg „Notizen zuerst“): sehr lange Runden bekommen mehr Platz, damit kurze Ereignisse nicht wegfallen.
    0.4.78: ab 3 h je angefangene Stunde mehr Platz, mit Deckel ab 5 h – sonst wird es ein Protokoll (gemessen: 7 h in
    ≈ 1.000 Wörtern, die zweite Hälfte des Abends in drei Absätzen)."""
    return woerter_fuer(_minuten(ein), lang)


def woerter_fuer(minuten: float, lang: bool = False) -> str:
    """Längenvorgabe für eine Dauer in Minuten (siehe woerter)."""
    if minuten > 300:
        return "1800–2400" if lang else "1200–1800"
    if minuten > 240:
        return "1500–2100" if lang else "1000–1600"
    if minuten > 180:
        return "1200–1800" if lang else "800–1400"
    if minuten > 120:
        return "900–1500" if lang else "600–1200"
    if minuten > 60:
        return "400–900"
    return "250–600"


def _sprache(ein: dict) -> str:
    return "English" if ein.get("sprache") == "en" else "Deutsch"


def _kopf(ein: dict) -> str:
    """Kampagne, System, Welt und die Runde – für alle Aufrufe gleich (enthält nichts Geheimes)."""
    zeilen = [f"Kampagne: {ein['kampagne']}"]
    if ein.get("system_name") or ein.get("system"):
        zeilen.append(f"Regelsystem: {ein.get('system_name') or ein.get('system')}")
    if ein.get("welt"):
        zeilen.append(f"Welt (Spielerwissen):\n{ein['welt'][:4000]}")
    zeilen.append(f"Session: Kapitel {ein['session_nummer']}" + (f" – {ein['session_titel']}"
                                                                 if ein.get("session_titel") else ""))
    runde = []
    for p in ein["personen"]:
        if p["rolle"] == "gm":
            runde.append("- Spielleitung (leitet die Runde, keine Figur)")
        else:
            c = p.get("charakter") or "(ohne Charaktername)"
            kurz = f" – {p['charakter_kurz']}" if p.get("charakter_kurz") else ""
            runde.append(f"- {p['name']} spielt {c}{kurz}")
    runde += [f"- {g}: Gast" for g in ein.get("gaeste", [])]
    zeilen.append("Am Tisch:\n" + "\n".join(runde))
    return "\n".join(zeilen)


SYSTEM_NOTIZEN = """Du hilfst bei der Nachbereitung einer Pen-&-Paper-Rollenspielsession. Du bekommst einen Abschnitt \
des Transkripts (automatisch erkannt, mit Fehlern; Sprecher sind Charaktere oder die Spielleitung).
Schreibe Szenennotizen zu diesem Abschnitt. Aus ihnen entsteht später der Recap – was hier fehlt, fehlt dort auch.
Halte vor allem fest:
- Wendepunkte und ihren Ausgang: wer gefangen, verurteilt, befreit, gerettet, verletzt oder getötet wird, wer flieht, \
wer wem etwas gibt, verspricht oder schuldet, welche Abmachungen mit welchen Bedingungen getroffen werden.
- Ereignisse, die die Lage ändern (Angriffe, Einstürze, Erscheinungen, Visionen) – Botschaften wörtlich in \
Anführungszeichen.
- Orte mit Namen, Nichtspielercharaktere mit Namen und Rolle, Gegenstände, Aufträge, Entscheidungen der Gruppe, offene \
Fragen.
- Auch Nebensätze mit Folgen: der Tod einer Nebenfigur („der ist tot“), bestehende Beziehungen („wir kennen uns, seit \
…“, „mein Bruder“, „schuldet mir“), Eigennamen von Gegenständen und Orten wörtlich („das Schwert Eisenwind“ statt \
„ein Schwert“ – mit dem Namen, der gesagt wird), und bei mehreren Personen in einer Szene, wer genau was tut.
Schreib den Zustand genau so, wie er am Tisch war: verletzt ist nicht tot, angedroht ist nicht geschehen, geplant ist \
nicht getan.
Höchstens {hoechstens} Notizen für den ganzen Abschnitt, gleichmäßig über seine Dauer verteilt – bis zur letzten \
Zeile. Kleinigkeiten (Essen, Smalltalk, einzelne Fragen) fasst du zusammen oder lässt sie weg, damit Platz für das \
Ende des Abschnitts bleibt.
Jede Notiz beginnt mit dem Zeitstempel der Zeile, aus der sie stammt, in eckigen Klammern, z. B. „[12:34]“ oder \
„[1:09:07]“ – ohne Zeitstempel ist eine Notiz unbrauchbar. Übernimm Namen genau so, wie sie gesagt werden. \
Der Sprecher „Spielleitung“ ist keine Figur der Geschichte: Spricht er, erzählt er oder spricht für einen \
Nichtspielercharakter – schreib dann den Namen dieser Figur; das Wort „Spielleitung“ kommt in den Notizen nicht vor. \
Schreib die Zeilen nicht ab, sondern fasse sie zusammen. \
Lass Regelfragen, Würfelwürfe, Werte (Lebenspunkte, Karma, Proben, Qualitätsstufen), Pausen und Gespräche außerhalb \
des Spiels ganz weg. Offensichtliche Witze markierst du mit „(Witz?)“. Erfinde nichts. \
Ist aus dem Abschnitt nicht eindeutig, wer handelt oder gemeint ist – Anrede ohne Namen („du“, „dich“), verstümmelte \
Stelle, mehrere mögliche Personen –, dann rate nicht: Schreib die Handlung ohne Namen und dahinter „(unklar, wer)“. \
Eine Person, die im Abschnitt keinen Namen hat, bekommt auch in der Notiz keinen („eine Bedienstete“) – nicht den \
Namen einer anderen Figur, die passen könnte. Steht vor dem Abschnitt \
„Bisher“, ist das nur zur Orientierung – nicht wiederholen.
Antworte nur mit JSON: {"notizen": ["[m:ss] …", …]}. Sprache der Notizen: {sprache}."""

# 0.4.63, Weg „Notizen zuerst“: laufender Stand von Abschnitt zu Abschnitt
ZUSATZ_STAND = """
Außerdem führst du einen kurzen Stand der Runde. Vor dem Abschnitt steht der bisherige Stand (falls es schon einen \
gibt). Gib ihn aktualisiert zurück: wer bei der Gruppe ist, wer verletzt, gefangen oder tot ist, wer welchen \
benannten Gegenstand hat, wer wen kennt oder wem etwas schuldet, welche Abmachungen gelten, welche Fragen offen sind. \
Eine kurze Zeile je Tatsache. Ändert sich etwas, ersetze die alte Zeile und schreib den Zeitstempel der Änderung \
dazu, z. B. „Kano: tot (seit [1:02:10])“. Was nicht mehr gilt, fällt weg. Höchstens {stand_hoechstens} Zeilen. Nur, \
was am Tisch gesagt wurde.
Für Notizen und Stand gilt außerdem (0.4.68): Eine Zusage, Abmachung, ein Kauf, ein Tod oder ein Besitzwechsel steht nur da, wenn er am Tisch ausdrücklich gesagt oder gezeigt wird (ein Ja, „abgemacht“, Handschlag, Übergabe, „der ist tot“). Eine höfliche, ausweichende oder unterbrochene Antwort ist keine Zusage – schreib dann „Antwort offen“ und was stattdessen geschieht. Gibt sich eine Figur als jemand anderes aus oder nennt einen erfundenen Namen, ist das keine neue Person: „X gibt sich als Y aus“.
Auch im Stand gilt: Was nicht eindeutig ist, bekommt „(unklar)“ statt einer Vermutung.
Steht vor dem Abschnitt „Unmittelbar davor“, ist das nur zum Verständnis – dazu schreibst du keine Notizen.
Antworte dann mit JSON: {"notizen": ["[m:ss] …", …], "stand": ["…", …]}."""
STAND_HOECHSTENS = 40

SYSTEM_TEIL = """Du hilfst bei der Nachbereitung einer langen Pen-&-Paper-Rollenspielsession. Du bekommst die \
Szenennotizen zu einem Teil der Runde (mit Zeitstempeln). Fasse diesen Teil in 4 bis 8 Sätzen zusammen, in der \
Reihenfolge des Geschehens.
- Jeder Wendepunkt mit seinem Ausgang: wer gefangen, verurteilt, befreit, gerettet, verletzt oder getötet wird, wer \
flieht, wer wem was gibt oder verspricht, welche Abmachungen gelten. Erscheinungen und Visionen mit ihrer Botschaft.
- Orte und Nichtspielercharaktere mit ihren Namen. Verletzt ist nicht tot, angedroht ist nicht geschehen.
- Nichts aus anderen Teilen, nichts erfinden, keine Regeln, Würfe oder Punkte. Die Spielleitung ist keine Figur – \
nenne die Nichtspielercharaktere, für die sie spricht.
Antworte nur mit JSON: {"zusammenfassung": "…"}. Sprache: {sprache}."""

SYSTEM_RECAP = """Du schreibst den Recap („Was bisher geschah“) einer Pen-&-Paper-Rollenspielsession. Er wird vor der \
nächsten Session allen Spielern vorgelesen.
Regeln:
- Nur, was am Tisch als Spielgeschehen passiert ist. Nichts erfinden, nichts ausschmücken, was nicht vorkam.
- Erzählstimme in der Vergangenheit, lebendig und vorlesbar, im Ton der Kampagne und ihrer Welt. {laenge} \
(ein Absatz je Szene oder Wendung), durch Leerzeilen getrennt.
- Steht in der Grundlage „(unklar, wer)“, bleibt das Kapitel an dieser Stelle vage („einer der Gefährten“, „jemand“): \
kein Name, keine Vermutung; die Marke selbst kommt nicht ins Kapitel.
- Alle Wendepunkte der Grundlage in ihrer Reihenfolge, jeder mit seinem Ausgang – lieber knapp erzählt als \
weggelassen. Ausgänge genau wie in der Grundlage: Wer verletzt ist, ist nicht tot; was angedroht war, ist nicht \
geschehen; wer etwas wofür gibt, steht so in der Grundlage.
- Die Figuren heißen nach ihren Charakteren, nicht nach den Menschen am Tisch. Die Spielleitung ist keine Figur: \
Was sie sagt, sagt ein Nichtspielercharakter oder die Erzählung; das Wort „Spielleitung“ kommt im Recap nicht vor. \
Regeln, Würfe, Punkte und Gespräche außerhalb des Spiels kommen nicht vor.
- Offensichtliche Witze sind kein Spielgeschehen.
- Titel: „Kapitel {nummer}: “ und ein kurzer, stimmungsvoller Titel.
- Offene Fäden: 0 bis 6 kurze Sätze zu ungelösten Fragen, Versprechen und Zielen der Gruppe.
- Reiner Text ohne Markdown: keine Sternchen, keine Rauten, keine Zwischenüberschriften, keine Listen.
Antworte nur mit JSON: {"title": "…", "text": "…", "openThreads": ["…"]}. Sprache: {sprache}."""

SYSTEM_VORSCHLAEGE = """Du pflegst die Kampagnen-Bibel einer Pen-&-Paper-Runde (Einträge: npc, location, quest, \
item, faction, other). Aus der Session schlägst du Änderungen vor; die Spielleitung prüft jeden Vorschlag.
Arten:
- create: etwas Neues, das noch nicht in der Bibel steht (auch nicht unter anderer Schreibweise).
- update: ein vorhandener Eintrag (targetEntryId aus „Bibel“ oder „Geheime Einträge“) bekommt neue Informationen \
aus dieser Session. detail = nur das Neue.
- reveal: ein Eintrag aus „Geheime Einträge“ ist in dieser Session als Spielerwissen aufgetaucht – die Gruppe trifft \
den NSC, betritt den Ort, erfährt den Namen in der Szene. Kein reveal, wenn nur die Spielleitung den Namen nebenbei \
erwähnt (Regelerklärung, Planung, außerhalb des Spiels). detail = nur, was die Spieler jetzt wissen.
Wichtig:
- detail ist Spielerwissen: nur, was am Tisch gesagt oder erlebt wurde. Übernimm NIE Inhalte aus „gmNotes“ oder aus \
dem Text geheimer Einträge in detail – sie dienen dir nur zum Erkennen und Zuordnen.
- gmNotes nur bei create: Hintergrund, den die Spielleitung am Tisch preisgegeben hat, den die Figuren aber nicht \
kennen sollen. Sonst leer.
- Schreib detail und gmNotes als Aussagen über die Spielwelt, nie als Bericht über den Spieltisch. Die Spielleitung liest \
das selbst – also nie „die SL hat angedeutet/erwähnt/beschrieben, dass …“, „laut SL …“ oder „am Tisch wurde gesagt …“. \
Richtig: „Oren hilft Iria gegen Bezahlung bei der Flucht.“ Falsch: „Die SL hat angedeutet, dass Oren Iria hilft.“ \
War es nur eine Andeutung, schreib es als Spur in der Welt: „Oren scheint Iria zu kennen.“
- suggestedVisibility: public, wenn die Spieler es am Tisch erfahren haben; gm_only, wenn nur die Spielleitung davon \
gesprochen hat. visibilityReason: ein kurzer Satz.
- confidence zwischen 0 und 1. flags: joke_suspected (vermutlich Witz), low_confidence, contradicts_bible (widerspricht \
einem vorhandenen Eintrag).
- evidence: 1 bis 3 Belege {"start": "m:ss", "quote": wörtliches Zitat, höchstens 200 Zeichen}. start ist der \
Zeitstempel der Zeile bzw. Notiz in eckigen Klammern.
- Keine Einträge für die Charaktere der Spieler. Höchstens {max} Vorschläge, das Wichtigste zuerst. Lieber wenige gute.
- Wichtig ist, was die Kampagne weiterträgt: Nichtspielercharaktere mit Namen, Fraktionen, Aufträge und Ziele der \
Gruppe (quest), Gegenstände mit eigenem Namen, Orte, an die die Gruppe zurückkehren kann. Kulisse, die nur einmal \
vorbeizieht, kommt zuletzt oder gar nicht.
- detail und gmNotes sind reiner Text ohne Markdown (keine Sternchen, keine Rauten); mehrere Punkte als eigene Zeilen. \
Keine Vermutungen – nur, was gesagt wurde.
Antworte nur mit JSON: {"proposals": [{"entryType": "…", "action": "…", "targetEntryId": null, "title": "…", \
"detail": "…", "gmNotes": null, "suggestedVisibility": "…", "visibilityReason": "…", "confidence": 0.7, \
"flags": [], "evidence": [{"start": "m:ss", "quote": "…"}]}]}. Sprache der Texte: {sprache}."""


SYSTEM_PRUEFUNG = """Du prüfst den Recap einer Pen-&-Paper-Session gegen seine Grundlage (Transkript oder \
Szenennotizen; Zeitangaben stehen vorn in eckigen Klammern, z. B. „[12:34]“). Du schreibst nichts um, du bewertest nur.
Für jeden nummerierten Absatz:
- urteil: "belegt" (die Handlungen und Ergebnisse des Absatzes stehen in der Grundlage), "teilweise" (das Wesentliche \
ist belegt, eine einzelne Handlung, ein Name oder ein Ergebnis aber nicht), "unbelegt" (das Geschehen des Absatzes \
kommt in der Grundlage gar nicht vor), "widerspricht" (die Grundlage sagt ausdrücklich etwas anderes) oder "witz" \
(stammt aus einem Witz oder einem Gespräch außerhalb des Spiels).
- Erzählerische Ausschmückung (Gefühle, Aussehen, Gerüche, Wetter, Gedanken, Übergänge) ist erlaubt und kein Grund \
für "teilweise", "unbelegt" oder "widerspricht". Kommt etwas in der Grundlage nur nicht vor, ist das nie \
"widerspricht".
- Eine einzige Aussage, die der Grundlage widerspricht, macht den ganzen Absatz "widerspricht", auch wenn alles andere \
stimmt – nie "teilweise": eine Figur stirbt, obwohl sie überlebt (oder umgekehrt); die falsche Person rettet, gibt, \
bekommt oder verspricht etwas; zwei Figuren werden zu einer; ein Ereignis geschieht, das nur angedroht war.
- Prüfe jede Beziehung einzeln, nicht nur, ob die Wörter vorkommen: Wer tut was wem? Wer gibt wem was, und wer hat es \
danach? War jemand schon verletzt, oder wird er es durch die erzählte Handlung? Wer verspricht wem was, gegen welche \
Gegenleistung? Ist eine Figur in der Szene anwesend oder wird nur über sie gesprochen? Ist etwas beobachtet, behauptet, \
vermutet, geplant, erinnert oder eine Vision? Eine vertauschte Richtung („A gibt B“ statt „B gibt A“), ein Zustand, der \
umgedreht ist (verletzt → tot), eine Vermutung als Tatsache oder eine nur erwähnte Figur als anwesend: "widerspricht".
- stellen: 1 bis 3 Stellen der Grundlage, auf die sich der Absatz stützt – Zeitangabe "m:ss" und ein kurzes \
wörtliches Zitat daraus (höchstens 20 Wörter). Leer, wenn nichts belegt ist.
- begruendung: nur bei "unbelegt", "widerspricht" und "witz", sonst leer. Ein einziger kurzer Satz für die \
Spielleitung (höchstens 20 Wörter), der die Stelle konkret nennt, ohne Zitat und ohne Zeitangabe – etwa „Der Regen \
kommt in der Runde nicht vor.“ oder „In der Runde gibt Mara das Schwert ab, sie bekommt es nicht.“
Antworte nur mit JSON: {"absaetze": [{"nr": 1, "urteil": "…", "stellen": [{"zeit": "m:ss", "zitat": "…"}], \
"begruendung": "…"}]}. Sprache der Begründungen: {sprache}."""

SYSTEM_VOLLSTAENDIGKEIT = """Du vergleichst den Recap einer Pen-&-Paper-Session mit seiner Grundlage (Szenennotizen \
oder Transkript, Zeitangaben vorn in eckigen Klammern). Du bewertest nicht, ob der Recap richtig ist – du suchst nur, \
was FEHLT: wichtige Ereignisse der Grundlage, die im Recap nicht vorkommen.
Der einzige Maßstab: Würde das Weglassen einem Spieler vor der nächsten Runde Wissen nehmen über die Lage, den \
Zustand einer Figur (verletzt, gerettet, gefangen, befreit, tot), eine Beziehung, einen Besitz (wer gibt wem was), ein \
Ziel oder einen Auftrag, eine Verpflichtung (Versprechen, Schuld, Abmachung) oder eine offene Handlungsmöglichkeit? \
Nur dann ist ein Ereignis wichtig. Ausdrücklich nicht wichtig, solange daraus kein bleibender Zustand entsteht: \
Schlafplätze, Essen und Trinken, Regeln und Würfe, kleine Segnungen ohne Folge, einzelne gescheiterte Versuche, \
Beschreibungen ohne Folgen, Witze, Smalltalk, Nebenhandlungen ohne spätere Bedeutung.
Rang je Ereignis: "kritisch" (Wendepunkt, Tod, Rettung, Abmachung, entscheidende Information), "wichtig" (ändert \
Zustand, Besitz, Beziehung oder Ziel), "nebensächlich" (alles andere – nur nennen, wenn sonst nichts fehlt).
Höchstens {hoechstens} fehlende Ereignisse, die wichtigsten zuerst. Jedes mit dem Zeitstempel der Grundlage genau so, \
wie er dort steht, und der Notiz wörtlich oder fast wörtlich – nichts umformulieren, nichts hinzufügen, nichts \
erfinden. Fehlt nichts Wichtiges, antworte mit einer leeren Liste.
Antworte nur mit JSON: {"fehlend": [{"zeit": "m:ss", "wichtigkeit": "kritisch", "notiz": "…"}]}. Sprache: {sprache}."""

SYSTEM_ERGAENZUNG = """Du ergänzt den Recap einer Pen-&-Paper-Session um Ereignisse, die eine Prüfung als fehlend \
erkannt hat. Jedes fehlende Ereignis steht mit Zeitstempel und Notiz da, dazu die Ereignisse unmittelbar davor und \
danach („steht zwischen … und …“); die Absätze des Recaps sind nummeriert.
Füge jedes Ereignis genau in den Absatz ein, der die Ereignisse davor oder danach erzählt, an der Stelle dazwischen – \
ein bis zwei Sätze, im Ton des Recaps, Erzählstimme in der Vergangenheit, Figuren nach ihren Charakteren, die \
Spielleitung ist keine Figur. Erzählt kein Absatz die Nachbarn, lass das Ereignis weg. Streiche nichts, kürze nichts, \
ändere keine vorhandenen Aussagen, erfinde nichts über die Notiz hinaus. Gib nur die Absätze zurück, die du geändert \
hast, jeweils vollständig. Reiner Text ohne Markdown.
Antworte nur mit JSON: {"absaetze": [{"nr": 2, "text": "…"}]}. Sprache: {sprache}."""

# 0.4.65, Weg „Notizen zuerst“: erst auswählen, dann schreiben, dann gegen die Auswahl prüfen
SYSTEM_AUSWAHL = """Du bereitest das Kapitel zu einer langen Pen-&-Paper-Session vor. Du bekommst die Szenennotizen \
(mit Zeitstempeln) und den Stand am Ende der Runde. Wähle die Ereignisse aus, die im Kapitel auf keinen Fall fehlen \
dürfen – höchstens {hoechstens}, in der Reihenfolge des Geschehens.
Wichtig ist, was einem Spieler vor der nächsten Runde Wissen nehmen würde, wenn es fehlt oder falsch ist:
- Wer gefangen, verurteilt, befreit, gerettet (von wem), verletzt oder getötet wird – und wer überlebt.
- Wer wem was gibt, verspricht oder schuldet; Abmachungen mit ihren Bedingungen; Schwüre und Aufträge.
- Visionen und Botschaften mit ihrem Inhalt; bestehende Beziehungen, die etwas bewirken („kennt ihn seit der Kindheit“).
- Wendepunkte der Lage (Katastrophen, Flucht, Ankunft an einem neuen Ort) und entscheidende Informationen.
Nicht: Essen, Kleidung, Kulisse, Begrüßungen und Vorstellungen, Bestellungen, Fahrten ohne Folgen, einzelne \
gescheiterte Versuche, Regeln und Würfe, Witze, Smalltalk.{abschnitt}
Je Ereignis: zeit (Zeitstempel der Notiz), ereignis (ein Satz, mit Namen), ausgang (wie es ausgeht, genau wie am \
Tisch: verletzt ist nicht tot, angedroht ist nicht geschehen; widersprechen sich Notizen, gilt die spätere Notiz bzw. \
der Stand am Ende), rang ("kritisch" oder "wichtig"). Steht in den Notizen „(unklar, wer)“, bleibt die Person auch \
hier ungenannt, und „(unklar, wer)“ steht hinter dem Ausgang. Eine Zusage, eine Abmachung, ein Kauf oder ein Tod ist nur dann der Ausgang, wenn die Notizen ihn ausdrücklich nennen; sonst ist der Ausgang „offen“ (z. B. „Antwort offen, dann Angriff“). Decknamen sind keine eigenen Personen. Die Spielleitung ist keine Figur. Erfinde nichts.
Antworte nur mit JSON: {"ereignisse": [{"zeit": "m:ss", "ereignis": "…", "ausgang": "…", "rang": "…"}]}. \
Sprache: {sprache}."""
AUSWAHL_HOECHSTENS = 20
# 0.4.76: Die Auswahl über eine ganze lange Runde in einem Aufruf füllte ihr Kontingent in Zeitfolge und hörte auf – in
# zwei von sechs Messläufen hatte die letzte Stunde kein einziges Pflichtereignis. Darum wählt das Modell je
# Zeitabschnitt mit eigenem Anteil am Kontingent. 0.4.78: Jeder Abschnitt bekommt den Stand nur bis zu seinem Ende –
# mit dem Stand am Ende der Runde wanderten Beträge und Abmachungen in Ereignisse Stunden davor (gemessen: der erst
# nach fünf Stunden vereinbarte Preis schon bei einer Anwerbung drei Stunden davor, in zwei von drei Läufen).
AUSWAHL_ABSCHNITT_MIN = 45  # Minuten je Abschnitt, etwa
AUSWAHL_ABSCHNITT_AB = 60  # erst Runden über so vielen Minuten werden geteilt
AUSWAHL_ABSCHNITT = """
Du bekommst nur Abschnitt {k} von {n} der Runde ({von} bis {bis}). Wähle nur Ereignisse aus diesem Abschnitt; die \
übrigen Abschnitte wählt jemand anders. Der Stand bis zum Ende dieses Abschnitts hilft dir, Ausgänge richtig zu \
benennen; was erst später vereinbart oder bekannt wird, gehört nicht in diesen Abschnitt."""


def auswahl_hoechstens(ein: dict) -> int:
    """0.4.73: Zahl der Pflichtereignisse nach Dauer der Runde – 20 für 155 Minuten waren zu knapp (Kritisches fiel
    schon aus der Auswahl). 0.4.78: ab 3 h je angefangene Stunde fünf mehr, höchstens 45."""
    return auswahl_hoechstens_fuer(_minuten(ein))


def auswahl_hoechstens_fuer(minuten: float) -> int:
    if minuten > 300:
        return 45
    if minuten > 240:
        return 40
    if minuten > 180:
        return 35
    return 30 if minuten > 120 else AUSWAHL_HOECHSTENS if minuten > 60 else 12


_STAND_ZEIT = re.compile(r"\[(\d{1,2}(?::\d{2}){1,2})\]")


def stand_bis(stand: list[str], bis_s: float) -> list[str]:
    """0.4.78: Der Stand, wie er bis zu einem Zeitpunkt galt. Jede Angabe trägt den Zeitpunkt ihrer Änderung („… (seit
    [4:54:25])“); Teile einer Zeile (durch „;“ getrennt), deren Zeitpunkt danach liegt, fallen weg. Angaben ohne
    Zeitpunkt bleiben – über sie lässt sich nichts sagen."""
    aus = []
    for zeile in stand or []:
        teile = []
        for teil in str(zeile).split(";"):
            zeiten = [zeit_lesen(z) for z in _STAND_ZEIT.findall(teil)]
            if any(z is not None and z > bis_s + 60 for z in zeiten):
                continue
            teile.append(teil.strip())
        rest = "; ".join(t for t in teile if t)
        if rest and not re.fullmatch(r"[^:]{1,60}:", rest):  # nur noch „Name:“ übrig – weglassen
            aus.append(rest)
    return aus


# 0.4.78: Teile im Kapitel. Eine lange Runde bleibt ein Kapitel; findet sich ein natürlicher Einschnitt (Zeitsprung,
# Orts- und Szenenwechsel, Ende eines Erzählstrangs, Pause), bekommt jeder Teil eine Überschrift, eigene
# Pflichtereignisse und eigenen Platz (nur, wenn es inhaltlich Sinn ergibt).
# Kein Einschnitt → keine Teile. Der Server prüft Lage, Mindestdauer und das Zitat aus den Notizen.
TEILE_AB_MIN = 240  # ab so vielen Minuten Spielzeit wird ein Einschnitt gesucht
TEILE_ZWEI_AB_MIN = 420  # ab so vielen Minuten sind zwei Einschnitte möglich
TEILE_FENSTER = (0.3, 0.7)  # Einschnitte nur in diesem Anteil der Runde
TEIL_MIN_MIN = 60  # jeder Teil dauert mindestens so viele Minuten
EINSCHNITT_NAEHE_S = 600.0  # die zitierte Notiz liegt höchstens so weit vom genannten Zeitpunkt
_EINSCHNITT_ARTEN = {"zeitsprung": "zeitsprung", "time_skip": "zeitsprung", "ortswechsel": "ortswechsel",
                     "scene_change": "ortswechsel", "strangende": "strangende", "end_of_thread": "strangende",
                     "pause": "pause", "break": "pause"}
SYSTEM_EINSCHNITT = """Du gliederst die Szenennotizen einer langen Pen-&-Paper-Runde. Gesucht ist ein natürlicher \
Einschnitt, an dem die Geschichte selbst einen Schnitt macht – nur zwischen {von} und {bis}, höchstens {anzahl}:
- ein Zeitsprung in der Spielwelt („am nächsten Abend“, „eine Woche später“),
- ein Wechsel von Ort und Szene, nach dem etwas Neues beginnt,
- das Ende eines Erzählstrangs, oder eine lange Pause am Tisch.
Kein Einschnitt mitten in einer Szene, einem Kampf oder einem Gespräch. Gibt es keinen klaren Einschnitt, liefere eine \
leere Liste – lieber keiner als ein erzwungener.
Je Einschnitt: zeit (Zeitstempel der ersten Notiz nach dem Einschnitt), art ("zeitsprung", "ortswechsel", "strangende" \
oder "pause"), zitat (diese Notiz, wörtlich, höchstens 20 Wörter), titel (Überschrift für den Teil ab hier, 2 bis 5 \
Wörter, ohne Nummer). Dazu titel_anfang: die Überschrift für den Teil davor. Überschriften im Ton der Kampagne, ohne \
Namen der Menschen am Tisch, keine Regeln, nichts erfinden.
Antworte nur mit JSON: {"titel_anfang": "…", "einschnitte": [{"zeit": "h:mm:ss", "art": "…", "zitat": "…", \
"titel": "…"}]}. Sprache: {sprache}."""


PFLICHT_VORSPANN = ("\n\nDiese Ereignisse müssen im Kapitel vorkommen, jedes mit genau diesem Ausgang, in dieser "
                    "Reihenfolge; sie wurden an der Abschrift geprüft – widerspricht ihnen eine Notiz, gilt das Ereignis. "
                    "Kleinigkeiten ohne Folgen (Essen, Kleidung, einzelne Versuche) höchstens in einem Halbsatz:\n")
TEIL_ANFANG = """
- Dies ist Teil {k} von {n} eines Kapitels; die übrigen Teile schreibt jemand anders. Erzähle nur diesen Teil, ohne \
Vorgriff auf Späteres."""
TEIL_FORTSETZUNG = """
- Dies ist Teil {k} von {n} desselben Kapitels und setzt den Teil davor nahtlos fort: keine Einleitung, keine \
Beschreibung der Welt, keine Vorstellung der Figuren, kein Rückblick auf früher Erzähltes. Die Überschrift des Teils \
setzt jemand anders; „title“ gibst du trotzdem an."""
TEIL_OHNE_SCHLUSS = """
- Kein Schlusswort für den ganzen Abend – die Runde geht im nächsten Teil weiter."""


TEIL_DAUER_VORGABE = 150  # Minuten: jeder Teil bekommt Platz und Pflichtereignisse wie eine Runde dieser Länge


def teil_grenzen(teil: dict, lang: bool = True) -> tuple[int, int]:
    """Platz eines Teils (Cloud 900–1500, lokal 600–1200) – ein Teil liest sich wie ein normaler Abend."""
    w = woerter_fuer(TEIL_DAUER_VORGABE, lang).split("–")
    return int(w[0]), int(w[-1])


def teil_hoechstens(teil: dict) -> int:
    """Pflichtereignisse eines Teils: wie bei einem normalen Abend (30)."""
    return auswahl_hoechstens_fuer(TEIL_DAUER_VORGABE)


def _ueberschrift(t) -> str:
    t = regelreste(klartext(t if isinstance(t, str) else ""))
    t = re.sub(r"^(?:Teil|Part|Kapitel|Chapter)\s+\d+\s*[:.–-]\s*", "", t, flags=re.I).strip(" „“\"'.")
    return t[:80] if 0 < len(t.split()) <= 8 else ""


def einschnitte_lesen(d: dict, notizen: str, dauer_s: float, hoechstens: int, sprache: str = "de") -> list[dict]:
    """Antwort der Suche nach Einschnitten → Teile [{von, bis, titel}] (Sekunden) oder [] (keine Teile). Ein Einschnitt
    zählt nur, wenn er im Fenster liegt, die Teile lang genug bleiben und das Zitat als Notiz nahe dem Zeitpunkt steht;
    der Schnitt fällt auf die erste Notiz ab diesem Zeitpunkt."""
    zeilen = [(_zeit_vorn(z), _normwoerter(z)) for z in (notizen or "").splitlines() if z.strip()]
    zeilen = [(t, n) for t, n in zeilen if t is not None]
    unten, oben = dauer_s * TEILE_FENSTER[0], dauer_s * TEILE_FENSTER[1]
    kandidaten = []
    for e in d.get("einschnitte") or d.get("cuts") or []:
        if not isinstance(e, dict):
            continue
        zeit = zeit_lesen(e.get("zeit") if e.get("zeit") is not None else e.get("time"))
        art = _EINSCHNITT_ARTEN.get(str(e.get("art") or e.get("kind") or "").strip().lower())
        zitat = klartext(e.get("zitat") or e.get("quote") or "") if isinstance(e.get("zitat") or e.get("quote"), str) else ""
        if zeit is None or not art or not zitat or not (unten <= zeit <= oben):
            continue
        belegt = [t for t, n in zeilen if zitat_belegt(zitat, n) and abs(t - zeit) <= EINSCHNITT_NAEHE_S]
        if not belegt:
            continue
        schnitt = next((t for t, _n in zeilen if t >= min(belegt)), None)
        if schnitt is None:
            continue
        kandidaten.append((schnitt, _ueberschrift(e.get("titel") or e.get("title"))))
    kandidaten.sort()
    gewaehlt: list[tuple[float, str]] = []
    for schnitt, titel in kandidaten:
        if len(gewaehlt) >= hoechstens:
            break
        davor = gewaehlt[-1][0] if gewaehlt else 0.0
        if schnitt - davor >= TEIL_MIN_MIN * 60 and dauer_s - schnitt >= TEIL_MIN_MIN * 60:
            gewaehlt.append((schnitt, titel))
    if not gewaehlt:
        return []
    ersatz = "Part {n}" if sprache == "en" else "Teil {n}"
    titel = [_ueberschrift(d.get("titel_anfang") or d.get("first_title"))] + [t for _s, t in gewaehlt]
    grenzen = [0.0] + [s for s, _t in gewaehlt] + [dauer_s]
    return [{"von": grenzen[i], "bis": grenzen[i + 1], "titel": titel[i] or ersatz.format(n=i + 1)}
            for i in range(len(titel))]


def teil_notizen(notizen: str, von: float, bis: float) -> str:
    """Die Notizen eines Teils (Zeitstempel ab von, vor bis); Zeilen ohne Zeitstempel bleiben bei der Zeile davor."""
    aus, drin = [], False
    for z in (notizen or "").splitlines():
        if not z.strip():
            continue
        t = _zeit_vorn(z)
        if t is not None:
            drin = von <= t < bis
        if drin:
            aus.append(z)
    return "\n".join(aus)


def teile_verorten(text: str, anfaenge: list[str], titel: list[str]) -> list[dict]:
    """Nach Nachbesserung, Kürzung und Filter: In welchem Absatz beginnt jeder Teil? anfaenge = erster Absatz jedes
    Teils, wie er geschrieben wurde. Gesucht wird der Anfang (sechs Wörter), sonst der Absatz mit den meisten gemeinsamen
    Wortstämmen. Lässt sich ein Teil nicht wiederfinden, gibt es keine Teile – lieber ohne Überschrift als an falscher
    Stelle. Liefert [{title, firstParagraph}]."""
    teile = absaetze(text)
    if len(anfaenge) < 2 or len(anfaenge) != len(titel) or len(teile) < len(anfaenge):
        return []
    norm = [_normwoerter(t) for t in teile]
    aus = [{"title": titel[0], "firstParagraph": 0}]
    letzter = 0
    for anfang, t in zip(anfaenge[1:], titel[1:]):
        w = re.findall(r"\w+", anfang.casefold())[:6]
        kopf = " " + " ".join(w) + " "
        wo = next((i for i in range(letzter + 1, len(teile)) if norm[i].startswith(kopf)), None)
        if wo is None:  # ein ganzer Satz des Anfangs steht noch da (der Filter hat davor etwas gestrichen)
            saetze = [_normwoerter(x) for x in _SATZENDE.split(anfang)[:3] if len(x.split()) >= 5]
            wo = next((i for i in range(letzter + 1, len(teile)) if any(x in norm[i] for x in saetze)), None)
        if wo is None:
            s = _staemme(anfang)
            beste = max(((len(s & _staemme(teile[i])) / len(s), -i) for i in range(letzter + 1, len(teile))),
                        default=(0.0, None)) if s else (0.0, None)
            wo = -beste[1] if beste[0] >= 0.5 else None
        if wo is None:
            return []
        aus.append({"title": t, "firstParagraph": wo})
        letzter = wo
    return aus


def auswahl_abschnitte(notizen: str, minuten: float, hoechstens: int, start: float = 0.0) -> list[dict]:
    """0.4.76: Notizen in gleich lange Zeitabschnitte (etwa AUSWAHL_ABSCHNITT_MIN Minuten) mit je einem Anteil am
    Kontingent. Zeilen ohne Zeitstempel bleiben beim Abschnitt davor; leere Abschnitte fallen weg, ihr Anteil geht an
    die übrigen. Kurze Runden: ein Abschnitt. start (0.4.78): Beginn in Sekunden, wenn nur ein Teil der Runde kommt."""
    zeilen = [z for z in (notizen or "").splitlines() if z.strip()]
    if minuten <= AUSWAHL_ABSCHNITT_AB:
        return [{"k": 1, "n": 1, "von": start, "bis": start + minuten * 60, "notizen": "\n".join(zeilen),
                 "anteil": hoechstens}]
    n = max(2, math.ceil(minuten / AUSWAHL_ABSCHNITT_MIN))
    breite = minuten * 60 / n
    stuecke: list[list[str]] = [[] for _ in range(n)]
    k = 0
    for z in zeilen:
        t = _zeit_vorn(z)
        if t is not None:
            k = min(n - 1, max(0, int((t - start) // breite)))
        stuecke[k].append(z)
    belegt = [i for i in range(n) if stuecke[i]]
    if not belegt:
        return []
    anteile = {i: hoechstens // len(belegt) for i in belegt}
    for i in belegt[:hoechstens % len(belegt)]:
        anteile[i] += 1
    return [{"k": j + 1, "n": len(belegt), "von": start + i * breite, "bis": start + (i + 1) * breite,
             "notizen": "\n".join(stuecke[i]),
             "anteil": anteile[i]} for j, i in enumerate(belegt) if anteile[i] > 0]

SYSTEM_PFLICHT = """Du vergleichst das Kapitel einer Pen-&-Paper-Session mit einer Liste von Ereignissen, die darin \
vorkommen müssen. Für jedes Ereignis: status "erzaehlt" (kommt mit diesem Ausgang vor), "fehlt" (kommt nicht vor, oder \
nur das Ereignis, nicht sein Ausgang) oder "widerspricht" (kommt vor, aber mit anderem Ausgang, anderer Person oder \
vertauschter Richtung). Abweichende Zahlen, Beträge, Uhrzeiten oder Formulierungen, die am Ausgang nichts ändern, sind \
kein Widerspruch. zitat: bei "erzaehlt" die Wörter des Kapitels, die den Ausgang erzählen – wörtlich abgeschrieben, \
höchstens 25 Wörter; findest du keine, ist der Status "fehlt". Bei "widerspricht" die Wörter des Kapitels, die dem \
Ausgang widersprechen, ebenfalls wörtlich. absatz: die Nummer \
des Absatzes, in dem es steht bzw. in den es nach der Reihenfolge des Geschehens gehört. begruendung: bei "fehlt" und \
"widerspricht" ein kurzer Satz für die Spielleitung, ohne Zitat, sonst leer. Bewerte nur die Liste, nicht den Stil.
Antworte nur mit JSON: {"punkte": [{"nr": 1, "status": "…", "absatz": 2, "zitat": "…", "begruendung": "…"}]}. \
Sprache: {sprache}."""

SYSTEM_PFLICHT_NACH = """Du überarbeitest einzelne Absätze im Kapitel einer Pen-&-Paper-Session. Je Absatz steht \
dabei, welches Ereignis darin fehlt oder falsch erzählt ist, mit dem richtigen Ausgang.
Schreib nur diese Absätze neu: Fehlendes knapp an der passenden Stelle einfügen, Falsches richtigstellen – die falsche Aussage wird ersetzt, nicht zusätzlich stehen gelassen. Alles andere \
im Absatz bleibt, wie es ist. Steht das Ereignis mit genau diesem Ausgang doch schon im Absatz, gib den Absatz \
unverändert zurück. Gleicher Ton, Vergangenheit, Figuren nach ihren Charakteren, ein Absatz ohne Leerzeilen, \
reiner Text ohne Markdown. Keine Regeln, Würfe oder Punkte; die Spielleitung ist keine Figur. Erfinde nichts.
Antworte nur mit JSON: {"absaetze": [{"nr": 2, "text": "…"}]}. Sprache: {sprache}."""
# 0.4.74, Weg „Notizen zuerst“: Zweiter Blick in die Abschrift. Die schweren Fehler entstanden beim Zuhören an
# mehrdeutigen oder verstümmelten Stellen (Anrede ohne Namen, Würfelgespräch, namenlose Person); jede spätere Prüfung las
# nur die Notizen und damit denselben Fehler. Darum werden die kritischen und die unklaren Ereignisse der Auswahl vor dem
# Schreiben noch einmal gegen den Wortlaut der Abschrift rund um ihren Zeitpunkt gehalten. Berichtigt wird nur mit einem
# Zitat, das der Server in der Abschrift wiederfindet; sonst bleibt es „unklar“ und das Kapitel dort vage.
SYSTEM_ZWEITER_BLICK = """Du prüfst einzelne Ereignisse einer Pen-&-Paper-Session gegen den Wortlaut der Abschrift \
(automatisch erkannt, mit Fehlern). Die Spielleitung spricht alle Nebenfiguren und redet die Spieler oft mit „du“ an – \
„du“ meint dann die Figur des Spielers, der gerade gehandelt hat oder angesprochen wird; Spieler sprechen von ihrer \
Figur in der Ich-Form. Zu jedem Ereignis steht der Ausschnitt der Abschrift rund um seinen Zeitpunkt.
Für jedes Ereignis: urteil "stimmt" (Ereignis und Ausgang stehen so im Wortlaut, mit denselben Personen in derselben \
Rolle), "berichtigt" (der Wortlaut sagt etwas anderes: andere Person, andere Richtung, anderer Ausgang, Person ohne \
Namen) oder "unklar" (der Wortlaut lässt mehrere Lesarten zu oder enthält die Stelle nicht).
Bei "berichtigt": ereignis und ausgang neu – ein Satz, nur was der Wortlaut hergibt, nichts erfinden; eine Person, die \
im Wortlaut keinen Namen hat, bleibt ohne Namen – und zitat: die Zeile oder Zeilen der Abschrift, die es belegen, \
wörtlich abgeschrieben, höchstens 30 Wörter. Bei "unklar": grund in einem kurzen Satz (was offen bleibt). Rate nie; im \
Zweifel "unklar". Keine Regeln, Würfe oder Punkte.
Antworte nur mit JSON: {"ereignisse": [{"nr": 1, "urteil": "…", "ereignis": "…", "ausgang": "…", "zitat": "…", \
"grund": "…"}]}. Sprache: {sprache}."""
# 0.4.76: Der Ausgang kommt oft zwei, drei Minuten nach dem Ereignis („Schön, schön …“, dann das Messer) – das Fenster
# reicht darum weiter nach vorn als zurück (vorher ± 90 s; ein Fehler wurde so 56 s hinter dem Fenster übersehen).
ZWEITER_BLICK_DAVOR_S = 60.0  # Abschrift so viele Sekunden vor dem Zeitpunkt des Ereignisses …
ZWEITER_BLICK_DANACH_S = 240.0  # … und so viele danach
ZWEITER_BLICK_HOECHSTENS = 12  # so viele Ereignisse je Kapitel (alle unklaren, dann die kritischen)
ZWEITER_BLICK_ZEICHEN = 6000  # höchstens so viel Abschrift je Ereignis
ZWEITER_BLICK_JE_AUFRUF = 6  # mehr Ereignisse: mehrere Aufrufe, damit die Antwort nicht abbricht
_URTEIL_BLICK = {"stimmt": "stimmt", "ok": "stimmt", "correct": "stimmt", "confirmed": "stimmt", "berichtigt": "berichtigt",
                 "korrigiert": "berichtigt", "corrected": "berichtigt", "unklar": "unklar", "unclear": "unklar"}


def _fenster(zeilen: list[tuple[float, str]], zeit: float, davor: float = ZWEITER_BLICK_DAVOR_S,
             danach: float = ZWEITER_BLICK_DANACH_S, zeichen: int = ZWEITER_BLICK_ZEICHEN) -> str:
    """Zeilen der Abschrift von `davor` Sekunden vor bis `danach` Sekunden nach einem Zeitpunkt; zu viel Text wird vom
    Zeitpunkt her begrenzt (Abstand jeweils im Verhältnis zur Länge der Seite)."""
    nah = [((zeit - t) / davor if t < zeit else (t - zeit) / danach, i) for i, (t, _z) in enumerate(zeilen)
           if -davor <= t - zeit <= danach]
    if not nah:
        return ""
    gewaehlt, summe = set(), 0
    for _d, i in sorted(nah):
        if summe + len(zeilen[i][1]) + 1 > zeichen and gewaehlt:
            break
        gewaehlt.add(i)
        summe += len(zeilen[i][1]) + 1
    return "\n".join(zeilen[i][1] for i in sorted(gewaehlt))


# 0.4.68, Weg „Notizen zuerst“: Ein Kapitel weit über der Längenvorgabe wird gekürzt – aber nur übernommen, wenn
# danach jedes Pflichtereignis noch erzählt ist, kein neuer Widerspruch entsteht und kein Name fehlt. Sonst bleibt die
# lange Fassung: gekürzt wird nur, wenn keine Information verloren geht.
SYSTEM_KUERZEN = """Du kürzt das Kapitel einer Pen-&-Paper-Session auf {unten} bis {ziel} Wörter (nicht weniger), \
damit es vorlesbar bleibt. Streiche nur Ausschmückung: Kulisse, Kleidung, Essen und Getränke, Wege von A nach B, wörtliche Rede, die \
nichts Neues sagt, Wiederholungen. Alles andere bleibt: jede Handlung mit ihrem Ausgang und mit den richtigen Personen, \
jeder Name, jeder benannte Gegenstand, jede Abmachung und jede Information, in derselben Reihenfolge. Die Ereignisse \
der Liste kommen alle mit genau diesem Ausgang vor. Verändere keine Aussage, füge nichts hinzu. Gleicher Ton, \
Vergangenheit, Absätze durch Leerzeilen getrennt, reiner Text ohne Markdown.
Antworte nur mit JSON: {"text": "…"}. Sprache: {sprache}."""
KUERZEN_AB = 1.2  # erst ab 20 % über der oberen Grenze kürzen
KUERZEN_MINDESTENS = 0.1  # weniger als 10 % gespart: lohnt nicht, lange Fassung bleibt
_NAMENSFOLGE = re.compile(r"(?<=[a-zäöüß,;:–] )[A-ZÄÖÜ][\w’'-]+(?: [A-ZÄÖÜ][\w’'-]+)+")


def untergrenze(ein: dict, lang: bool = False) -> int:
    """Untere Grenze der Längenvorgabe in Wörtern („900–1500“ → 900)."""
    return int(woerter(ein, lang).split("–")[0])


def obergrenze(ein: dict, lang: bool = False) -> int:
    """Obere Grenze der Längenvorgabe in Wörtern („900–1500“ → 1500)."""
    return int(woerter(ein, lang).split("–")[-1])


def namen_im_text(text: str, extra: list[str] | tuple = ()) -> set[str]:
    """Eigennamen eines Kapitels als Wortstämme: Folgen großgeschriebener Wörter mitten im Satz („Du Yuesheng“,
    „Madame Xue“, „Richard Wilhelm“) und bekannte Namen (Figuren, Bibel), soweit sie im Text stehen."""
    aus = set()
    for folge in _NAMENSFOLGE.findall(text):
        aus.update(w.casefold()[:5] for w in folge.split() if len(w) >= 3)
    klein = text.casefold()
    for name in extra:
        for w in re.findall(r"\w+", name or ""):
            if len(w) >= 3 and re.search(rf"\b{re.escape(w.casefold())}", klein):
                aus.add(w.casefold()[:5])
    return aus


def _namen_fehlen(lang: str, kurz: str, extra: list[str], pflicht: str | None = None) -> list[str]:
    """Namen des langen Textes, die im kurzen fehlen. pflicht (0.4.76): nur Namen zählen, die auch dort stehen (die
    Pflichtereignisse) oder bekannte Namen sind – sonst blockierte jede Kulisse („Bar“, „Lady Luck“, „Brot“) das Kürzen."""
    kurz_klein = kurz.casefold()
    namen = namen_im_text(lang, extra)
    if pflicht is not None:
        bekannt = {w.casefold()[:5] for name in extra for w in re.findall(r"\w+", name or "") if len(w) >= 3}
        pflicht_klein = pflicht.casefold()
        namen = {n for n in namen if n in bekannt or re.search(rf"\b{re.escape(n)}", pflicht_klein)}
    return sorted(n for n in namen if not re.search(rf"\b{re.escape(n)}", kurz_klein))


# 0.4.69: Korrektur per Hinweis der Spielleitung (vorerst nur zum Messen im Probelauf). Nur genannte Absätze ändern
# sich; die SL hat immer recht.
SYSTEM_KORREKTUR = """Du überarbeitest das Kapitel einer Pen-&-Paper-Session nach Hinweisen der Spielleitung. Die \
Spielleitung war dabei und hat immer recht: Was in einem Hinweis steht, gilt, auch wenn die Notizen etwas anderes sagen.
- Ein Hinweis zu einem Absatz: Schreib genau diesen Absatz neu. Stell die beanstandete Stelle richtig oder streich \
sie; alles andere im Absatz bleibt, wie es ist.
- Hinweise ohne Absatzangabe (H1, H2 …; Fehler oder Fehlendes): Ordne jeden selbst dem Absatz zu, den er betrifft – \
Fehlendes dem Absatz, in den es nach der Reihenfolge des Geschehens gehört – und ändere nur diese Absätze. Fehlendes \
knapp einfügen, der Rest des Absatzes bleibt. Steht etwas Falsches schon im Absatz, ersetze es, statt das Richtige \
nur dazuzuschreiben.
- Ein Hinweis ist eine Anweisung, kein Text zum Abschreiben: Ins Kapitel kommt nur, was die Spielleitung darin sehen \
will, ohne Wörter wie „Hinweis“ oder „Spielleitung“. Nichts darüber hinaus erfinden.
Gleicher Ton, Vergangenheit, Figuren nach ihren Charakteren, ein Absatz ohne Leerzeilen, reiner Text ohne Markdown. \
Keine Regeln, Würfe oder Punkte.
Antworte nur mit JSON: {"absaetze": [{"nr": 2, "text": "…", "hinweise": [1, 3]}]} – nur die geänderten Absätze; \
"hinweise" nennt die Nummern der H-Hinweise, die du in diesem Absatz umgesetzt hast. Sprache: {sprache}."""
KORREKTUR_NOTIZEN_ZEICHEN = 60_000  # Notizen als Hintergrund, gekürzt
KORREKTUR_MINDESTENS = 0.3  # ein Absatz darf beim Richtigstellen nicht auf weniger als 30 % schrumpfen
KORREKTUR_FREI_HOECHSTENS = 5  # so viele Absätze darf ein Hinweistext ohne Absatzangabe höchstens ändern
_META = re.compile(r"\b(?:Hinweis|Spielleitung|Spielleiter|game master|hint)\b", re.I)


def hinweis_liste(frei: str) -> list[str]:
    """Ein freier Hinweistext → einzelne Hinweise, ein Satz bzw. eine Zeile je Hinweis („Mr. Du“ trennt nicht)."""
    from app.artefakte import _saetze

    aus = []
    for zeile in (frei or "").splitlines():
        aus += [satz for satz, _m in _saetze(" ".join(zeile.split())) if len(satz.split()) >= 2]
    return aus


def hinweis_saetze(frei: str) -> int:
    """Wie viele Hinweise stecken in einem freien Text?"""
    return len(hinweis_liste(frei))


def satz_aenderung(alt: str, neu: str) -> dict:
    """Welche Sätze ein Korrekturdurchgang in einem Absatz neu geschrieben bzw. gestrichen hat (zum Abhaken)."""
    from app.artefakte import _saetze

    a, n = [x for x, _m in _saetze(alt)], [x for x, _m in _saetze(neu)]
    return {"neu": [x for x in n if x not in a], "weg": [x for x in a if x not in n]}


def pruefliste(hinweise: dict[int, str], frei: list[str], zuordnung: dict[int, list[int]], alt: list[str],
               neu: dict[int, str]) -> list[dict]:
    """Je Hinweis: Text, Absätze (1-basiert), in denen er umgesetzt wurde, und die geänderten Sätze dort. Ohne Absatz
    gilt er als nicht umgesetzt. zuordnung: {Nummer des freien Hinweises (1-basiert): [Absatz-Index]}."""
    def eintrag(text: str, idx: list[int]) -> dict:
        idx = sorted(i for i in set(idx) if i in neu)
        return {"text": text, "umgesetzt": bool(idx),
                "absaetze": [{"absatz": i + 1, **satz_aenderung(alt[i], neu[i])} for i in idx]}

    return ([eintrag(h, [i]) for i, h in sorted(hinweise.items())]
            + [eintrag(h, zuordnung.get(n, [])) for n, h in enumerate(frei, 1)])


def hinweis_zuordnung(d: dict, anzahl: int) -> dict[int, list[int]]:
    """Aus der Modellantwort: welcher freie Hinweis (1-basiert) in welchem Absatz (Index) umgesetzt wurde."""
    aus: dict[int, list[int]] = {}
    for a in d.get("absaetze") or d.get("paragraphs") or []:
        if not isinstance(a, dict) or not isinstance(a.get("hinweise"), list):
            continue
        try:
            i = int(a.get("nr") or a.get("index") or 0) - 1
        except (TypeError, ValueError):
            continue
        for h in a["hinweise"]:
            try:
                n = int(str(h).lstrip("Hh"))
            except ValueError:
                continue
            if 1 <= n <= anzahl:
                aus.setdefault(n, []).append(i)
    return aus


def korrektur_anwenden(d: dict, teile: list[str], hinweise: dict[int, str], frei: str) -> tuple[dict[int, str], list[dict]]:
    """Antwort des Modells prüfen, ohne Modell: Absätze mit Hinweis dürfen sich ändern; ein Hinweistext ohne
    Absatzangabe (0.4.70, ein Feld wie in der App geplant) höchstens so viele weitere, wie er Sätze hat (bis
    KORREKTUR_FREI_HOECHSTENS). Ein Absatz darf nicht ausgehöhlt werden, und kein Hinweis darf als Wortlaut („laut
    Hinweis …“) ins Kapitel. Liefert ({Index: neuer Text}, [verworfen mit Grund])."""
    neu: dict[int, str] = {}
    verworfen: list[dict] = []
    frei_rest = min(max(hinweis_saetze(frei), 1), KORREKTUR_FREI_HOECHSTENS) if frei.strip() else 0
    for a in d.get("absaetze") or d.get("paragraphs") or []:
        if not isinstance(a, dict):
            continue
        try:
            i = int(a.get("nr") or a.get("index") or 0) - 1
        except (TypeError, ValueError):
            continue
        t = " ".join(klartext(a.get("text")).split()) if isinstance(a.get("text"), str) else ""
        if not (0 <= i < len(teile)) or not t or i in neu or t == teile[i]:
            continue
        if i not in hinweise and frei_rest <= 0:
            verworfen.append({"absatz": i + 1, "grund": "ohne Hinweis geändert"})
            continue
        if len(t) < len(teile[i]) * KORREKTUR_MINDESTENS:
            verworfen.append({"absatz": i + 1, "grund": "zu stark gekürzt"})
            continue
        if _META.search(t) and not _META.search(teile[i]):
            verworfen.append({"absatz": i + 1, "grund": "Hinweis abgeschrieben"})
            continue
        if i not in hinweise:
            frei_rest -= 1
        neu[i] = t
    return neu, verworfen


PFLICHT_STATUS = {"erzaehlt": "erzaehlt", "erzählt": "erzaehlt", "told": "erzaehlt", "present": "erzaehlt",
                  "fehlt": "fehlt", "missing": "fehlt", "widerspricht": "widerspricht", "contradicts": "widerspricht",
                  "contradicted": "widerspricht", "unklar": "unklar", "unclear": "unklar"}
OHNE_ZITAT = {"de": "Kein Satz im Kapitel erzählt diesen Ausgang.", "en": "No sentence in the chapter tells this outcome."}


def auswahl_lesen(d: dict, hoechstens: int = AUSWAHL_HOECHSTENS) -> list[dict]:
    """Antwort der Auswahl → [{zeit, ereignis, ausgang, rang}], höchstens `hoechstens`, ohne Leeres, Doppeltes und
    Regelreste, nach Zeit sortiert."""
    aus, gesehen = [], set()
    for e in d.get("ereignisse") or d.get("events") or []:
        if not isinstance(e, dict):
            continue
        ereignis = regelreste(klartext(e.get("ereignis") or e.get("event") or ""))[:300]
        if not ereignis or ereignis.casefold() in gesehen or _SPIELLEITUNG.search(ereignis):
            continue
        gesehen.add(ereignis.casefold())
        rang = str(e.get("rang") or e.get("rank") or "").strip().lower()
        aus.append({"zeit": zeit_lesen(e.get("zeit") if e.get("zeit") is not None else e.get("time")),
                    "ereignis": ereignis, "ausgang": regelreste(klartext(e.get("ausgang") or e.get("outcome") or ""))[:300],
                    "rang": "kritisch" if rang in ("kritisch", "critical") else "wichtig"})
    if len(aus) > hoechstens:  # 0.4.76: zu viele – Kritisches bleibt, dann Wichtiges in der Reihenfolge des Modells
        kritisch = [e for e in aus if e["rang"] == "kritisch"][:hoechstens]
        wichtig = [e for e in aus if e["rang"] != "kritisch"][:hoechstens - len(kritisch)]
        aus = kritisch + wichtig
    return sorted(aus, key=lambda e: (e["zeit"] is None, e["zeit"] or 0.0))


def auswahl_text(punkte: list[dict]) -> str:
    return "\n".join(f"{i + 1}. [{_zeit(p['zeit']) if p['zeit'] is not None else '?'}] {p['ereignis']}"
                     + (f" – Ausgang: {p['ausgang']}" if p["ausgang"] else "") for i, p in enumerate(punkte))


_STAMM_FUELL = {"wird", "wurde", "werden", "nach", "über", "eine", "einen", "einem", "einer", "sich", "auch", "dann",
                "dass", "noch", "doch", "aber", "sind", "sein", "seine", "seinen", "ihre", "ihren", "nicht", "mehr", "will",
                "hatte", "hatten", "haben", "gegen", "beim", "vom", "zum", "zur", "unter", "durch", "ausgang", "offen"}


def _staemme(text: str) -> set[str]:
    """Wortstämme (erste fünf Buchstaben) der Wörter ab vier Buchstaben, ohne Füllwörter – zum Wiederfinden eines
    Ereignisses in einem Absatz, auch wenn das Kapitel anders formuliert."""
    return {w[:5] for w in re.findall(r"\w+", (text or "").casefold()) if len(w) >= 4 and w not in _STAMM_FUELL}


WIDERSPRUCH_STAEMME = 2  # 0.4.76: so viele Stämme des Ereignisses muss der Absatz enthalten, wenn das Zitat fehlt


def pflicht_lesen(d: dict, anzahl_punkte: int, anzahl_absaetze: int, text: str = "", sprache: str = "de",
                  punkte: list[dict] | None = None) -> list[dict]:
    """Antwort der Pflichtprüfung → je Punkt {nr, status, absatz (0-basiert oder None), zitat, begruendung}. Fehlende
    Punkte gelten als ungeprüft und lösen nichts aus.

    0.4.73: Jedes „erzählt“ braucht ein Zitat aus dem Kapitel, das der Server nachrechnet (die Prüfung hatte „Jeremy ging
    über Bord“ als „verliert das Silber“ durchgehen lassen). Ohne auffindbares Zitat gilt der Punkt als „fehlt“; ein
    „widerspricht“ ohne auffindbares Zitat wird „unklar“ (kein Umschreiben auf Zuruf, nur ein Hinweis). Steht das Zitat
    in einem anderen Absatz als angegeben, gilt der Absatz des Zitats.

    0.4.76: „widerspricht“ ohne auffindbares Zitat bleibt „widerspricht“, wenn der angegebene Absatz das Ereignis
    erkennbar enthält (mindestens WIDERSPRUCH_STAEMME Wortstämme aus Ereignis und Ausgang) – sonst blieb eine falsche
    Person im Kapitel stehen, obwohl die Auswahl es richtig hatte. Gelingt das nicht, gilt der Punkt als ungeprüft (keine
    Nachbesserung, keine Markierung); ein bloßes „unklar“ aus der Prüfung markiert ebenfalls nichts mehr."""
    teile = absaetze(text) if text else []
    norm = _normwoerter(text) if text else ""
    norm_teile = [_normwoerter(t) for t in teile]
    nach_nr = {}
    for a in d.get("punkte") or d.get("points") or []:
        if not isinstance(a, dict):
            continue
        try:
            nr = int(a.get("nr") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= nr <= anzahl_punkte and nr not in nach_nr:
            nach_nr[nr] = a
    aus = []
    for nr in range(1, anzahl_punkte + 1):
        a = nach_nr.get(nr) or {}
        try:
            absatz = int(a.get("absatz") or a.get("paragraph") or 0) - 1
        except (TypeError, ValueError):
            absatz = -1
        status = PFLICHT_STATUS.get(str(a.get("status") or "").strip().lower(), "ungeprueft")
        zitat = klartext(a.get("zitat") or a.get("quote") or "")[:300] if isinstance(a.get("zitat") or a.get("quote"), str) else ""
        begruendung = klartext(a.get("begruendung") or a.get("reason") or "")[:300]
        if text and status in ("erzaehlt", "widerspricht"):
            if zitat and zitat_belegt(zitat, norm):
                wo = next((i for i, nt in enumerate(norm_teile) if zitat_belegt(zitat, nt)), None)
                if wo is not None:
                    absatz = wo
            elif status == "erzaehlt":
                status, zitat = "fehlt", ""
                begruendung = begruendung or OHNE_ZITAT.get(sprache, OHNE_ZITAT["de"])
            else:
                zitat = ""
                p = punkte[nr - 1] if punkte and nr <= len(punkte) else None
                if not (p and 0 <= absatz < len(teile) and len(_staemme(f"{p['ereignis']} {p.get('ausgang') or ''}")
                                                              & _staemme(teile[absatz])) >= WIDERSPRUCH_STAEMME):
                    status = "ungeprueft"
        if status == "unklar":
            status = "ungeprueft"
        aus.append({"nr": nr, "status": status, "absatz": absatz if 0 <= absatz < anzahl_absaetze else None,
                    "zitat": zitat, "begruendung": begruendung})
    if punkte:
        reihenfolge_pruefen(aus, punkte)
    return aus


REIHENFOLGE_ABSTAND_S = 1800.0  # so viel früher muss das andere Ereignis geschehen sein …
REIHENFOLGE_ABSAETZE = 2  # … und so viele Absätze später erzählt werden, damit es als verlegt gilt


def reihenfolge_pruefen(befund: list[dict], punkte: list[dict]) -> None:
    """0.4.78: Steht ein erzähltes Ereignis deutlich vor einem anderen, das am Tisch eine halbe Stunde früher geschah
    (mindestens zwei Absätze davor)? Dann bekommt es „reihenfolge“ = Nummer des anderen. Das ist nur ein Hinweis für die
    Prüfansicht: Wer Erzählstränge nebeneinander erzählt, springt ein wenig; eine ganze verlegte Szene (eine Rettung
    vor ihrem Anlass) fällt so auf. Nachgebessert wird deshalb nichts."""
    erzaehlt = [(b, punkte[b["nr"] - 1]) for b in befund
                if b["status"] == "erzaehlt" and b.get("absatz") is not None and b["nr"] <= len(punkte)
                and punkte[b["nr"] - 1].get("zeit") is not None]
    for b, p in erzaehlt:
        spaeter = [(q_b["absatz"], q_b["nr"]) for q_b, q in erzaehlt
                   if q["zeit"] <= p["zeit"] - REIHENFOLGE_ABSTAND_S and q_b["absatz"] >= b["absatz"] + REIHENFOLGE_ABSAETZE]
        if spaeter:
            b["reihenfolge"] = max(spaeter)[1]


FEHLEND_HOECHSTENS = 5  # so viele fehlende Ereignisse darf die Vollständigkeitsprüfung nennen
ERGAENZEN_RAENGE = ("kritisch", "wichtig")  # nur diese Ränge werden ergänzt
RELATION_FENSTER_S = 60.0  # Transkript ± so viele Sekunden um die belegten Stellen eines Absatzes
RELATION_ZEICHEN = 6000  # höchstens so viel Transkript je Absatz

SYSTEM_RELATIONEN = """Du prüfst einzelne Absätze des Recaps einer Pen-&-Paper-Session gegen kurze Ausschnitte des \
ORIGINALTRANSKRIPTS (automatisch erkannt, mit Fehlern; der Sprecher „Spielleitung“ spricht für Nichtspielercharaktere). \
Es geht nur um Beziehungen, nicht um Vollständigkeit oder Stil:
- Wer tut was wem? Wer gibt wem was, und wer hat es danach? War jemand schon verletzt, oder wird er es erst durch die \
erzählte Handlung? Wer verspricht wem was, gegen welche Gegenleistung? Wer kennt wen, seit wann, wer bürgt für wen? \
Ist eine Figur in der Szene anwesend oder wird nur über sie gesprochen? Sind zwei Figuren verwechselt oder zu einer \
verschmolzen? Ist etwas beobachtet, behauptet, vermutet, geplant, erinnert oder eine Vision?
Für jeden Absatz: urteil "stimmt" (alle Beziehungen wie im Transkript), "widerspricht" (mindestens eine Beziehung ist \
im Transkript anders: Richtung vertauscht, Zustand umgedreht, Figuren verwechselt, Vermutung als Tatsache, Erwähnte \
als Anwesende) oder "unklar" (der Ausschnitt reicht nicht). Bei "widerspricht": begruendung mit der richtigen \
Beziehung in einem Satz und einem kurzen wörtlichen Zitat aus dem Transkript.
Antworte nur mit JSON: {"absaetze": [{"nr": 1, "urteil": "…", "begruendung": "…", "zitat": "…"}]}. \
Sprache: {sprache}."""

SYSTEM_NACHBESSERUNG = """Du überarbeitest einzelne Absätze des Recaps einer Pen-&-Paper-Session. Eine Prüfung hat \
sie beanstandet; der Grund steht jeweils dabei.
Schreibe jeden genannten Absatz neu, sodass er nur noch enthält, was die Grundlage belegt. Lass Unbelegtes und \
Ausgeschmücktes weg, statt es umzuformulieren – lieber kürzer. Gleicher Ton, Erzählstimme in der Vergangenheit, Figuren \
nach ihren Charakteren. Die Spielleitung ist keine Figur: Was sie sagt oder tut, sagt oder tut ein \
Nichtspielercharakter oder die Erzählung. Bleibt von einem Absatz nichts Belegtes übrig, gib als text "" zurück. Reiner Text ohne Markdown.
Antworte nur mit JSON: {"absaetze": [{"nr": 2, "text": "…"}]}. Sprache: {sprache}."""

URTEILE = {"belegt": "supported", "teilweise": "partial", "unbelegt": "unsupported", "widerspricht": "contradicted",
           "witz": "off_game", "supported": "supported", "partial": "partial", "unsupported": "unsupported",
           "contradicted": "contradicted", "off_game": "off_game"}
BEANSTANDET = ("unsupported", "contradicted", "off_game")
HINWEIS_RELATION = {"de": "Bitte prüfen: ", "en": "Please check: "}
HINWEIS_TISCH = {"de": "Klingt nach Gespräch am Tisch: ", "en": "Sounds like table talk: "}
TISCH_ZITAT = 120  # Zeichen; die Begründung bleibt ein kurzer Satz


def hinweise_eintragen(pruefung: dict, befunde: list[dict], sprache: str | None) -> None:
    """0.4.62: Wörtliche Rede, die nach Gespräch am Tisch klingt, bleibt im Kapitel; die Spielleitung bekommt am
    Absatz einen Hinweis (Recap.review), statt dass der Filter still etwas löscht.
    0.4.64: Der Absatz gilt dann als „vermutlich außerhalb des Spiels“ (off_game) – die App markiert nur noch
    beanstandete Absätze –, und die Begründung ist genau dieser eine Satz. Widerspricht der Absatz zugleich der
    Grundlage, bleibt dieses Urteil mit seiner Begründung stehen."""
    vorsatz = HINWEIS_TISCH["en" if sprache == "en" else "de"]
    absaetze_ = {b.get("index"): b for b in pruefung.get("paragraphs") or []}
    for f in befunde:
        if f.get("art") != "tischgespraech" or f.get("absatz") not in absaetze_:
            continue
        b = absaetze_[f["absatz"]]
        if b.get("verdict") == "contradicted" and b.get("note"):
            continue
        zitat = f["text"] if len(f["text"]) <= TISCH_ZITAT else f["text"][:TISCH_ZITAT].rsplit(" ", 1)[0] + " …“"
        b["verdict"], b["note"] = "off_game", vorsatz + zitat


def pflicht_eintragen(pruefung: dict, befund: list[dict], anzahl: int) -> None:
    """0.4.65: Was nach der Nachbesserung noch einem ausgewählten Ereignis widerspricht, markiert den Absatz als
    „widerspricht“ – mit der Begründung der Pflichtprüfung, falls die Gegenprüfung keine eigene hat."""
    absaetze_ = {b.get("index"): b for b in pruefung.get("paragraphs") or []}
    for f in befund:
        if f.get("status") != "widerspricht" or f.get("absatz") is None or not (0 <= f["absatz"] < anzahl):
            continue
        b = absaetze_.get(f["absatz"])
        if b is None:
            continue
        if not (b.get("verdict") == "contradicted" and b.get("note")):
            b["note"] = f["begruendung"] or None
        b["verdict"] = "contradicted"


# 0.4.75: Prüfansicht aus der Pflichtprüfung und den unklaren Stellen statt aus der Gegenprüfung jedes Absatzes gegen die
# Notizen. Die Gegenprüfung las dieselben Notizen wie das Kapitel und fand so 1 von 7 schweren Fehlern, markierte dafür
# Formulierungen; hier stehen nur Dinge, die der Server nachgerechnet hat (Zitate) oder die das Modell selbst als nicht
# eindeutig gekennzeichnet hat. Für die App unverändert: Recap.review mit verdict, note und Belegstellen.
UNKLAR_HINWEIS = {"de": "Die Aufnahme ist hier nicht eindeutig: ", "en": "The recording is ambiguous here: "}
FEHLT_HINWEIS = {"de": "Nicht erzählt: ", "en": "Not told: "}
REIHENFOLGE_HINWEIS = {"de": "Reihenfolge prüfen: „{a}“ ({za}) steht vor „{b}“ ({zb}), das am Tisch früher geschah.",
                       "en": "Check the order: “{a}” ({za}) comes before “{b}” ({zb}), which happened earlier at the table."}
UNKLAR_NAEHE_S = 300.0  # eine unklare Notiz ohne eigenes Ereignis gehört zum Absatz des zeitlich nächsten Ereignisses


def hinweise_filtern(pruefung: dict) -> None:
    """0.4.78: Die Texte der Prüfansicht gehen durch denselben Feinschliff wie das Kapitel (Regelreste, Versalien,
    Tischsprache). Bleibt danach nichts übrig, bleibt der ursprüngliche Hinweis."""
    from app import artefakte

    for b in pruefung.get("paragraphs") or []:
        if isinstance(b.get("note"), str) and b["note"]:
            b["note"] = artefakte.feinschliff(regelreste(b["note"])) or b["note"]


def pruefansicht_aus_pflicht(text: str, punkte: list[dict], befund: list[dict], unklar: list[dict], blick: list[dict],
                             modell: str, nachgebessert: bool, sprache: str = "de") -> dict:
    """Je Absatz ein Urteil aus den Pflichtereignissen: „widerspricht“ → contradicted; „unklar“ (zweiter Blick oder Notiz)
    → unsupported mit dem Hinweis, was offen ist; „fehlt“ → partial mit „Nicht erzählt: …“; nur Erzähltes → supported mit
    den Zeitpunkten der Ereignisse als Belegstellen; ohne Ereignis → unchecked. Zitate verorten die Absätze neu, falls
    der Filter inzwischen etwas gestrichen hat."""
    teile = absaetze(text)
    norm_teile = [_normwoerter(t) for t in teile]
    sp = "en" if sprache == "en" else "de"
    je_absatz: dict[int, list[tuple[dict, dict]]] = {}
    zeit_zu_absatz: list[tuple[float, int]] = []
    for b in befund:
        if not (1 <= b.get("nr", 0) <= len(punkte)):
            continue
        p = punkte[b["nr"] - 1]
        absatz = b.get("absatz")
        if b.get("zitat"):
            wo = next((i for i, nt in enumerate(norm_teile) if zitat_belegt(b["zitat"], nt)), None)
            if wo is not None:
                absatz = wo
        if absatz is None or not (0 <= absatz < len(teile)):
            continue
        je_absatz.setdefault(absatz, []).append((p, b))
        if p.get("zeit") is not None:
            zeit_zu_absatz.append((p["zeit"], absatz))
    belege_blick = {z["nr"]: z["zitat"] for z in blick if z.get("zitat")}
    aus = []
    for i in range(len(teile)):
        eintraege = je_absatz.get(i, [])
        verdict, note, evidence = "unchecked", None, []
        for p, b in eintraege:
            if p.get("zeit") is not None:
                evidence.append({"start": p["zeit"], "quote": belege_blick.get(b["nr"], "")})
        if eintraege:
            verdict = "supported"
        for p, b in eintraege:
            if b["status"] == "fehlt" and verdict in ("supported",):
                verdict, note = "partial", FEHLT_HINWEIS[sp] + p["ereignis"] + (f" – {p['ausgang']}" if p["ausgang"] else "")
        for p, b in eintraege:
            q_nr = b.get("reihenfolge")
            if q_nr and verdict == "supported" and 1 <= q_nr <= len(punkte):
                q = punkte[q_nr - 1]
                verdict, note = "partial", REIHENFOLGE_HINWEIS[sp].format(
                    a=p["ereignis"], za=_zeit(p["zeit"]), b=q["ereignis"], zb=_zeit(q["zeit"]))
        for p, b in eintraege:
            if p.get("unklar") and verdict in ("supported", "partial"):
                grund = _UNKLAR.search(p.get("ausgang") or "")
                was = grund.group(0).strip(" ()") if grund else ""
                was = re.sub(r"^(?:unklar|unsicher)[:,]?\s*", "", was, flags=re.I)
                if not was or was.casefold() in ("wer", "wer handelt", "von wem", "who"):
                    was = _UNKLAR.sub("", p["ereignis"]).strip() + (" – wer?" if sp == "de" else " – who?")
                verdict, note = "unsupported", UNKLAR_HINWEIS[sp] + was
        for p, b in eintraege:
            if b["status"] == "widerspricht":
                verdict, note = "contradicted", b.get("begruendung") or note
        aus.append({"index": i, "verdict": verdict, "note": note, "evidence": evidence[:3]})
    # unklare Notizen ohne eigenes Ereignis: an den Absatz des zeitlich nächsten Ereignisses
    for u in unklar:
        if u.get("quelle") == "zweiter_blick" or u.get("zeit") is None or not zeit_zu_absatz:
            continue
        abstand, absatz = min((abs(z - u["zeit"]), a) for z, a in zeit_zu_absatz)
        b = aus[absatz]
        if abstand <= UNKLAR_NAEHE_S and b["verdict"] in ("supported", "partial", "unchecked"):
            b["verdict"], b["note"] = "unsupported", UNKLAR_HINWEIS[sp] + _UNKLAR.sub("", u["notiz"]).strip()
    return {"model": modell, "revised": nachgebessert, "paragraphs": aus}


def spielleitung_beanstanden(befund: list[dict], text: str) -> list[dict]:
    """Absätze, in denen die Spielleitung als Figur auftritt („die Spielleitung zusicherte“), gelten als beanstandet –
    die Nachbesserung bekommt den Grund. Die Prüfung durch das Modell übersieht das regelmäßig."""
    teile = absaetze(text)
    for b in befund:
        i = b.get("index", -1)
        if 0 <= i < len(teile) and _SPIELLEITUNG.search(teile[i]) and b["verdict"] not in BEANSTANDET:
            b["verdict"] = "off_game"
            b["note"] = "Die Spielleitung ist keine Figur – nenne den Nichtspielercharakter, für den sie spricht, oder erzähle ohne sie."
    return befund


def fehlend_lesen(d: dict, grundlage: str) -> list[dict]:
    """Antwort der Vollständigkeitsprüfung → [{zeit, notiz, belegt}]. belegt: Es gibt in der Grundlage eine Zeile mit
    diesem Zeitstempel, deren Text zur Notiz passt, oder eine Zeile, die den Notiztext enthält. Nur belegte Punkte
    dürfen ergänzt werden – der Prüfer darf keine neue Wahrheit erzeugen."""
    roh = [z.strip() for z in grundlage.split("\n") if z.strip() and _zeit_vorn(z) is not None]  # nur Notizen mit Zeit
    zeilen = [(_zeit_vorn(z), _notizkern(z), z) for z in roh]
    aus = []
    for f in d.get("fehlend") or d.get("missing") or []:
        if len(aus) >= FEHLEND_HOECHSTENS:
            break
        if not isinstance(f, dict):
            continue
        zeit = zeit_lesen(f.get("zeit") if f.get("zeit") is not None else f.get("time"))
        notiz = klartext(f.get("notiz") or f.get("note") or "")[:400]
        if not notiz:
            continue
        rang = RAENGE.get(str(f.get("wichtigkeit") or f.get("importance") or "").strip().lower(), "wichtig")
        kern = _notizkern(notiz)
        woerter = set(kern.split())
        treffer = None
        for i, (zt, zk, _z) in enumerate(zeilen):
            if not zk:
                continue
            if kern and (kern in zk or zk in kern):
                treffer = i
                break
            if zeit is not None and zt is not None and abs(zt - zeit) < 1 and woerter:
                gemeinsam = len(woerter & set(zk.split())) / len(woerter)
                if gemeinsam >= 0.6:
                    treffer = i
                    break
        davor = zeilen[treffer - 1][2] if treffer is not None and treffer > 0 else ""
        danach = zeilen[treffer + 1][2] if treffer is not None and treffer + 1 < len(zeilen) else ""
        aus.append({"zeit": zeit, "notiz": notiz, "wichtigkeit": rang, "belegt": treffer is not None,
                    "davor": davor[:300], "danach": danach[:300], "ergaenzt": False})
    return aus


RAENGE = {"kritisch": "kritisch", "critical": "kritisch", "wichtig": "wichtig", "major": "wichtig",
          "nebensächlich": "nebensächlich", "nebensaechlich": "nebensächlich", "minor": "nebensächlich"}
_FUELL = {"nicht", "einer", "einem", "einen", "eines", "gruppe", "wurde", "wurden", "werden", "haben", "hatte",
          "hatten", "sowie", "dieser", "diese", "dieses", "seine", "seinen", "ihrer", "ihren", "durch", "während",
          "nachdem", "bevor", "danach", "dabei", "wieder", "sagte", "fragt", "fragte", "spielleitung", "charakter"}


def _kennwoerter(text: str) -> set[str]:
    """Kennzeichnende Wörter eines Textes (ab sechs Buchstaben, ohne Füllwörter) – zum Wiederfinden einer Stelle."""
    return {w for w in _notizkern(text).split() if len(w) >= 6 and w not in _FUELL}


KURZER_ENTWURF = 0.5  # 0.4.78: Entwurf unter diesem Anteil der Untergrenze → einmal neu anfordern
ROHANTWORT_ZEICHEN = 20000  # so viel der Rohantwort eines zu kurzen Entwurfs bleibt für die Fehlersuche
ABSATZ_HOECHSTENS = 220  # Wörter; längere Absätze werden an Satzgrenzen geteilt
ABSATZ_ZIEL = 140
_SATZENDE = re.compile(r"(?<=[.!?…])[»«“\"')]*\s+(?=[„\"»«(]?[A-ZÄÖÜ0-9])")


_ZAHL_ANFANG = re.compile(r"\d{1,3}\.\s")


def absaetze_teilen(text: str) -> str:
    """Absätze mit mehr als ABSATZ_HOECHSTENS Wörtern an Satzgrenzen in Stücke von etwa ABSATZ_ZIEL Wörtern teilen.
    Manche Modelle liefern das ganze Kapitel als einen Block – dann kann es niemand vorlesen, und die Gegenprüfung gibt
    nur ein Urteil für alles. Kein Wort wird verändert, nur Leerzeilen kommen hinzu."""
    aus = []
    roh: list[str] = []
    for absatz in [a.strip() for a in re.split(r"\n\s*\n", text or "") if a.strip()]:
        # 0.4.76: Ein Absatz, der mit einer nackten Zahl beginnt („22. Die Gruppe …“), ist ein Umbruch mitten im Satz
        # („… der Vorfall von⏎⏎22. Die Gruppe“) – wieder anhängen, wenn der Absatz davor keinen Satzschluss hat.
        if roh and _ZAHL_ANFANG.match(absatz) and not re.search(r"[.!?…:“”\"»«)]$", roh[-1]):
            roh[-1] += " " + absatz
        else:
            roh.append(absatz)
    for absatz in roh:
        if len(absatz.split()) <= ABSATZ_HOECHSTENS:
            aus.append(absatz.strip())
            continue
        saetze = [x for x in _SATZENDE.split(absatz.strip()) if x.strip()]
        stueck: list[str] = []
        for satz in saetze:
            stueck.append(satz.strip())
            if sum(len(x.split()) for x in stueck) >= ABSATZ_ZIEL:
                aus.append(" ".join(stueck))
                stueck = []
        if stueck:
            rest = " ".join(stueck)
            if aus and len(rest.split()) < ABSATZ_ZIEL // 3:
                aus[-1] += " " + rest  # kein Absatz aus einem halben Satz
            else:
                aus.append(rest)
    return "\n\n".join(aus)


def absaetze(text: str) -> list[str]:
    """Absätze eines Recaps (durch Leerzeile getrennt) – wie die App sie zählt."""
    return [a.strip() for a in re.split(r"\n\s*\n", text.strip()) if a.strip()]


def pruefung_lesen(d: dict, anzahl: int) -> list[dict]:
    """Antwort der Gegenprüfung → je Absatz {index, verdict, note, evidence: [{start, quote}]} (start in Sekunden,
    noch nicht gegen das Transkript geprüft – das macht die Zentrale). Fehlende Absätze: unchecked."""
    roh = d.get("absaetze") or d.get("paragraphs") or []
    nach_nr: dict[int, dict] = {}
    for a in roh if isinstance(roh, list) else []:
        if not isinstance(a, dict):
            continue
        try:
            nr = int(a.get("nr") or a.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= nr <= anzahl and nr not in nach_nr:
            nach_nr[nr] = a
    aus = []
    for nr in range(1, anzahl + 1):
        a = nach_nr.get(nr)
        if a is None:
            aus.append({"index": nr - 1, "verdict": "unchecked", "note": None, "evidence": []})
            continue
        urteil = URTEILE.get(str(a.get("urteil") or a.get("verdict") or "").strip().lower(), "unchecked")
        belege = []
        for st in (a.get("stellen") or a.get("evidence") or [])[:3]:
            if isinstance(st, str):
                st = {"zeit": st}
            if not isinstance(st, dict):
                continue
            zeit = zeit_lesen(st.get("zeit") if st.get("zeit") is not None else st.get("start"))
            zitat = klartext(st.get("zitat") or st.get("quote") or "")[:300]
            if zeit is not None or zitat:
                belege.append({"start": zeit, "quote": zitat})
        note = klartext(a.get("begruendung") or a.get("note") or "")[:500] or None
        aus.append({"index": nr - 1, "verdict": urteil, "note": note, "evidence": belege})
    return aus


def _erwaehnt(name: str, text: str) -> bool:
    """Kommt der Name (oder ein markantes Wort daraus) im Text vor?"""
    klein = text.lower()
    if name.lower() in klein:
        return True
    woerter = [w for w in re.findall(r"\w+", name.lower()) if len(w) >= 4 and w not in ("der", "die", "das", "the")]
    return any(re.search(rf"\b{re.escape(w)}", klein) for w in woerter)


def klartext(text) -> str:
    """Markdown aus Modelltexten entfernen – die App zeigt reinen Text, und kleine Modelle streuen trotz Anweisung
    **fett**, ## Überschriften und Listen in einer Zeile („… - **Ziel:** … - **Lage:** …“) ein."""
    t = str(text or "")
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)  # Überschriften
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)  # fett
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", t)  # kursiv
    t = t.replace("`", "")
    if len(re.findall(r"\s+[-•]\s+(?=[A-ZÄÖÜ0-9])", t)) >= 2:  # Liste in einer Zeile → eigene Zeilen
        t = re.sub(r"\s+[-•]\s+(?=[A-ZÄÖÜ0-9])", "\n- ", t)
    if len(re.findall(r"\s+\d{1,2}\.\s+(?=[A-ZÄÖÜ])", t)) >= 2:  # „… 1. Route … 2. Tarnung …“
        t = re.sub(r"\s+(\d{1,2}\.)\s+(?=[A-ZÄÖÜ])", r"\n\1 ", t)
    t = re.sub(r"(?m)^\s*[•*]\s+", "- ", t)
    t = _MODELLMUELL.sub("", t)  # Steuerzeichen des Modells („<tool_call|>“, „<|im_end|>“) haben im Text nichts verloren
    t = re.sub(r"(?m)^[ \t\-•,;:]*[\]\}\[\{][ \t\-•\]\}\[\{,;:]*$\n?", "", t)  # Zeilen nur aus Klammern (abgeschnittenes JSON)
    return re.sub(r"[ \t]+\n", "\n", t).strip()


_MODELLMUELL = re.compile(r"<\|?/?[a-z_]*(?:tool_call|im_end|im_start|eot_id|end_of_turn|start_of_turn)[a-z_]*\|?>|</?s>",
                          re.I)
_ZEITMARKE = re.compile(r"\[\d{1,2}(?::\d{2}){1,2}\]")


def _schon_erzaehlt(notiz: str, text: str) -> bool:
    """Steht das Ereignis der Notiz schon im Text? Deterministisch über Kennwörter: Mindestens drei Kennwörter der
    Notiz, und drei Viertel davon kommen im Text vor. Verhindert, dass die Vollständigkeitsprüfung Vorhandenes
    „ergänzt“ (Lauf 1 am 06.10.: dieselbe Notiz stand als zweiter Satz noch einmal da)."""
    kw = {w[:7] for w in _kennwoerter(notiz)}  # Wortstämme, damit „Lysanders“ zu „Lysander“ passt
    if len(kw) < 3:
        return False
    da = {w[:7] for w in _kennwoerter(text)}
    return len(kw & da) >= max(3, int(len(kw) * 0.75 + 0.5))


def _rohe_notiz(neu: str, alt: str, notizen: list[str]) -> bool:
    """Hat das Modell statt zu erzählen die Notiz abgeschrieben? Zeitmarke im neuen Text, die im alten nicht war,
    oder der Notizkern steht wörtlich darin."""
    if _ZEITMARKE.search(neu) and not _ZEITMARKE.search(alt):
        return True
    kern_neu = _notizkern(neu)
    return any(_notizkern(n) and _notizkern(n) in kern_neu for n in notizen)


RECAP_SCHLUESSEL = ("text", "recap", "summary", "zusammenfassung", "body", "content", "story", "inhalt")


def _form(v) -> str:
    if isinstance(v, str):
        return f"str[{len(v)}]"
    if isinstance(v, dict):
        return "{" + ",".join(v.keys()) + "}"
    if isinstance(v, list):
        return f"list[{len(v)}]"
    return type(v).__name__


def recap_text(d: dict) -> str:
    """Den Recap-Text aus der Antwort holen – kleine Modelle halten sich nicht immer an den Feldnamen „text“:
    sie nennen ihn „recap“ oder „summary“, verschachteln ihn oder liefern Absätze als Liste."""
    t = _recap_haupttext(d)
    if not t:
        return ""
    # 0.4.78: Lange Runden gliedert das Modell manchmal selbst – Einleitung in „text“, die Abschnitte in einem eigenen
    # Feld („abschnitte“: [{"titel": …, "text": …}]). Gelesen wurde nur „text“; vom Kapitel blieb die Einleitung
    # (gemessen, ein Lauf von drei: 163 Wörter Entwurf bei gleich vielen Ausgabe-Tokens). Weitere lange Textfelder
    # kommen darum in ihrer Reihenfolge dazu – außer sie wiederholen nur, was schon dasteht.
    staemme = _staemme(t)
    teile = [t]
    for k, v in d.items():
        if k in _KEIN_KAPITELTEXT or not (str(k).casefold() in _EXTRA_SCHLUESSEL
                                          or (isinstance(v, list) and any(isinstance(a, dict) for a in v))):
            continue
        x = _als_text(v)
        if not x or x == t or len(x.split()) < EXTRA_TEXT_WOERTER:
            continue
        s = _staemme(x)
        if s and len(s & staemme) / len(s) >= EXTRA_TEXT_UEBERLAPPUNG:
            continue
        teile.append(x)
        staemme |= s
    return "\n\n".join(teile)


_TEXTFELDER = ("text", "inhalt", "content", "absatz", "paragraph", "body", "zusammenfassung", "summary", "erzaehlung",
               "story")
_KEIN_KAPITELTEXT = {"title", "titel", "openThreads", "open_threads", "offeneFaeden", "offene_faeden"}
_EXTRA_SCHLUESSEL = {*RECAP_SCHLUESSEL, "abschnitte", "teile", "sections", "chapters", "parts", "paragraphs",
                     "absaetze", "kapitel", "szenen", "scenes", "fortsetzung", "continuation", "hauptteil", "main"}
EXTRA_TEXT_WOERTER = 40  # kürzere Felder sind keine Kapitelteile
EXTRA_TEXT_UEBERLAPPUNG = 0.6  # so viele Wortstämme schon im Kapitel: nur eine Wiederholung (z. B. Zusammenfassung)


def _als_text(v) -> str:
    """Ein Feld als Fließtext: Zeichenkette, Liste von Zeichenketten oder Liste von Abschnitten mit Textfeld."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list) and v:
        teile = []
        for a in v:
            if isinstance(a, str):
                teile.append(a.strip())
            elif isinstance(a, dict):
                teile.append(next((a[k].strip() for k in _TEXTFELDER if isinstance(a.get(k), str) and a[k].strip()), ""))
        return "\n\n".join(x for x in teile if x)
    return ""


def _recap_haupttext(d: dict) -> str:
    for k in RECAP_SCHLUESSEL:
        t = _als_text(d.get(k))
        if t:
            return t
    for v in d.values():  # eine Ebene verschachtelt: {"recap": {"title": …, "text": …}}
        if isinstance(v, dict):
            for k in RECAP_SCHLUESSEL:
                t = _als_text(v.get(k))
                if t:
                    return t
    lang = [_als_text(v) for k, v in d.items() if k not in _KEIN_KAPITELTEXT and len(_als_text(v)) >= 200]
    return lang[0] if len(lang) == 1 else ""


_TITEL_DOPPELT = re.compile(r"^\s*((?:Kapitel|Chapter)\s+\d+)\s*[:.–-]\s*(?:(?:Kapitel|Chapter)\s+\d+\s*[:.–-]\s*)+",
                            re.IGNORECASE)


def titel_saeubern(titel: str) -> str:
    """0.4.78: „Kapitel 1: Kapitel 1: Schatten“ → „Kapitel 1: Schatten“ (lokale Modelle setzen die Vorgabe doppelt)."""
    return _TITEL_DOPPELT.sub(lambda m: m.group(1) + ": ", titel or "").strip()


def _json(antwort: Antwort, retten: bool = False) -> dict:
    """JSON-Objekt aus der Antwort. retten=True: eine abgeschnittene Antwort (Längengrenze erreicht) bis zum letzten
    vollständigen Wert übernehmen – nur für Szenennotizen, wo ein fehlender Rest nichts verfälscht."""
    text = antwort.text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        wert = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        try:
            wert = json.loads(m.group(0)) if m else None
        except ValueError:
            wert = None
        if wert is None and retten:
            wert = json_retten(text)
            if isinstance(wert, dict):
                wert["_gerettet"] = True  # abgeschnitten – der Aufrufer weiß dann, dass das Ende fehlt
        if wert is None:
            raise AntwortFehler("Das Sprachmodell hat kein gültiges JSON geliefert.") from None
    if not isinstance(wert, dict):
        raise AntwortFehler("Das Sprachmodell hat kein JSON-Objekt geliefert.")
    return wert


def json_retten(text: str) -> dict | None:
    """Abgeschnittenes JSON bis zum letzten vollständigen Wert kürzen und die offenen Klammern schließen."""
    start = text.find("{")
    if start < 0:
        return None
    stapel: list[str] = []
    im_text = escape = False
    letzte = None  # (Position nach einem vollständigen Wert, offene Klammern dort)
    for i in range(start, len(text)):
        c = text[i]
        if im_text:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                im_text = False
                if stapel and stapel[-1] == "[":
                    letzte = (i + 1, list(stapel))
            continue
        if c == '"':
            im_text = True
        elif c in "{[":
            stapel.append(c)
        elif c in "}]":
            if not stapel:
                break
            stapel.pop()
            letzte = (i + 1, list(stapel))
            if not stapel:
                break
    if letzte is None:
        return None
    pos, offen = letzte
    rest = "".join("]" if k == "[" else "}" for k in reversed(offen))
    try:
        wert = json.loads(text[start:pos] + rest)
    except ValueError:
        return None
    return wert if isinstance(wert, dict) else None


# ---------------------------------------------------------------- Ablauf
def _zahl(n: int) -> str:
    return f"{n:,}".replace(",", " ")  # 3 800 mit schmalem Leerzeichen – kein Punkt, der mit dem Komma streitet


@dataclass
class Zaehler:
    tokens_in: int = 0
    tokens_out: int = 0
    aufrufe: int = 0

    token_s: list[float] = field(default_factory=list)  # gemessene Geschwindigkeit je Aufruf (lokales Modell)

    letzte_antwort: str = ""  # Rohtext der letzten Antwort (nur für die Fehlersuche im Modellvergleich)
    kosten: float = 0.0  # Cent, je Aufruf mit dem Preis des jeweiligen Klienten (zwei Modelle möglich)

    def kosten_cent(self) -> int:
        return round(self.kosten)

    def aufruf(self, klient: Klient, system: str, nutzer: str, retten: bool = False) -> dict:
        a = self._chat(klient, system, nutzer)
        try:
            return _json(a, retten)
        except SprachmodellFehler:  # ein zweiter Versuch – kleine Modelle stolpern gelegentlich
            a = self._chat(klient, system, nutzer + "\n\nAntworte ausschließlich mit gültigem JSON.")
            return _json(a, retten)

    def _chat(self, klient: Klient, system: str, nutzer: str) -> Antwort:
        a = klient.chat(system, nutzer)
        self.letzte_antwort = a.text
        self.tokens_in += a.tokens_in
        self.tokens_out += a.tokens_out
        self.aufrufe += 1
        preis = getattr(klient, "cent_pro_mio", None)
        if preis:
            self.kosten += (a.tokens_in * preis[0] + a.tokens_out * preis[1]) / 1_000_000
        rate = getattr(klient, "token_s", None)
        if rate:
            from app.worker_prozess import taetigkeit

            self.token_s.append(rate)
            taetigkeit(f"Sprachmodell: Aufruf {self.aufrufe}, {_zahl(a.tokens_in)} Token gelesen, "
                       f"{_zahl(a.tokens_out)} geschrieben, {rate:.1f} Token/s".replace(".", ","),
                       schritt="sprachmodell", tokenS=round(rate, 1), aufruf=self.aufrufe)
        return a

    @property
    def token_s_mittel(self) -> float | None:
        return round(sum(self.token_s) / len(self.token_s), 1) if self.token_s else None


def _notizkern(n: str) -> str:
    """Notiz ohne Zeitstempel, Satzzeichen und Groß-/Kleinschreibung – zum Erkennen doppelter Notizen."""
    m = _ZEIT_VORN.match(n)
    return re.sub(r"[^\wäöüß]+", " ", (n[m.end():] if m else n).lower()).strip()


_SPRECHER_VORN = re.compile(r"^\[[^\]]*\]\s*[^:\n]{1,60}:\s*")  # „[1:00] Spielleitung: “


def _zeilenkerne(zeilen: list[str]) -> set[str]:
    """Transkriptzeilen als Kern (mit und ohne Sprechernamen), um abgeschriebene Zeilen in den Notizen zu erkennen."""
    kerne = set()
    for z in zeilen:
        k = _notizkern(z)
        if len(k) >= 12:
            kerne.add(k)
            ohne = _SPRECHER_VORN.sub("", z)
            if ohne != z and len(_notizkern(ohne)) >= 12:
                kerne.add(_notizkern(ohne))
    return kerne


def _teile_nach_zeit(notizen: list[str]) -> list[str]:
    """Notizen in Teile von etwa TEIL_MINUTEN Spielzeit, keiner länger als TEIL_TOKEN Token. Nach Zeit statt nach
    Textmenge, damit eine wortreiche halbe Stunde nicht den ganzen Teil füllt und das Ende der Runde in einen
    Zwei-Minuten-Teil rutscht."""
    zeiten = [t for t in (_zeit_vorn(n) for n in notizen) if t is not None]
    if not zeiten:
        return stuecke(notizen, TEIL_TOKEN)
    von, bis = min(zeiten), max(zeiten)
    anzahl = max(1, round((bis - von) / (TEIL_MINUTEN * 60)))
    laenge = (bis - von) / anzahl if anzahl else 0
    gruppen: list[list[str]] = [[] for _ in range(anzahl)]
    letzte = 0
    for n in notizen:
        t = _zeit_vorn(n)
        if t is not None and laenge:
            letzte = min(anzahl - 1, int((t - von) / laenge))
        gruppen[letzte].append(n)
    teile = []
    for g in gruppen:
        if g:
            teile += stuecke(g, TEIL_TOKEN)  # zu wortreich: weiter teilen
    return teile


def _zeilen_davor(zeilen: list[str], sekunden: float, hoechstens: int = 60) -> list[str]:
    """Die letzten Zeilen eines Abschnitts aus den letzten `sekunden` (nach Zeitstempel), höchstens `hoechstens`."""
    zeiten = [_zeit_vorn(z) for z in zeilen]
    ende = max((t for t in zeiten if t is not None), default=None)
    if ende is None or sekunden <= 0:
        return []
    aus = [z for z, t in zip(zeilen, zeiten) if t is not None and t >= ende - sekunden]
    return aus[-hoechstens:]


def _ohne_doppelte(notizen: list[str]) -> list[str]:
    """Gleiche Notizen aus verschiedenen Aufrufen (Rest nachgeholt, Abschnitt geteilt) nur einmal."""
    gesehen, aus = set(), []
    for n in notizen:
        k = _notizkern(n)
        if k and k in gesehen:
            continue
        gesehen.add(k)
        aus.append(n)
    return aus


@dataclass
class Ablauf:
    klient: Klient
    max_transkript_tokens: int = 90_000  # darüber: erst Szenennotizen
    stueck_tokens: int = 6_000
    zaehler: Zaehler = field(default_factory=Zaehler)
    schritt: Callable[[str], None] | None = None  # Zwischenstand für die App (summarizing.notes, .recap …)
    letzter_verlauf: str = ""  # Zusammenfassungen der Teile (lange Runden), für den Modellvergleich
    gliederung: str = "auto"  # lange Runden: "auto" = Notizen direkt in Zeitabschnitten, Teile nur als Ausweichlösung;
    #                            "direkt" / "teile" erzwingen das eine oder andere (Modellvergleich, A/B)
    temperatur_notizen: float | None = None  # Testoption: Temperatur nur für die Szenennotizen
    letztes_kapitel1: str = ""  # erster Entwurf des Recaps, vor Ergänzung und Nachbesserung (Modellvergleich)
    letzter_befund_fehlend: list = field(default_factory=list)  # Befund der Vollständigkeitsprüfung
    letzte_pruefung_vorher: list = field(default_factory=list)  # Fakten-/Relationsprüfung vor der Nachbesserung
    letzte_pruefung_nachher: list = field(default_factory=list)  # … und danach (leer, wenn nicht nachgebessert)
    letztes_kapitel2: str = ""  # nach der Ergänzung, vor der Nachbesserung
    letzte_relationen_vorher: list = field(default_factory=list)  # Relationsprüfung gegen das Transkript
    letzte_relationen_nachher: list = field(default_factory=list)
    nachbesserung: bool = True  # Testoption (Modellvergleich): False = Faktenprüfung ohne Umschreiben
    warnungen: list = field(default_factory=list)  # übersprungene Schritte mit Grund und Antwortanfang (Fehlersuche)
    vorschlag_klient: Klient | None = None  # 0.4.61: eigenes Modell für die Vorschläge (sonst klient)
    bereinigt: list = field(default_factory=list)  # 0.4.61: was der Artefakt-Filter entfernt hat (app/artefakte.py)
    # 0.4.63, Weg „Notizen zuerst“ (vorerst nur im Probelauf): Notizen auch dann, wenn die Abschrift ins Modell passt;
    # größere Abschnitte mit Überlappung als Lesekontext und laufendem Stand; mehr Platz für lange Runden.
    notizen_zuerst: bool = False
    notiz_stueck: int = 15_000  # Token Abschrift je Abschnitt im Weg „Notizen zuerst“
    notizen_je_stueck: int = 40
    ueberlappung_s: float = 120.0  # so viele Sekunden vor der Grenze sieht der nächste Abschnitt als Lesekontext
    letzte_notizen: str = ""
    letzter_stand: list = field(default_factory=list)
    letztes_transkript: str = ""
    letzte_auswahl: list = field(default_factory=list)  # 0.4.65: Pflichtpunkte des Wegs „Notizen zuerst“
    letzte_pflicht: dict = field(default_factory=dict)  # 0.4.65: Prüfung gegen die Pflichtpunkte, vorher/nachher
    letzte_kuerzung: dict = field(default_factory=dict)  # 0.4.68: Kürzung eines zu langen Kapitels (angenommen?)
    letztes_lang: str = ""  # 0.4.68: Kapitel vor einer angenommenen Kürzung
    notiz_klient: Klient | None = None  # 0.4.73: eigenes Modell für Notizen, Stand und Auswahl (sonst klient)
    letzte_unklar: list = field(default_factory=list)  # 0.4.73: Stellen „(unklar, wer)“ aus Notizen und Auswahl
    zweiter_blick_an: bool = True  # 0.4.74: kritische und unklare Ereignisse vor dem Schreiben an der Abschrift prüfen
    pruefansicht: str = "pflicht"  # 0.4.75: „pflicht“ (aus Pflichtprüfung und unklaren Stellen) oder „gegenpruefung“ (alt)
    letzter_zweiter_blick: list = field(default_factory=list)  # 0.4.74: je geprüftem Ereignis Urteil, Zitat, vorher/nachher
    letzter_kurzer_entwurf: list = field(default_factory=list)  # 0.4.78: zu kurze Entwürfe mit Rohantwort (Fehlersuche)
    letzte_grenzen: tuple | None = None  # 0.4.78: Längenvorgabe des Kapitels, wenn es aus Teilen besteht
    letzte_teile: list = field(default_factory=list)  # 0.4.78: Teile im Kapitel [{title, firstParagraph}] (leer: keine)
    letzte_einschnitte: dict = field(default_factory=dict)  # 0.4.78: Antwort der Suche nach Einschnitten (Probelauf)
    _teile_anfang: tuple = ((), ())  # 0.4.78: erster Absatz und Überschrift je Teil, zum Wiederfinden am Ende

    def _schritt(self, name: str) -> None:
        try:
            if self.schritt is not None:
                self.schritt(name)
            else:
                from app.worker_prozess import schritt_melden

                schritt_melden(name)
        except Exception:  # noqa: BLE001 – ein Zwischenstand darf nie den Auftrag kosten
            log.debug("Zwischenstand %s nicht gemeldet", name, exc_info=True)

    def grundlage(self, ein: dict, fortschritt: Callable[[float], None]) -> tuple[str, str]:
        """Transkript oder – wenn zu lang – Szenennotizen daraus. Liefert (Überschrift, Text)."""
        zeilen = transkript_zeilen(ein["transkript"])
        text = "\n".join(zeilen)
        self.letztes_transkript = text
        if tokens(text) <= self.max_transkript_tokens and not self.notizen_zuerst:
            return "Transkript", text
        if self.notizen_zuerst:
            return self._notizen_mit_stand(ein, zeilen, fortschritt)
        kopf = _kopf(ein)
        system = SYSTEM_NOTIZEN.replace("{sprache}", _sprache(ein)).replace("{hoechstens}", str(NOTIZEN_HOECHSTENS))
        self._schritt("notes")
        # Notizen sind kürzer als ihr Abschnitt, aber nicht beliebig: Die Antwort darf höchstens ANTWORT_HOECHSTENS
        # Token lang werden. Darum höchstens NOTIZ_STUECK Token je Abschnitt, auch bei großem Kontext.
        groesse = min(self.stueck_tokens, NOTIZ_STUECK)
        for runde in range(3):  # sehr lange Sessions bei kleinem Kontext: Notizen noch einmal verdichten
            teile = stuecke(zeilen, groesse)
            notizen = []
            for i, teil in enumerate(teile):
                was = "des Transkripts" if runde == 0 else "der bisherigen Szenennotizen (bitte weiter verdichten)"
                # Die letzten Notizen davor: wer ist da, was ist offen – damit Abschnitte nicht ohne Vorgeschichte stehen
                bisher = ("\n\nBisher (nur zur Orientierung, nicht wiederholen):\n" + "\n".join(notizen[-BISHER:])
                          if notizen and runde == 0 else "")
                notizen += self._notizen(system, f"{kopf}{bisher}\n\nAbschnitt {i + 1} von {len(teile)} {was}:\n",
                                         teil.split("\n"))
                fortschritt(min(0.6, 0.6 * (runde * 0.3 + (i + 1) / len(teile) * 0.7)))
            notizen = _ohne_doppelte(notizen)
            text = "\n".join(notizen)
            if tokens(text) <= self.max_transkript_tokens or not notizen:
                break
            zeilen = notizen
        return "Szenennotizen (aus dem Transkript verdichtet)", text

    def _notizen_mit_stand(self, ein: dict, zeilen: list[str], fortschritt: Callable[[float], None]) -> tuple[str, str]:
        """Weg „Notizen zuerst“: Abschnitte von notiz_stueck Token, nacheinander. Jeder sieht den laufenden Stand und die
        letzten ueberlappung_s Sekunden davor als Lesekontext (dazu keine Notizen – so entstehen an der Grenze keine
        zwei Fassungen derselben Szene). Ändert sich etwas, steht im Stand die Zeit der Änderung: aus „X ist hier“ und
        „X ist dort“ wird eine Abfolge, kein Widerspruch."""
        kopf = _kopf(ein)
        system = (SYSTEM_NOTIZEN.replace("{sprache}", _sprache(ein)).replace("{hoechstens}", str(self.notizen_je_stueck))
                  + ZUSATZ_STAND.replace("{stand_hoechstens}", str(STAND_HOECHSTENS)))
        self._schritt("notes")
        teile = [t.split("\n") for t in stuecke(zeilen, self.notiz_stueck)]
        notizen: list[str] = []
        self.letzter_stand = []
        for i, teil in enumerate(teile):
            vorspann = kopf
            if self.letzter_stand:
                vorspann += "\n\nStand bisher:\n" + "\n".join(f"- {s}" for s in self.letzter_stand)
            if i > 0:
                davor = _zeilen_davor(teile[i - 1], self.ueberlappung_s)
                if davor:
                    vorspann += "\n\nUnmittelbar davor (nur zum Verständnis, dazu keine Notizen):\n" + "\n".join(davor)
            self._stand_neu = None
            notizen += self._notizen(system, f"{vorspann}\n\nAbschnitt {i + 1} von {len(teile)} des Transkripts:\n",
                                     teil)
            if self._stand_neu:
                self.letzter_stand = self._stand_neu[:STAND_HOECHSTENS]
            fortschritt(min(0.6, 0.6 * (i + 1) / len(teile)))
        notizen = _ohne_doppelte(notizen)
        self.letzte_notizen = "\n".join(notizen)
        self.letzte_unklar = unklar_stellen(self.letzte_notizen)
        return "Szenennotizen (aus dem Transkript verdichtet)", self.letzte_notizen

    def _notizen(self, system: str, vorspann: str, zeilen: list[str], tiefe: int = 0, erinnert: bool = False) -> list[str]:
        """Szenennotizen zu einem Abschnitt. Liefert das Modell nichts Brauchbares, wird der Abschnitt geteilt
        (höchstens zweimal). Kommen die Notizen ohne Zeitstempel, werden sie einmal neu angefordert (ohne Zeiten
        lässt sich nichts prüfen und nichts belegen). Bricht die Antwort ab oder enden die Notizen lange vor dem
        Abschnittsende, bekommt der nicht abgedeckte Rest einen eigenen Aufruf – sonst fehlen genau die Stellen, an
        denen das Modell aufgab; Notizen ohne Zeit aus dem ersten Versuch fallen dann weg, damit nichts doppelt steht."""
        vorher = getattr(self.klient, "temperatur", None)
        if self.temperatur_notizen is not None and hasattr(self.klient, "temperatur"):
            self.klient.temperatur = self.temperatur_notizen
        try:
            d = self.zaehler.aufruf(self.notiz_klient or self.klient, system, vorspann + "\n".join(zeilen), retten=True)
        except AntwortFehler:
            if tiefe >= 2 or len(zeilen) < 8:
                raise
            log.info("Szenennotizen: Abschnitt wird geteilt (%d Zeilen)", len(zeilen))
            mitte = len(zeilen) // 2
            return (self._notizen(system, vorspann, zeilen[:mitte], tiefe + 1)
                    + self._notizen(system, vorspann, zeilen[mitte:], tiefe + 1))
        finally:
            if hasattr(self.klient, "temperatur"):
                self.klient.temperatur = vorher
        gerettet = bool(d.pop("_gerettet", False))
        stand = d.get("stand")
        if isinstance(stand, list) and stand:  # Weg „Notizen zuerst“: der zuletzt gelieferte Stand gilt
            self._stand_neu = [klartext(str(s))[:300] for s in stand if klartext(str(s))]
        kopien = _zeilenkerne(zeilen)
        notizen, zuletzt = [], None
        for n in d.get("notizen") or []:
            n = regelreste(_notiz_normieren(str(n)))
            m = _ZEIT_VORN.match(n)
            if m:
                zuletzt = f"[{m.group(1)}]"
            elif zuletzt is not None:
                n = f"{zuletzt} {n}"  # Notiz ohne Zeit gehört zur Stelle davor
            if n and _REGELN.search(n):  # 0.4.73: erst die Wendung bzw. den Satzteil nehmen, erst zuletzt die Notiz
                from app.artefakte import regelteil

                zeit_m = _ZEIT_VORN.match(n)
                kopf_z = n[:zeit_m.end()] if zeit_m else ""
                rest = regelteil(n[len(kopf_z):].strip(), _REGELN)
                n = f"{kopf_z}{rest}".strip() if rest else ""
            if not n or _bruchstueck(n):
                continue  # Würfe und Werte gehören nicht in die Geschichte; Bruchstücke auch nicht
            if _notizkern(n) in kopien or _notizkern(_SPRECHER_VORN.sub("", n)) in kopien:
                continue  # abgeschriebene Transkriptzeile statt Notiz
            if not notizen or n != notizen[-1]:  # Schleifen ergeben gleiche Zeilen hintereinander
                notizen.append(n)
        if gerettet and notizen:
            notizen.pop()  # die letzte Notiz einer abgeschnittenen Antwort ist meist selbst unvollständig
        mit_zeit = sum(1 for n in notizen if _zeit_vorn(n) is not None)
        if notizen and not erinnert and mit_zeit < ZEIT_ANTEIL * len(notizen):
            log.info("Szenennotizen: %d von %d ohne Zeitstempel – neu angefordert", len(notizen) - mit_zeit, len(notizen))
            neu = self._notizen(system, vorspann + ERINNERUNG_ZEIT, zeilen, tiefe, erinnert=True)
            if sum(1 for n in neu if _zeit_vorn(n) is not None) >= ZEIT_ANTEIL * max(1, len(neu)):
                return neu  # der zweite Versuch hat schon Rest und Abdeckung geprüft
            return notizen if len(notizen) >= len(neu) else neu
        # Deckt das Ergebnis den Abschnitt ab? Zeit der letzten Notiz gegen die Zeit der letzten Zeile.
        zeiten = [t for t in (_zeit_vorn(z) for z in zeilen) if t is not None]
        bis = max((t for t in (_zeit_vorn(n) for n in notizen) if t is not None), default=None)
        if zeiten and tiefe < 3 and (gerettet or bis is None or bis < zeiten[0] + ABDECKUNG * (zeiten[-1] - zeiten[0])):
            ab = 0 if bis is None else next((i for i, z in enumerate(zeilen)
                                              if (_zeit_vorn(z) or 0) > bis), len(zeilen))
            if gerettet and bis is None:
                ab = len(zeilen) // 2
            rest = zeilen[ab:]
            if 8 <= len(rest) < len(zeilen):
                log.info("Szenennotizen: Rest des Abschnitts (%d Zeilen) wird nachgeholt", len(rest))
                if bis is not None:
                    notizen = [n for n in notizen if _zeit_vorn(n) is not None]
                notizen += self._notizen(system, vorspann, rest, tiefe + 1)
        return notizen

    def gegliedert(self, notizen: str) -> str:
        """Lange Runden ohne Zwischenzusammenfassung: die Notizen selbst, in feste Zeitabschnitte gegliedert
        („Abschnitt 2 von 5 (30:00–1:00:00)“). Alles bleibt erhalten, was die Notizen festgehalten haben; die
        Gliederung sorgt dafür, dass der Recap jeden Abschnitt sieht und gleich behandelt."""
        teile = _teile_nach_zeit([z for z in notizen.split("\n") if z.strip()])
        if len(teile) < 2:
            return notizen
        aus = []
        for i, teil in enumerate(teile):
            zeiten = re.findall(r"^\[(\d{1,2}(?::\d{2}){1,2})\]", teil, re.M)
            von_bis = f" ({zeiten[0]}–{zeiten[-1]})" if zeiten else ""
            aus.append(f"Abschnitt {i + 1} von {len(teile)}{von_bis}:\n{teil}")
        return "\n\n".join(aus)

    def verlauf(self, ein: dict, notizen: str) -> str:
        """Lange Runden: die Notizen in Teile gliedern und jeden Teil einzeln zusammenfassen. Der Recap bekommt diese
        Gliederung, damit kein Teil der Runde untergeht (kleine Modelle erzählen sonst vor allem den Anfang)."""
        teile = _teile_nach_zeit([z for z in notizen.split("\n") if z.strip()])
        if len(teile) < 2:
            return ""
        system = SYSTEM_TEIL.replace("{sprache}", _sprache(ein))
        kopf = _kopf(ein)
        aus = []
        for i, teil in enumerate(teile):
            zeiten = re.findall(r"^\[(\d{1,2}(?::\d{2}){1,2})\]", teil, re.M)
            von_bis = f" ({zeiten[0]}–{zeiten[-1]})" if zeiten else ""
            try:
                d = self.zaehler.aufruf(self.klient, system, f"{kopf}\n\nTeil {i + 1} von {len(teile)}{von_bis}:\n{teil}")
                text = klartext(d.get("zusammenfassung") or d.get("summary") or "")
            except AntwortFehler:
                text = ""
            if not text:
                return ""  # lieber ohne Gliederung als mit Lücke
            aus.append(f"Teil {i + 1} von {len(teile)}{von_bis}:\n{text}")
        return "\n\n".join(aus)

    def grenzen(self, ein: dict) -> tuple[int, int]:
        """Längenvorgabe (Wörter) des ganzen Kapitels; bei Teilen die Summe der Teile (0.4.78)."""
        if self.letzte_grenzen:
            return self.letzte_grenzen
        return untergrenze(ein, self.notizen_zuerst), obergrenze(ein, self.notizen_zuerst)

    def recap(self, ein: dict, titel: str, grundlage: str, grenzen: tuple[int, int] | None = None,
              zusatz: str = "") -> dict:
        bibel = "\n".join(f"- [{e['typ']}] {e['name']}" + (f": {e['zusammenfassung'][:500]}"
                                                           if _erwaehnt(e["name"], grundlage) else "")
                          for e in ein["bibel"])
        nutzer = (f"{_kopf(ein)}\n\nBekannt aus früheren Sessions (Spielerwissen):\n{bibel or '(noch nichts)'}"
                  f"\n\n{titel}:\n{grundlage}")
        unten, oben = grenzen or (untergrenze(ein, self.notizen_zuerst), obergrenze(ein, self.notizen_zuerst))
        if self.notizen_zuerst:  # 0.4.73: Länge vorab planen statt hinterher kürzen – Absatzzahl hält das Modell besser ein
            laenge = (f"Etwa {max(4, round(oben / 130))} Absätze mit zusammen {unten} bis {oben} Wörtern – nicht mehr als "
                      f"{oben} –, je Absatz 80 bis 180 Wörter")
        else:
            laenge = f"{unten}–{oben} Wörter, in Absätzen von je 80 bis 180 Wörtern"
        system = (SYSTEM_RECAP.replace("{sprache}", _sprache(ein)).replace("{nummer}", str(ein["session_nummer"]))
                  .replace("{laenge}", laenge))
        if zusatz:  # 0.4.78: Teile im Kapitel – als weitere Regel vor der Antwortform
            system = system.replace("\nAntworte nur mit JSON", zusatz + "\nAntworte nur mit JSON", 1)
        self._schritt("recap")
        r: dict | None = None
        for versuch in range(2):
            # 0.4.78: Ein Entwurf weit unter der Untergrenze ist kein Kapitel (gemessen: nur die Einleitung, danach machte
            # die Nachbesserung aus 30 fehlenden Ereignissen eine Aufzählung). Einmal neu anfordern, das Längere zählt.
            extra = "" if not versuch else (
                f"\n\nDein erster Entwurf hatte nur {len(r['text'].split())} Wörter. Schreib das ganze Kapitel mit "
                f"{unten} bis {oben} Wörtern, alle Abschnitte in ihrer Reihenfolge, als Fließtext im Feld „text“.")
            d = self.zaehler.aufruf(self.klient, system, nutzer + extra)
            neu = self._recap_lesen(d)
            if r is None or len(neu["text"].split()) > len(r["text"].split()):
                r = neu
            if len(r["text"].split()) >= unten * KURZER_ENTWURF or _minuten(ein) <= AUSWAHL_ABSCHNITT_AB:
                break  # kurze Runden dürfen kurz sein; zusammenfallen kann ein Entwurf erst mit vielen Abschnitten
            self.letzter_kurzer_entwurf.append({"woerter": len(neu["text"].split()), "untergrenze": unten,
                                                "antwort": (self.zaehler.letzte_antwort or "")[:ROHANTWORT_ZEICHEN]})
        return r

    def _recap_lesen(self, d: dict) -> dict:
        text = recap_text(d)
        if not text:
            # Nur Schlüssel und Längen ins Protokoll – nie Inhalte
            form = ", ".join(f"{k}:{_form(v)}" for k, v in d.items()) or "leer"
            log.warning("Sprachmodell: Recap-Antwort ohne text (%s)", form)
            raise SprachmodellFehler(f"Das Sprachmodell hat keinen Recap geliefert (Antwort: {form}).")
        faeden = [klartext(f)[:300] for f in (d.get("openThreads") or d.get("open_threads") or []) if klartext(f)][:10]
        text = re.sub(r"\s+([,.;:!?])", r"\1", _UNKLAR.sub("", klartext(text)))  # die Marke gehört nicht ins Kapitel
        if "\n\n" not in text and "\n" in text:  # Absätze nur mit einfachem Umbruch – für App und Prüfung trennen
            text = re.sub(r"\n+", "\n\n", text)
        text = absaetze_teilen(text)
        return {"title": titel_saeubern(klartext(d.get("title") or d.get("titel"))[:300]), "text": text,
                "openThreads": faeden}

    def vollstaendigkeit(self, ein: dict, titel: str, grundlage: str, text: str) -> list[dict]:
        """Was fehlt im Recap, obwohl es in der Grundlage steht? Liefert [{zeit, notiz, belegt}]; nur belegte Punkte
        (in der Grundlage wiedergefunden) werden ergänzt."""
        system = (SYSTEM_VOLLSTAENDIGKEIT.replace("{sprache}", _sprache(ein))
                  .replace("{hoechstens}", str(FEHLEND_HOECHSTENS)))
        nutzer = f"{_kopf(ein)}\n\n{titel}:\n{grundlage}\n\nRecap:\n{text}"
        self._schritt("review")
        aus = fehlend_lesen(self.zaehler.aufruf(self.klient, system, nutzer), grundlage)
        return [f for f in aus if not _schon_erzaehlt(f["notiz"], text)]

    def ergaenzen(self, ein: dict, text: str, fehlend: list[dict]) -> str | None:
        """Genau eine Ergänzung um belegte fehlende Ereignisse. Nur geänderte Absätze kommen zurück; ein Absatz wird
        nur übernommen, wenn er nicht kürzer geworden ist (die Anweisung lautet: nichts streichen)."""
        punkte = [f for f in fehlend if f.get("belegt") and f.get("wichtigkeit", "wichtig") in ERGAENZEN_RAENGE]
        if not punkte:
            return None
        teile = absaetze(text)

        def zeile(f: dict) -> str:
            z = f"- [{_zeit(f['zeit']) if f.get('zeit') is not None else '?'}] {f['notiz']}"
            if f.get("davor") or f.get("danach"):
                z += f"\n  (steht zwischen: {f.get('davor') or '(Anfang)'} – und – {f.get('danach') or '(Ende)'})"
            return z

        liste = "\n".join(zeile(f) for f in punkte)
        nummeriert = "\n\n".join(f"Absatz {i + 1}:\n{a}" for i, a in enumerate(teile))
        nutzer = f"{_kopf(ein)}\n\nFehlende Ereignisse:\n{liste}\n\nRecap, Absatz für Absatz:\n{nummeriert}"
        self._schritt("revision")
        d = self.zaehler.aufruf(self.klient, SYSTEM_ERGAENZUNG.replace("{sprache}", _sprache(ein)), nutzer)
        neu = {}
        for a in d.get("absaetze") or d.get("paragraphs") or []:
            try:
                nr = int(a.get("nr") or a.get("index") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            t = klartext(a.get("text")) if isinstance(a.get("text"), str) else ""
            if not (1 <= nr <= len(teile)) or len(t) < len(teile[nr - 1]):
                continue  # gekürzt oder unbekannter Absatz
            if _rohe_notiz(t, teile[nr - 1], [f["notiz"] for f in punkte]):
                continue  # Notiz abgeschrieben statt erzählt
            alt = _kennwoerter(teile[nr - 1])
            # Deterministische Absicherung: Der alte Absatz muss die Nachbarn eines eingefügten Ereignisses erzählen,
            # und der neue Text muss das Ereignis enthalten – sonst stünde es an der falschen Stelle.
            passend = False
            for f in punkte:
                nachbarn = _kennwoerter(f.get("davor", "")) | _kennwoerter(f.get("danach", ""))
                if nachbarn and not (nachbarn & alt):
                    continue
                if _kennwoerter(f["notiz"]) & (_kennwoerter(t) - alt):
                    f["ergaenzt"], passend = True, True
            if passend:
                neu[nr - 1] = t
        if not neu:
            return None
        return "\n\n".join(neu.get(i, t) for i, t in enumerate(teile))

    def auswahl(self, ein: dict, notizen: str, hoechstens: int | None = None, von: float = 0.0,
                bis: float | None = None) -> list[dict]:
        """0.4.65: die entscheidenden Ereignisse mit Ausgang, bevor das Kapitel geschrieben wird. 0.4.78: auch für einen
        Teil der Runde (von/bis in Sekunden); jeder Abschnitt sieht den Stand nur bis zu seinem Ende."""
        ende = _minuten(ein) * 60
        bis = ende if bis is None else bis
        hoechstens = hoechstens or auswahl_hoechstens(ein)
        abschnitte = auswahl_abschnitte(notizen, (bis - von) / 60, hoechstens, start=von)
        self._schritt("recap")
        aus, gesehen = [], set()
        for a in abschnitte:
            zusatz = "" if a["n"] == 1 else (AUSWAHL_ABSCHNITT.replace("{k}", str(a["k"])).replace("{n}", str(a["n"]))
                                             .replace("{von}", _zeit(a["von"])).replace("{bis}", _zeit(a["bis"])))
            system = (SYSTEM_AUSWAHL.replace("{sprache}", _sprache(ein)).replace("{hoechstens}", str(a["anteil"]))
                      .replace("{abschnitt}", zusatz))
            am_ende = a["bis"] >= ende - 60
            stand = "\n".join(f"- {s}" for s in (self.letzter_stand if am_ende
                                                  else stand_bis(self.letzter_stand, a["bis"]))) or "(kein Stand)"
            stand_titel = ("Stand am Ende der Runde" if am_ende else f"Stand bis {_zeit(a['bis'])}")
            nutzer = (f"{_kopf(ein)}\n\nSzenennotizen{' dieses Abschnitts' if a['n'] > 1 else ' der Runde'}:\n"
                      f"{a['notizen']}\n\n{stand_titel} (mit dem Zeitpunkt der Änderung):\n{stand}")
            for e in auswahl_lesen(self.zaehler.aufruf(self.notiz_klient or self.klient, system, nutzer), a["anteil"]):
                if e["ereignis"].casefold() not in gesehen:
                    gesehen.add(e["ereignis"].casefold())
                    aus.append(e)
        aus.sort(key=lambda e: (e["zeit"] is None, e["zeit"] or 0.0))
        for e in aus:
            if _UNKLAR.search(e["ereignis"] + " " + e["ausgang"]):
                e["unklar"] = True
        return aus

    def einschnitte(self, ein: dict, notizen: str) -> list[dict]:
        """0.4.78: Natürliche Einschnitte einer langen Runde → Teile [{von, bis, titel}] oder [] (dann keine Teile)."""
        dauer = _minuten(ein) * 60
        if dauer < TEILE_AB_MIN * 60 or not notizen.strip():
            return []
        anzahl = 2 if dauer >= TEILE_ZWEI_AB_MIN * 60 else 1
        system = (SYSTEM_EINSCHNITT.replace("{sprache}", _sprache(ein)).replace("{anzahl}", str(anzahl))
                  .replace("{von}", _zeit(dauer * TEILE_FENSTER[0])).replace("{bis}", _zeit(dauer * TEILE_FENSTER[1])))
        self._schritt("recap")
        d = self.zaehler.aufruf(self.notiz_klient or self.klient, system, f"{_kopf(ein)}\n\nSzenennotizen:\n{notizen}")
        teile = einschnitte_lesen(d, notizen, dauer, anzahl, "en" if ein.get("sprache") == "en" else "de")
        self.letzte_einschnitte = {"antwort": d, "teile": teile}
        return teile

    def zweiter_blick(self, ein: dict, punkte: list[dict]) -> list[dict]:
        """0.4.74: Kritische und unklare Ereignisse der Auswahl gegen die Abschrift rund um ihren Zeitpunkt halten.
        Ändert `punkte` an Ort und Stelle (berichtigt oder als unklar markiert) und liefert das Protokoll."""
        zeilen = [(_zeit_vorn(z), z) for z in transkript_zeilen(ein.get("transkript") or [])]
        zeilen = [(t, z) for t, z in zeilen if t is not None]
        kandidaten = [i for i, p in enumerate(punkte) if p.get("zeit") is not None and (p.get("unklar") or p["rang"] == "kritisch")]
        kandidaten.sort(key=lambda i: (not punkte[i].get("unklar"), punkte[i]["zeit"]))
        kandidaten = sorted(kandidaten[:ZWEITER_BLICK_HOECHSTENS])
        fenster = {i: _fenster(zeilen, punkte[i]["zeit"]) for i in kandidaten}
        kandidaten = [i for i in kandidaten if fenster[i].strip()]
        if not kandidaten:
            return []
        self._schritt("review")
        protokoll: list[dict] = []
        for start in range(0, len(kandidaten), ZWEITER_BLICK_JE_AUFRUF):
            gruppe = kandidaten[start:start + ZWEITER_BLICK_JE_AUFRUF]
            bloecke = []
            for nr, i in enumerate(gruppe, 1):
                p = punkte[i]
                bloecke.append(f"Ereignis {nr} [{_zeit(p['zeit'])}]: {p['ereignis']}"
                               + (f" – Ausgang: {p['ausgang']}" if p["ausgang"] else "")
                               + f"\nAbschrift dazu:\n{fenster[i]}")
            d = self.zaehler.aufruf(self.klient, SYSTEM_ZWEITER_BLICK.replace("{sprache}", _sprache(ein)),
                                    f"{_kopf(ein)}\n\n" + "\n\n---\n\n".join(bloecke))
            antworten = {}
            for a in d.get("ereignisse") or d.get("events") or []:
                if isinstance(a, dict):
                    try:
                        antworten.setdefault(int(a.get("nr") or 0), a)
                    except (TypeError, ValueError):
                        continue
            for nr, i in enumerate(gruppe, 1):
                p, a = punkte[i], antworten.get(nr) or {}
                # ohne Antwort oder mit unbekanntem Urteil bleibt das Ereignis, wie es war („ungeprüft“) – nur ein
                # ausdrückliches „unklar“ macht es unklar
                urteil = _URTEIL_BLICK.get(str(a.get("urteil") or a.get("verdict") or "").strip().lower(), "ungeprueft")
                zitat = klartext(a.get("zitat") or a.get("quote") or "")[:300] if isinstance(a.get("zitat") or a.get("quote"), str) else ""
                grund = klartext(a.get("grund") or a.get("reason") or "")[:300]
                eintrag = {"nr": i + 1, "zeit": p["zeit"], "zeit_text": _zeit(p["zeit"]), "urteil": urteil,
                           "vorher": f"{p['ereignis']}" + (f" – {p['ausgang']}" if p["ausgang"] else ""),
                           "nachher": "", "zitat": "", "grund": grund}
                if urteil == "berichtigt":
                    neu_e = regelreste(klartext(a.get("ereignis") or a.get("event") or ""))[:300]
                    neu_a = regelreste(klartext(a.get("ausgang") or a.get("outcome") or ""))[:300]
                    if neu_e and zitat and zitat_belegt(zitat, _normwoerter(fenster[i])):
                        p["ereignis"], p["ausgang"], p["berichtigt"] = neu_e, neu_a, True
                        eintrag.update(nachher=f"{neu_e}" + (f" – {neu_a}" if neu_a else ""), zitat=zitat)
                    else:  # Berichtigung ohne auffindbaren Beleg: dann lieber unklar als neu falsch
                        urteil = eintrag["urteil"] = "unklar"
                        eintrag["grund"] = grund or "Beleg in der Abschrift nicht gefunden."
                if urteil == "unklar" and not p.get("unklar"):
                    p["unklar"] = True
                    p["ausgang"] = (p["ausgang"] + " " if p["ausgang"] else "") + f"(unklar: {eintrag['grund'] or 'wer'})"
                    self.letzte_unklar.append({"zeit": p["zeit"], "zeit_text": _zeit(p["zeit"]),
                                               "notiz": f"{p['ereignis']} – {eintrag['grund'] or 'unklar, wer'}",
                                               "quelle": "zweiter_blick"})
                protokoll.append(eintrag)
        self.letzte_unklar.sort(key=lambda u: (u.get("zeit") is None, u.get("zeit") or 0.0))
        return protokoll

    def pflicht_pruefen(self, ein: dict, punkte: list[dict], text: str) -> list[dict]:
        teile = absaetze(text)
        nutzer = (f"Ereignisse, die vorkommen müssen:\n{auswahl_text(punkte)}\n\nKapitel, Absatz für Absatz:\n"
                  + "\n\n".join(f"Absatz {i + 1}:\n{a}" for i, a in enumerate(teile)))
        self._schritt("review")
        return pflicht_lesen(self.zaehler.aufruf(self.klient, SYSTEM_PFLICHT.replace("{sprache}", _sprache(ein)), nutzer),
                             len(punkte), len(teile), text, "en" if ein.get("sprache") == "en" else "de", punkte)

    def pflicht_nachbessern(self, ein: dict, punkte: list[dict], befund: list[dict], text: str) -> str | None:
        """Genau eine Nachbesserung, nur für Absätze mit fehlenden oder falsch erzählten Pflichtpunkten. Ein Absatz
        bleibt ein Absatz; fehlt etwas, darf er nicht kürzer werden, sonst höchstens um ein Drittel."""
        teile = absaetze(text)
        je_absatz: dict[int, list[str]] = {}
        for b in befund:
            if b["status"] in ("fehlt", "widerspricht") and b["absatz"] is not None:
                p = punkte[b["nr"] - 1]
                art = "fehlt" if b["status"] == "fehlt" else "falsch erzählt"
                je_absatz.setdefault(b["absatz"], []).append(
                    f"- {art}: {p['ereignis']}" + (f" – richtiger Ausgang: {p['ausgang']}" if p["ausgang"] else ""))
        if not je_absatz:
            return None
        liste = "\n\n".join(f"Absatz {i + 1}:\n{teile[i]}\nZu tun:\n" + "\n".join(je_absatz[i])
                              for i in sorted(je_absatz))
        self._schritt("revision")
        d = self.zaehler.aufruf(self.klient, SYSTEM_PFLICHT_NACH.replace("{sprache}", _sprache(ein)),
                                f"{_kopf(ein)}\n\n{liste}")
        neu = {}
        for a in d.get("absaetze") or d.get("paragraphs") or []:
            try:
                nr = int(a.get("nr") or a.get("index") or 0) - 1
            except (TypeError, ValueError, AttributeError):
                continue
            t = " ".join(klartext(a.get("text")).split()) if isinstance(a.get("text"), str) else ""
            if nr not in je_absatz or not t:
                continue
            nur_fehlend = all(z.startswith("- fehlt") for z in je_absatz[nr])
            if len(t) < (len(teile[nr]) if nur_fehlend else len(teile[nr]) * 2 / 3):
                continue
            neu[nr] = t
        if not neu:
            return None
        # 0.4.78: Ein Absatz bleibt beim Umschreiben ein Absatz – wird er dabei sehr lang (viele fehlende Ereignisse an
        # einer Stelle), wird er an Satzgrenzen geteilt wie jeder andere, statt als Block im Kapitel zu stehen.
        return absaetze_teilen("\n\n".join(neu.get(i, t) for i, t in enumerate(teile)))

    def pflicht(self, ein: dict, punkte: list[dict], r: dict) -> list[dict]:
        """Kapitel gegen die Pflichtpunkte prüfen, einmal nachbessern, noch einmal prüfen. Liefert den letzten Befund."""
        vorher = self.pflicht_pruefen(ein, punkte, r["text"])
        self.letzte_pflicht = {"vorher": vorher, "nachher": [], "nachgebessert": False}
        neu = self.pflicht_nachbessern(ein, punkte, vorher, r["text"])
        if not neu:
            return vorher
        r["text"], self.letzte_pflicht["nachgebessert"] = neu, True
        nachher = self.pflicht_pruefen(ein, punkte, neu)
        self.letzte_pflicht["nachher"] = nachher
        return nachher

    def kuerzen(self, ein: dict, punkte: list[dict], r: dict, befund: list[dict]) -> list[dict] | None:
        """0.4.68: Kapitel weit über der Längenvorgabe kürzen. Übernommen nur, wenn alle bisher erzählten Pflichtpunkte
        erzählt bleiben, kein neuer Widerspruch dazukommt und kein Name fehlt. Liefert den neuen Befund oder None."""
        n = len(r["text"].split())
        unten, grenze = self.grenzen(ein)
        self.letzte_kuerzung = {"woerter": n, "grenze": grenze, "angenommen": False}
        if n <= grenze * KUERZEN_AB:
            self.letzte_kuerzung["grund"] = "nicht zu lang"
            return None
        nutzer = (f"Ereignisse, die vorkommen müssen:\n{auswahl_text(punkte)}\n\nKapitel:\n{r['text']}")
        system = (SYSTEM_KUERZEN.replace("{sprache}", _sprache(ein)).replace("{ziel}", str(grenze))
                  .replace("{unten}", str(unten)))
        self._schritt("revision")
        t, m = "", 0
        for versuch in range(2):  # 0.4.76: zu kurz geraten → einmal nachfordern, mit der Zahl
            zusatz = "" if not versuch else (f"\n\nDeine erste Kürzung hatte nur {m} Wörter. Schreib mindestens {unten} "
                                             f"Wörter; streiche nur Ausschmückung.")
            d = self.zaehler.aufruf(self.klient, system, nutzer + zusatz)
            t = klartext(d.get("text") if isinstance(d.get("text"), str) else "")
            if "\n\n" not in t and "\n" in t:
                t = re.sub(r"\n+", "\n\n", t)
            t = absaetze_teilen(t) if t else ""
            m = len(t.split())
            self.letzte_kuerzung["woerter_neu"] = m
            if not t or m >= grenze * 0.5:
                break
            self.letzte_kuerzung["zu_kurz"] = m
        if not t or m > n * (1 - KUERZEN_MINDESTENS) or m < grenze * 0.5:
            self.letzte_kuerzung["grund"] = "Länge passt nicht"
            return None
        extra = [p.get("charakter") or "" for p in ein.get("personen") or []] + \
                [e.get("name") or "" for e in ein.get("bibel") or []]
        fehlen = _namen_fehlen(r["text"], t, extra, auswahl_text(punkte))
        if fehlen:
            self.letzte_kuerzung["grund"] = "Namen fehlen: " + ", ".join(fehlen[:10])
            return None
        neu = self.pflicht_pruefen(ein, punkte, t)
        alt_erz = {b["nr"] for b in befund if b["status"] == "erzaehlt"}
        neu_erz = {b["nr"] for b in neu if b["status"] == "erzaehlt"}
        alt_wid = {b["nr"] for b in befund if b["status"] == "widerspricht"}
        neu_wid = {b["nr"] for b in neu if b["status"] == "widerspricht"}
        if not alt_erz <= neu_erz or not neu_wid <= alt_wid:
            self.letzte_kuerzung["grund"] = ("Pflichtpunkte verloren: "
                                             + ", ".join(str(x) for x in sorted((alt_erz - neu_erz) | (neu_wid - alt_wid))))
            return None
        self.letztes_lang, r["text"] = r["text"], t
        self.letzte_kuerzung.update(angenommen=True, grund="")
        return neu

    def korrigieren(self, ein: dict, text: str, hinweise: dict[int, str], frei: str = "", notizen: str = ""
                    ) -> tuple[str, dict]:
        """0.4.69: Ein Durchgang „Korrektur per Hinweis“. hinweise: {Absatz-Index (0-basiert): Text der SL}. Alle
        anderen Absätze bleiben zeichengleich (sie werden nie neu zusammengesetzt). Liefert (Text, Bericht)."""
        from app import artefakte

        teile = absaetze(text)
        hinweise = {i: h.strip() for i, h in hinweise.items() if 0 <= i < len(teile) and h.strip()}
        frei = frei.strip()
        frei_liste = hinweis_liste(frei) or ([frei] if frei else [])
        bericht = {"anzahl": len(hinweise) + len(frei_liste), "geaendert": [], "verworfen": [],
                   "ohne_aenderung": [], "pruefliste": []}
        if not hinweise and not frei:
            return text, bericht
        liste = [f"- Absatz {i + 1}: {h}" for i, h in sorted(hinweise.items())]
        liste += [f"- H{n} (ohne Absatzangabe): {h}" for n, h in enumerate(frei_liste, 1)]
        grund = (f"Szenennotizen (nur zur Orientierung; bei Widerspruch gilt der Hinweis):\n"
                 f"{notizen[:KORREKTUR_NOTIZEN_ZEICHEN]}\n\n") if notizen.strip() else ""
        nutzer = (f"{_kopf(ein)}\n\n{grund}Kapitel, Absatz für Absatz:\n"
                  + "\n\n".join(f"Absatz {i + 1}:\n{a}" for i, a in enumerate(teile))
                  + "\n\nHinweise der Spielleitung:\n" + "\n".join(liste))
        self._schritt("revision")
        d = self.zaehler.aufruf(self.klient, SYSTEM_KORREKTUR.replace("{sprache}", _sprache(ein)), nutzer)
        neu, bericht["verworfen"] = korrektur_anwenden(d, teile, hinweise, frei)
        geschuetzt = [e["name"] for e in ein.get("bibel") or []]
        for i, t in list(neu.items()):
            sauber, _befunde = artefakte.kapitel(t, ein.get("personen") or [], geschuetzt)
            sauber = " ".join(sauber.split())
            if not sauber:
                bericht["verworfen"].append({"absatz": i + 1, "grund": "vom Filter geleert"})
                del neu[i]
            else:
                neu[i] = sauber
        bericht["geaendert"] = [i + 1 for i in sorted(neu)]
        bericht["ohne_aenderung"] = [i + 1 for i in sorted(hinweise) if i not in neu]
        bericht["pruefliste"] = pruefliste(hinweise, frei_liste, hinweis_zuordnung(d, len(frei_liste)), teile, neu)
        return "\n\n".join(neu.get(i, a) for i, a in enumerate(teile)), bericht

    def relationen(self, ein: dict, text: str, befund: list[dict]) -> list[dict]:
        """Beziehungen je Absatz gegen kurze Ausschnitte des Originaltranskripts prüfen – um die Stellen, die die
        Gegenprüfung belegt hat. Szenennotizen können selbst schon falsch sein; das Transkript nicht. Liefert je
        Absatz {index, urteil, begruendung, zitat, fenster} und setzt im Befund "contradicted", wo es widerspricht."""
        zeilen = [(_zeit_vorn(z), z) for z in transkript_zeilen(ein.get("transkript") or [])]
        zeilen = [(t, z) for t, z in zeilen if t is not None]
        teile = absaetze(text)
        bloecke, fenster = [], {}
        for b in befund:
            i = b.get("index", -1)
            zeiten = sorted({e["start"] for e in b.get("evidence") or [] if e.get("start") is not None})[:3]
            if not (0 <= i < len(teile)) or not zeiten:
                continue
            spans = [z for t, z in zeilen if any(abs(t - zt) <= RELATION_FENSTER_S for zt in zeiten)]
            text_spans = "\n".join(spans)[:RELATION_ZEICHEN]
            if not text_spans.strip():
                continue
            fenster[i] = [_zeit(zt) for zt in zeiten]
            bloecke.append(f"Absatz {i + 1}:\n{teile[i]}\n\nTranskript dazu ({', '.join(fenster[i])}):\n{text_spans}")
        if not bloecke:
            return []
        self._schritt("review")
        d = self.zaehler.aufruf(self.klient, SYSTEM_RELATIONEN.replace("{sprache}", _sprache(ein)),
                                f"{_kopf(ein)}\n\n" + "\n\n---\n\n".join(bloecke))
        aus = []
        for a in d.get("absaetze") or d.get("paragraphs") or []:
            if not isinstance(a, dict):
                continue
            try:
                nr = int(a.get("nr") or a.get("index") or 0)
            except (TypeError, ValueError):
                continue
            if not (1 <= nr <= len(teile)) or (nr - 1) not in fenster:
                continue
            urteil = str(a.get("urteil") or a.get("verdict") or "").strip().lower()
            urteil = {"stimmt": "stimmt", "ok": "stimmt", "supported": "stimmt", "widerspricht": "widerspricht",
                      "contradicted": "widerspricht", "contradiction": "widerspricht"}.get(urteil, "unklar")
            eintrag = {"index": nr - 1, "urteil": urteil, "begruendung": klartext(a.get("begruendung") or "")[:400],
                       "zitat": klartext(a.get("zitat") or "")[:300], "fenster": fenster[nr - 1]}
            aus.append(eintrag)
            if urteil == "widerspricht" and eintrag["begruendung"]:
                # Nur ein Hinweis für die Spielleitung, kein Urteil: Die Relationsprüfung schlägt bei kleinen
                # Modellen überwiegend falsch an (06.10.: 1 echter Treffer in 12 Flaggen). Das Urteil des Absatzes
                # und damit die Nachbesserung bleiben unberührt. 0.4.64: Die App zeigt Begründungen nur an
                # beanstandeten Absätzen, und dort genau einen Satz – der Hinweis füllt also nur eine leere
                # Begründung; im Modellvergleich bleibt er vollständig (letzte_relationen_*).
                for b in befund:
                    if b.get("index") == nr - 1 and b.get("verdict") in BEANSTANDET and not b.get("note"):
                        b["note"] = HINWEIS_RELATION["en" if ein.get("sprache") == "en" else "de"] + eintrag["begruendung"]
        return aus

    def pruefen(self, ein: dict, titel: str, grundlage: str, text: str) -> list[dict]:
        """Gegenprüfung (Stufe 3): jeden Absatz gegen die Grundlage bewerten – ein eigener Aufruf, der den Recap
        nicht geschrieben hat und nichts umschreibt."""
        teile = absaetze(text)
        if not teile:
            return []
        liste = "\n\n".join(f"Absatz {i + 1}:\n{a}" for i, a in enumerate(teile))
        nutzer = f"{_kopf(ein)}\n\n{titel}:\n{grundlage}\n\nRecap, Absatz für Absatz:\n{liste}"
        system = SYSTEM_PRUEFUNG.replace("{sprache}", _sprache(ein))
        return pruefung_lesen(self.zaehler.aufruf(self.klient, system, nutzer), len(teile))

    def nachbessern(self, ein: dict, titel: str, grundlage: str, text: str, befund: list[dict]) -> str | None:
        """Beanstandete Absätze einmal neu schreiben lassen. None, wenn nichts zu tun war oder nichts kam."""
        teile = absaetze(text)
        schlecht = [b for b in befund if b["verdict"] in BEANSTANDET and b["index"] < len(teile)]
        if not schlecht:
            return None
        liste = "\n\n".join(f"Absatz {b['index'] + 1} (Grund: {b['note'] or b['verdict']}):\n{teile[b['index']]}"
                             for b in schlecht)
        nutzer = f"{_kopf(ein)}\n\n{titel}:\n{grundlage}\n\nBeanstandete Absätze:\n{liste}"
        system = SYSTEM_NACHBESSERUNG.replace("{sprache}", _sprache(ein))
        d = self.zaehler.aufruf(self.klient, system, nutzer)
        neu = {}
        for a in d.get("absaetze") or d.get("paragraphs") or []:
            try:
                nr = int(a.get("nr") or a.get("index") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if any(b["index"] == nr - 1 for b in schlecht) and isinstance(a.get("text"), str):
                neu[nr - 1] = klartext(a["text"])
        if not neu:
            return None
        return "\n\n".join(neu.get(i, t) for i, t in enumerate(teile) if neu.get(i, t).strip())

    def gegenpruefen(self, ein: dict, titel: str, grundlage: str, r: dict) -> dict:
        """Prüfung mit höchstens einer Nachbesserung. Ändert r["text"], wenn nachgebessert wurde. Scheitert das
        Sprachmodell hier, bleibt der Recap wie er ist – die Absätze gelten dann als ungeprüft."""
        pruefung = {"model": self.klient.modell, "revised": False, "paragraphs": []}
        try:
            self._schritt("review")
            befund = spielleitung_beanstanden(self.pruefen(ein, titel, grundlage, r["text"]), r["text"])
            self.letzte_pruefung_vorher, self.letzte_pruefung_nachher = befund, []
            self.letzte_relationen_vorher, self.letzte_relationen_nachher = [], []
            if self.nachbesserung and any(b["verdict"] in BEANSTANDET for b in befund):
                self._schritt("revision")
                neu = self.nachbessern(ein, titel, grundlage, r["text"], befund)
                if neu:
                    r["text"], pruefung["revised"] = neu, True
                    self._schritt("review")
                    befund = spielleitung_beanstanden(self.pruefen(ein, titel, grundlage, neu), neu)
                    self.letzte_pruefung_nachher = befund
            # Relationen einmal, auf der Endfassung: nur Hinweise für die Spielleitung, keine Nachbesserung daraus
            self.letzte_relationen_nachher = self.relationen(ein, r["text"], befund)
            pruefung["paragraphs"] = befund
        except SprachmodellFehler as e:
            log.warning("Gegenprüfung übersprungen: %s", e)
            # Für die Fehlersuche im Modellvergleich: Grund und Anfang der letzten Modellantwort (bleibt lokal)
            self.warnungen.append({"schritt": "review", "fehler": str(e),
                                   "antwort": (self.zaehler.letzte_antwort or "")[:3000]})
            pruefung["paragraphs"] = [{"index": i, "verdict": "unchecked", "note": None, "evidence": []}
                                      for i in range(len(absaetze(r["text"])))]
        return pruefung

    def vorschlaege(self, ein: dict, titel: str, grundlage: str) -> list[dict]:
        def eintrag(e: dict) -> str:
            z = f"- id={e['id']} [{e['typ']}] {e['name']}"
            if not _erwaehnt(e["name"], grundlage):
                return z  # nur der Name – spart Platz; Inhalt nur für Einträge, die vorkommen
            z += f": {e.get('zusammenfassung', '')[:600]}"
            if e.get("gm_notes"):
                z += f"\n  gmNotes: {e['gm_notes'][:600]}"
            return z

        bibel = "\n".join(eintrag(e) for e in ein["bibel"])
        geheim = "\n".join(eintrag(e) for e in ein["geheim"])
        nutzer = (f"{_kopf(ein)}\n\nBibel (für Spieler sichtbar; gmNotes sind geheim):\n{bibel or '(leer)'}"
                  f"\n\nGeheime Einträge (nur Spielleitung oder nur einzelne Spieler kennen sie):\n{geheim or '(keine)'}\n\n{titel}:\n{grundlage}")
        system = (SYSTEM_VORSCHLAEGE.replace("{sprache}", _sprache(ein)).replace("{max}", str(MAX_VORSCHLAEGE_ROH)))
        self._schritt("proposals")
        d = self.zaehler.aufruf(self.vorschlag_klient or self.klient, system, nutzer)
        return pruefen(d.get("proposals") or [], {e["id"] for e in ein["bibel"]}, {e["id"] for e in ein["geheim"]},
                       charaktere=[p["charakter"] for p in ein["personen"] if p.get("charakter")],
                       namen={e["id"]: e["name"] for e in ein["bibel"] + ein["geheim"]}, grundlage=grundlage)

    def _vorschlag_grundlage(self, titel: str, grundlage: str) -> tuple[str, str]:
        """Weg „Notizen zuerst“: Die Vorschläge sehen weiter die ganze Abschrift, wenn sie ins Modell passt – dort
        waren sie gemessen gut (Large Ø 8,8/10); nur das Kapitel entsteht aus den Notizen."""
        if self.notizen_zuerst and self.letztes_transkript and tokens(self.letztes_transkript) <= self.max_transkript_tokens:
            return "Transkript", self.letztes_transkript
        return titel, grundlage

    def _recap_in_teilen(self, ein: dict, notizen: str, teile: list[dict]) -> dict:
        """0.4.78: Ein Kapitel aus Teilen. Jeder Teil wird für sich geschrieben – mit seinen Notizen, dem Stand bis zu
        seinem Ende, seinen Pflichtereignissen und eigenem Platz; ab Teil 2 als Fortsetzung mit dem letzten Absatz davor
        zum Anknüpfen. Titel des Kapitels aus Teil 1, offene Fäden aus dem letzten Teil."""
        stuecke, anfaenge, faeden, titel = [], [], [], ""
        summe = [0, 0]
        n = len(teile)
        for i, t in enumerate(teile):
            unten, oben = teil_grenzen(t)
            summe[0] += unten
            summe[1] += oben
            letzter = i == n - 1
            zeilen = self.letzter_stand if letzter else stand_bis(self.letzter_stand, t["bis"])
            stand = (f"\n\n{'Stand am Ende der Runde' if letzter else 'Stand bis ' + _zeit(t['bis'])} (Zustände, Besitz, "
                     f"Beziehungen, Abmachungen – mit dem Zeitpunkt der Änderung):\n"
                     + "\n".join(f"- {s}" for s in zeilen)) if zeilen else ""
            pflicht = (PFLICHT_VORSPANN + auswahl_text(t["auswahl"])) if t.get("auswahl") else ""
            davor = (f"\n\nSo endet der Teil davor (nur zum Anknüpfen, nicht wiederholen):\n{absaetze(stuecke[-1])[-1]}"
                     if stuecke else "")
            zusatz = (TEIL_ANFANG if i == 0 else TEIL_FORTSETZUNG).replace("{k}", str(i + 1)).replace("{n}", str(n))
            if not letzter:
                zusatz += TEIL_OHNE_SCHLUSS
            r = self.recap(ein, f"Szenennotizen von Teil {i + 1} von {n} ({_zeit(t['von'])}–{_zeit(t['bis'])}) in "
                                "Zeitabschnitten (jeder Abschnitt gehört in diesen Teil, in dieser Reihenfolge, mit etwa "
                                "gleich viel Raum; lieber knapper erzählen als ein Ereignis weglassen)",
                           self.gegliedert(teil_notizen(notizen, t["von"], t["bis"])) + stand + pflicht + davor,
                           grenzen=(unten, oben), zusatz=zusatz)
            if i == 0:
                titel = r["title"]
            faeden = r["openThreads"]
            stuecke.append(r["text"])
            anfaenge.append(absaetze(r["text"])[0] if absaetze(r["text"]) else "")
        self.letzte_grenzen = (summe[0], summe[1])
        self._teile_anfang = (anfaenge, [t["titel"] for t in teile])
        return {"title": titel, "text": "\n\n".join(stuecke), "openThreads": faeden}

    def ausfuehren(self, recap_ein: dict, vorschlag_ein: dict,
                   fortschritt: Callable[[float], None] = lambda _p: None, gegenpruefen: bool = False) -> dict:
        """Das ganze Ergebnis. Die Grundlage (Transkript bzw. Notizen) ist für beide gleich; die Notizen entstehen
        aus der Recap-Eingabe, die nichts Geheimes enthält. Die Gegenprüfung sieht nur, was der Recap sah."""
        titel, grundlage = self.grundlage(recap_ein, fortschritt)
        self.letzte_grenzen, self.letzte_teile, self._teile_anfang = None, [], ((), ())
        self.letzter_kurzer_entwurf, self.letzte_einschnitte = [], {}
        verlauf = ""
        if titel.startswith("Szenennotizen"):
            # Die Notizen passen fast immer in den Kontext (grundlage() verdichtet so lange). Dann bekommt der Recap
            # sie direkt, nur in Zeitabschnitte gegliedert: Jede weitere Zusammenfassung kostet Fakten, die in den
            # Notizen schon richtig standen. Teil-Zusammenfassungen nur, wenn die Notizen doch zu groß sind.
            zu_gross = tokens(grundlage) > self.max_transkript_tokens
            if self.gliederung == "teile" or (self.gliederung != "direkt" and zu_gross):
                verlauf = self.verlauf(recap_ein, grundlage)
        self.letzter_verlauf = verlauf
        if verlauf:
            r = self.recap(recap_ein, "Verlauf der Runde in Teilen (jeder Teil gehört in den Recap, in dieser "
                                      "Reihenfolge, jeder mit etwa gleich viel Raum)", verlauf)
        elif titel.startswith("Szenennotizen"):
            stand = ("\n\nStand am Ende der Runde (Zustände, Besitz, Beziehungen, Abmachungen – mit dem Zeitpunkt der "
                     "Änderung):\n" + "\n".join(f"- {s}" for s in self.letzter_stand)) if self.letzter_stand else ""
            pflicht = ""
            teile: list[dict] = []
            if self.notizen_zuerst:
                try:
                    teile = self.einschnitte(recap_ein, grundlage)
                except SprachmodellFehler as e:
                    log.warning("Suche nach Einschnitten übersprungen: %s", e)
                    self.warnungen.append({"schritt": "einschnitte", "fehler": str(e),
                                           "antwort": (self.zaehler.letzte_antwort or "")[:3000]})
                try:
                    if teile:  # 0.4.78: je Teil eigene Pflichtereignisse
                        for t in teile:
                            t["auswahl"] = self.auswahl(recap_ein, teil_notizen(grundlage, t["von"], t["bis"]),
                                                        teil_hoechstens(t), t["von"], t["bis"])
                        self.letzte_auswahl = [e for t in teile for e in t["auswahl"]]
                    else:
                        self.letzte_auswahl = self.auswahl(recap_ein, grundlage)
                except SprachmodellFehler as e:
                    log.warning("Auswahl übersprungen: %s", e)
                    self.warnungen.append({"schritt": "auswahl", "fehler": str(e),
                                           "antwort": (self.zaehler.letzte_antwort or "")[:3000]})
                if self.letzte_auswahl and self.zweiter_blick_an:
                    try:
                        self.letzter_zweiter_blick = self.zweiter_blick(recap_ein, self.letzte_auswahl)
                    except SprachmodellFehler as e:
                        log.warning("Zweiter Blick übersprungen: %s", e)
                        self.warnungen.append({"schritt": "zweiter_blick", "fehler": str(e),
                                               "antwort": (self.zaehler.letzte_antwort or "")[:3000]})
                if self.letzte_auswahl:
                    pflicht = PFLICHT_VORSPANN + auswahl_text(self.letzte_auswahl)
            if teile:
                r = self._recap_in_teilen(recap_ein, grundlage, teile)
            else:
                r = self.recap(recap_ein, "Szenennotizen der Runde in Zeitabschnitten (jeder Abschnitt gehört in den "
                                          "Recap, in dieser Reihenfolge, mit etwa gleich viel Raum; lieber knapper "
                                          "erzählen als ein Ereignis weglassen)", self.gegliedert(grundlage) + stand + pflicht)
        else:
            r = self.recap(recap_ein, titel, grundlage)
        self.letztes_kapitel1 = r["text"]
        # Erst Fehlendes ergänzen, dann Falsches prüfen – sonst prüft man einen Text, der gleich wieder wächst.
        # Die Vollständigkeitsprüfung sieht dieselbe Grundlage wie der Recap; ihre Punkte zählen nur, wenn sie dort
        # wiederzufinden sind (fehlend_lesen), damit der Prüfer nichts erfindet.
        pflicht_rest: list[dict] = []
        try:
            if self.letzte_auswahl:
                # 0.4.65: statt der allgemeinen Vollständigkeitsprüfung gezielt gegen die ausgewählten Ereignisse
                pflicht_rest = self.pflicht(recap_ein, self.letzte_auswahl, r)
                gekuerzt = self.kuerzen(recap_ein, self.letzte_auswahl, r, pflicht_rest)
                if gekuerzt is not None:
                    pflicht_rest = gekuerzt
                self.letzte_pflicht["kuerzung"] = self.letzte_kuerzung
            else:
                grund_titel, grund_text = (("Szenennotizen der Runde in Zeitabschnitten", self.gegliedert(grundlage))
                                           if titel.startswith("Szenennotizen") and not verlauf else (titel, grundlage))
                self.letzter_befund_fehlend = self.vollstaendigkeit(recap_ein, grund_titel, grund_text, r["text"])
                neu = self.ergaenzen(recap_ein, r["text"], self.letzter_befund_fehlend)
                if neu:
                    r["text"] = neu
        except SprachmodellFehler as e:
            log.warning("Vollständigkeitsprüfung übersprungen: %s", e)
        self.letztes_kapitel2 = r["text"]
        # Artefakte deterministisch entfernen (Du-Form/abgeschriebene SL-Rede, Regelsprache, Kraftausdrücke, Namen am
        # Tisch, Wiederholungen) – vor der Gegenprüfung, damit sie den Text prüft, den die SL bekommt.
        from app import artefakte

        geschuetzt = [e["name"] for e in recap_ein.get("bibel") or []]
        r["text"], self.bereinigt = artefakte.kapitel(r["text"], recap_ein.get("personen") or [], geschuetzt)
        r["title"] = artefakte.feinschliff(r.get("title") or "")
        r["openThreads"] = [artefakte.feinschliff(f) for f in r.get("openThreads") or []]
        fortschritt(0.6 if gegenpruefen else 0.8)
        if gegenpruefen and self.letzte_auswahl and self.pruefansicht != "gegenpruefung":
            # 0.4.75: keine Gegenprüfung gegen die Notizen mehr – die Prüfansicht entsteht aus dem, was nachgerechnet ist
            pruefung = pruefansicht_aus_pflicht(r["text"], self.letzte_auswahl, pflicht_rest, self.letzte_unklar,
                                                self.letzter_zweiter_blick, self.klient.modell,
                                                bool(self.letzte_pflicht.get("nachgebessert")), recap_ein.get("sprache") or "de")
            self.letzte_pruefung_vorher, self.letzte_pruefung_nachher = pruefung["paragraphs"], []
            self.letzte_relationen_vorher, self.letzte_relationen_nachher = [], []
        else:
            pruefung = self.gegenpruefen(recap_ein, titel, grundlage, r) if gegenpruefen else None
        if pruefung is not None and pruefung.get("revised"):  # Nachbesserung kann Artefakte wieder hineinbringen
            r["text"], nachher = artefakte.kapitel(r["text"], recap_ein.get("personen") or [], geschuetzt)
            self.bereinigt += [b for b in nachher if b["art"] not in ("tischgespraech", "erzaehlstimme")]
        if pruefung is not None:
            hinweise_eintragen(pruefung, self.bereinigt, recap_ein.get("sprache"))
            pflicht_eintragen(pruefung, pflicht_rest, len(absaetze(r["text"])))
            hinweise_filtern(pruefung)
        r["title"] = titel_saeubern(r.get("title") or "")
        if self._teile_anfang[0]:  # 0.4.78: wo jeder Teil nach allen Schritten beginnt
            self.letzte_teile = teile_verorten(r["text"], list(self._teile_anfang[0]), list(self._teile_anfang[1]))
            if self.letzte_teile:
                r["parts"] = self.letzte_teile
        fortschritt(0.8)
        v = [artefakte.vorschlag(x) for x in self.vorschlaege(vorschlag_ein, *self._vorschlag_grundlage(titel, grundlage))]
        fortschritt(1.0)
        aus = {**r, "proposals": v, "model": self.klient.modell, "tokensIn": self.zaehler.tokens_in,
               "tokensOut": self.zaehler.tokens_out, "costCents": self.zaehler.kosten_cent()}
        if pruefung is not None:
            aus["review"] = pruefung
        return aus


def _kern(titel: str) -> str:
    """„Litha Flamel (Deckname: Rita)“ → „litha flamel“ – Klammern und Zusätze nach Doppelpunkt/Gedankenstrich weg."""
    t = re.sub(r"\s*[(\[].*?[)\]]", "", titel)
    t = re.split(r"\s+[–-]\s+|:", t, maxsplit=1)[0]
    return " ".join(re.findall(r"\w+", t.casefold()))


def ist_spielercharakter(titel: str, charaktere) -> bool:
    """Trägt der Vorschlag den Namen eines Spielercharakters? (Nur der Name selbst, nicht „Tubos Versteck“.)"""
    kern = _kern(titel)
    if not kern:
        return False
    for c in charaktere:
        name = " ".join(re.findall(r"\w+", str(c).casefold()))
        if name and (kern == name or kern == _kern(str(c))):
            return True
    return False


# Sätze über den Spieltisch statt über die Spielwelt („In dieser Session etabliert“, „Die SL hat angedeutet“, „Dies
# bezieht sich auf …“) – kleine Modelle schreiben sie trotz Anweisung. Sie fliegen aus Detail und gmNotes. Wörter, die
# auch in der Spielwelt vorkommen („angedeutet“, „dient als“), bleiben erlaubt.
_META = re.compile(
    r"\b(?:Session|Sitzung|Spielrunde|Spielabend|Spieltisch|Spielleit\w*|SL|GM|Spieler(?:in|innen)?|Nebenmission"
    r"|bezieht sich auf|in this session|game ?master|players?|the table|refers to)\b", re.IGNORECASE)


def ohne_meta(text: str) -> str:
    """Sätze über Session, Spielleitung und Spieler entfernen; der Rest bleibt, wie er war."""
    if not text or not _META.search(text):
        return text
    behalten = []
    for zeile in text.split("\n"):
        saetze = re.split(r"(?<=[.!?])\s+", zeile)
        rest = " ".join(t for t in saetze if not _META.search(t)).strip()
        if rest:
            behalten.append(rest)
    return "\n".join(behalten).strip()


# Vermutungen des Modells („gehört vermutlich …“) sind keine Aussagen über die Welt. „scheint“ bleibt erlaubt: So
# beschreibt der Auftrag eine Spur, die am Tisch nur angedeutet wurde. Vermutungen von Figuren („Lysander vermutet“)
# sind Spielgeschehen und bleiben ebenfalls.
_VERMUTUNG = re.compile(r"\b(?:vermutlich|wahrscheinlich|möglicherweise|vielleicht|dürfte|dürften|presumably|probably"
                        r"|possibly|perhaps|likely)\b", re.IGNORECASE)


def ohne_vermutung(text: str) -> str:
    """Sätze mit Vermutungen des Modells entfernen; der Rest bleibt, wie er war."""
    if not text or not _VERMUTUNG.search(text):
        return text
    behalten = []
    for zeile in text.split("\n"):
        rest = " ".join(t for t in re.split(r"(?<=[.!?])\s+", zeile) if not _VERMUTUNG.search(t)).strip()
        if rest:
            behalten.append(rest)
    return "\n".join(behalten).strip()


def _normwoerter(text: str) -> str:
    return " " + " ".join(re.findall(r"\w+", (text or "").casefold())) + " "


def zitat_belegt(zitat: str, grundlage_norm: str) -> bool:
    """Steht das Zitat (wörtlich, ohne Satzzeichen) in der Grundlage? Lange Zitate zählen auch, wenn Anfang oder Ende
    aus fünf Wörtern wörtlich vorkommt – Modelle kürzen oder verbinden Zeilen."""
    w = re.findall(r"\w+", re.sub(r"\[[^\]]*\]", " ", zitat or "").casefold())
    if len(w) < 2:
        return False
    if f" {' '.join(w)} " in grundlage_norm:
        return True
    return len(w) >= 8 and (f" {' '.join(w[:5])} " in grundlage_norm or f" {' '.join(w[-5:])} " in grundlage_norm)


_NAMENSFUELL = {"der", "die", "das", "des", "dem", "den", "von", "vom", "zum", "zur", "und", "the", "of", "and"}


def nennungen(titel: str, grundlage_norm: str) -> int:
    """Wie oft kommt der Name in der Grundlage vor? Bei mehreren Wörtern zählt das seltenste („Gasthaus Zum Holzbein“
    → „holzbein“), damit allgemeine Wörter wie „Gasthaus“ nichts aufblähen."""
    woerter = [w for w in re.findall(r"\w+", _kern(titel)) if len(w) >= 4 and w not in _NAMENSFUELL]
    if not woerter:
        return 0
    return min(len(re.findall(rf" {re.escape(w)}\w*", grundlage_norm)) for w in woerter)


TYPGEWICHT = {"npc": 1.0, "faction": 1.0, "quest": 1.0, "item": 0.85, "location": 0.85, "other": 0.6}


def gewicht(v: dict, belegt: int, n: int) -> float:
    """Reihenfolge der Vorschläge: Änderungen an Vorhandenem vor Neuem, Figuren, Fraktionen und Aufträge vor Orten und
    Gegenständen, Häufiges vor Beiläufigem, Belegtes vor Unbelegtem."""
    import math

    bonus = 0.5 if v["action"] in ("update", "reveal") else 0.0
    return TYPGEWICHT.get(v["entryType"], 0.6) * (1 + math.log2(1 + min(n, 30))) + 0.3 * belegt + bonus


def zutrauen(modell: float, belegt: int, n: int) -> float:
    """Zutrauen aus dem, was der Server selbst prüfen kann: wörtlich gefundene Belege und wie oft der Name fällt.
    Das Modell gibt fast immer 1.0; sein Wert zählt nur noch nach unten."""
    rechnerisch = 0.3 + 0.15 * min(belegt, 3) + (0.1 if n >= 3 else 0.0) + (0.1 if n >= 8 else 0.0)
    return round(min(modell, rechnerisch, 0.95), 2)


def _zeit_aus_notizen(zitat: str, notizen: list[tuple[float, str]]) -> float | None:
    """Zitiert der Vorschlag eine Szenennotiz ohne ihren Zeitstempel, liefert die Notiz die Zeit."""
    k = _notizkern(zitat)
    if len(k) < 12:
        return None
    for zeit, kern in notizen:
        if k in kern or kern in k:
            return zeit
    return None


def pruefen(roh: list, bibel_ids: set[str], geheim_ids: set[str], charaktere=(),
            namen: dict[str, str] | None = None, grundlage: str = "") -> list[dict]:
    """Antwort des Modells in Vorschläge nach Schnittstelle übersetzen; Unbrauchbares fällt weg:
    - neue Einträge für die Charaktere der Spieler und Änderungen an Einträgen, die einen Spielercharakter meinen
      (namen: Eintrags-ID → Name), die das Modell trotz Anweisung gern anlegt
    - Sätze über den Spieltisch statt über die Spielwelt (ohne_meta); bleibt vom Detail nichts übrig, fällt der
      Vorschlag weg
    """
    namen = namen or {}
    notizen = [(_zeit_vorn(z), _notizkern(z)) for z in grundlage.split("\n") if _zeit_vorn(z) is not None]
    grund_norm = _normwoerter(grundlage)
    out, gewichte = [], []
    for v in roh if isinstance(roh, list) else []:
        if not isinstance(v, dict):
            continue
        typ, art = v.get("entryType"), v.get("action")
        titel, detail = klartext(v.get("title")), klartext(v.get("detail"))
        ziel = v.get("targetEntryId") or None
        if typ not in ENTRY_TYPES or art not in ("create", "update", "reveal") or not titel:
            continue
        if art == "create" and ist_spielercharakter(titel, charaktere):
            log.info("Vorschlag „%s“ verworfen – Spielercharakter", titel)
            continue
        if art != "create" and (ist_spielercharakter(titel, charaktere)
                                or ist_spielercharakter(namen.get(ziel or "", ""), charaktere)):
            log.info("Vorschlag „%s“ verworfen – Änderung an einem Spielercharakter", titel)
            continue
        if detail and not ohne_meta(detail):
            continue  # nur Sätze über den Spieltisch
        detail = ohne_meta(detail)
        if detail and not ohne_vermutung(detail):
            continue  # nur Vermutungen
        detail = ohne_vermutung(detail)
        if art == "update" and ziel not in bibel_ids | geheim_ids:
            continue
        if art == "reveal" and ziel not in geheim_ids:
            continue
        if art == "create":
            ziel = None
        sicht = v.get("suggestedVisibility")
        if art == "reveal":
            sicht = "public"
        elif sicht not in ("public", "gm_only"):
            sicht = "gm_only"
        try:
            sicherheit = max(0.0, min(1.0, float(v.get("confidence", 0.5))))
        except (TypeError, ValueError):
            sicherheit = 0.5
        belege = []
        for b in v.get("evidence") or []:
            if isinstance(b, dict) and str(b.get("quote") or "").strip():
                start = zeit_lesen(b.get("start"))
                if not start:  # fehlt oder 0 – aus Notizen steht der Zeitstempel oft im Zitat: „[1:09:07] …“
                    m = re.search(r"\[(\d{1,2}(?::\d{2}){1,2})\]", str(b["quote"]))
                    start = zeit_lesen(m.group(1)) if m else _zeit_aus_notizen(str(b["quote"]), notizen)
                belege.append({"start": start if start is not None else 0.0,
                               "quote": str(b["quote"]).strip()[:200]})
        belege = belege[:3]
        flags = [f for f in v.get("flags") or [] if f in FLAGS]
        if grundlage:
            belegt = sum(1 for b in belege if zitat_belegt(b["quote"], grund_norm))
            n = nennungen(titel, grund_norm)
            sicherheit = zutrauen(sicherheit, belegt, n)
            if belegt == 0 and "low_confidence" not in flags:
                flags.append("low_confidence")
        else:
            belegt, n = len(belege), 0
        if sicherheit < 0.4 and "low_confidence" not in flags:
            flags.append("low_confidence")
        v_neu = {
            "entryType": typ, "action": art, "targetEntryId": ziel, "title": titel[:300], "detail": detail[:4000],
            "gmNotes": (ohne_vermutung(ohne_meta(klartext(v.get("gmNotes"))))[:4000] or None) if art == "create" else None,
            "suggestedVisibility": sicht, "visibilityReason": klartext(v.get("visibilityReason"))[:500] or None,
            "confidence": sicherheit, "flags": flags, "evidence": belege,
        }
        out.append(v_neu)
        gewichte.append(gewicht(v_neu, belegt, n))
        if len(out) >= MAX_VORSCHLAEGE_ROH:
            break
    # Die gewichtigsten zuerst, bei Gleichstand in der Reihenfolge des Modells; dann auf MAX_VORSCHLAEGE kürzen
    reihenfolge = sorted(range(len(out)), key=lambda i: (-gewichte[i], i)) if grundlage else list(range(len(out)))
    return [out[i] for i in reihenfolge[:MAX_VORSCHLAEGE]]


# ================================================================ SL-Unterlagen
MAX_DOK_VORSCHLAEGE = 40
ART_TEXT = {
    "handout": "Spielerhandout – alles darin wissen die Spieler. detail enthält alles, gmNotes bleibt leer.",
    "gm": "SL-Unterlage – alles ist zunächst geheim. Trenne trotzdem: detail = was Spieler bei einer offenen "
          "Begegnung wahrnehmen würden (Aussehen, bekannter Ruf), gmNotes = Geheimnisse, Pläne, Hintergründe.",
    "mixed": "Gemischte Unterlage – Spielerwissen und Geheimes gemischt. Setze publicSuggested=true, wenn ein Eintrag "
             "insgesamt Spielerwissen ist, und begründe das in visibilityReason.",
}

SYSTEM_UNTERLAGE = """Du wertest eine Unterlage für eine Pen-&-Paper-Kampagne aus und schlägst Einträge für die \
Kampagnen-Bibel vor (npc, location, quest, item, faction, other). Die Spielleitung prüft jeden Vorschlag.
Art der Unterlage: {art}
Regeln:
- create: etwas, das noch nicht in der Bibel steht – auch nicht unter anderer Schreibweise. Sonst update mit \
targetEntryId aus der Bibel; detail und gmNotes enthalten dann nur das Neue.
- Widerspricht die Unterlage einem Bibel-Eintrag, mache ein update mit Flag contradicts_bible und erkläre den \
Widerspruch in gmNotes.
- detail = Spielerwissen, gmNotes = Geheimes. Abschnitte mit Markierungen wie „[SL]“, „[GM]“, „Geheim:“ oder \
„Secret:“ gehören immer in gmNotes.
- Nur, was in der Unterlage steht. Nichts erfinden. Regeltexte, Werte-Tabellen und Inhaltsverzeichnisse sind keine \
Einträge.
- evidence: 1 bis 3 Belege {"page": Seitenzahl aus „[Seite N]“ oder null, "quote": wörtliches Zitat, höchstens \
200 Zeichen}. confidence zwischen 0 und 1.
- worldInfo: kurzer Welt-Hintergrund (höchstens 800 Zeichen), NUR aus Spielerwissen, sonst null.
- Höchstens {max} Vorschläge, das Wichtigste zuerst.
Antworte nur mit JSON: {"proposals": [{"entryType": "…", "action": "create", "targetEntryId": null, "title": "…", \
"detail": "…", "gmNotes": null, "publicSuggested": false, "visibilityReason": null, "confidence": 0.8, "flags": [], \
"evidence": [{"page": 3, "quote": "…"}]}], "worldInfo": null}. Sprache der Texte: {sprache}."""

SYSTEM_WELT = """Fasse diese Stücke eines Welt-Hintergrunds für eine Pen-&-Paper-Kampagne zu einem zusammenhängenden \
Text von höchstens 1500 Zeichen zusammen. Nur Spielerwissen, nichts erfinden, Absätze durch Leerzeilen.
Antworte nur mit JSON: {"worldInfo": "…"}. Sprache: {sprache}."""


def _norm_titel(t: str) -> str:
    return " ".join(re.findall(r"\w+", t.lower()))


def dokument_pruefen(roh: list, bibel: dict[str, str]) -> list[dict]:
    """Antwort in Vorschläge übersetzen. bibel: id → Name. Ein create mit dem Namen eines vorhandenen Eintrags wird
    zum update dieses Eintrags (keine Doppelten)."""
    namen = {_norm_titel(n): i for i, n in bibel.items()}
    out = []
    for v in roh if isinstance(roh, list) else []:
        if not isinstance(v, dict):
            continue
        typ, art = v.get("entryType"), v.get("action")
        titel = klartext(v.get("title"))
        ziel = v.get("targetEntryId") or None
        if typ not in ENTRY_TYPES or art not in ("create", "update") or not titel:
            continue
        if art == "create" and _norm_titel(titel) in namen:
            art, ziel = "update", namen[_norm_titel(titel)]
        if art == "update" and ziel not in bibel:
            continue
        if art == "create":
            ziel = None
        try:
            sicherheit = max(0.0, min(1.0, float(v.get("confidence", 0.5))))
        except (TypeError, ValueError):
            sicherheit = 0.5
        belege = []
        for b in v.get("evidence") or []:
            if isinstance(b, dict) and str(b.get("quote") or "").strip():
                beleg = {"quote": str(b["quote"]).strip()[:200]}
                try:
                    if b.get("page") is not None and int(b["page"]) >= 1:
                        beleg["page"] = int(b["page"])
                except (TypeError, ValueError):
                    pass
                belege.append(beleg)
        flags = [f for f in v.get("flags") or [] if f in FLAGS]
        if sicherheit < 0.4 and "low_confidence" not in flags:
            flags.append("low_confidence")
        out.append({
            "entryType": typ, "action": art, "targetEntryId": ziel, "title": titel[:300],
            "detail": klartext(v.get("detail"))[:4000],
            "gmNotes": klartext(v.get("gmNotes"))[:4000] or None,
            "publicSuggested": bool(v.get("publicSuggested")),
            "visibilityReason": str(v.get("visibilityReason") or "").strip()[:500] or None,
            "confidence": sicherheit, "flags": flags, "evidence": belege[:3],
        })
    return out


def _zusammenlegen(alle: list[dict]) -> list[dict]:
    """Vorschläge aus mehreren Stücken derselben Unterlage zusammenführen (gleiches Ziel bzw. gleicher Titel)."""
    def verbinden(a: str | None, b: str | None) -> str | None:
        a, b = (a or "").strip(), (b or "").strip()
        if not b or b in a:
            return a or None
        if not a or a in b:
            return b
        return f"{a}\n\n{b}"

    ergebnis: dict[tuple, dict] = {}
    for v in alle:
        schluessel = ("u", v["targetEntryId"]) if v["action"] == "update" else ("c", v["entryType"],
                                                                                _norm_titel(v["title"]))
        alt = ergebnis.get(schluessel)
        if alt is None:
            ergebnis[schluessel] = dict(v)
            continue
        alt["detail"] = verbinden(alt["detail"], v["detail"]) or ""
        alt["gmNotes"] = verbinden(alt["gmNotes"], v["gmNotes"])
        alt["publicSuggested"] = alt["publicSuggested"] and v["publicSuggested"]
        alt["confidence"] = max(alt["confidence"], v["confidence"])
        alt["flags"] = sorted(set(alt["flags"]) | set(v["flags"]))
        alt["evidence"] = (alt["evidence"] + v["evidence"])[:3]
        alt["visibilityReason"] = alt["visibilityReason"] or v["visibilityReason"]
    return list(ergebnis.values())[:MAX_DOK_VORSCHLAEGE]


@dataclass
class DokumentAblauf:
    klient: Klient
    stueck_tokens: int = 30_000
    zaehler: Zaehler = field(default_factory=Zaehler)

    def ausfuehren(self, ein: dict, fortschritt: Callable[[float], None] = lambda _p: None) -> dict:
        """ein: sprache, kampagne, system(_name), welt, art, titel, abschnitte [{seite, text}],
        bibel [{id, typ, name, zusammenfassung, gm_notes, sichtbarkeit}]."""
        zeilen = [(f"[Seite {a['seite']}]\n" if a.get("seite") else "") + a["text"] for a in ein["abschnitte"]]
        teile = stuecke(zeilen, self.stueck_tokens)
        bibel = {e["id"]: e["name"] for e in ein["bibel"]}
        system = (SYSTEM_UNTERLAGE.replace("{art}", ART_TEXT[ein["art"]]).replace("{sprache}", _sprache(ein))
                  .replace("{max}", str(MAX_DOK_VORSCHLAEGE)))
        kopf = _kopf_dokument(ein)
        alle, welt = [], []
        for i, teil in enumerate(teile):
            def eintrag(e: dict) -> str:
                z = f"- id={e['id']} [{e['typ']}] {e['name']}" + (" (geheim)" if e.get("sichtbarkeit") != "public" else "")
                if _erwaehnt(e["name"], teil):
                    z += f": {e.get('zusammenfassung', '')[:600]}"
                    if e.get("gm_notes"):
                        z += f"\n  gmNotes: {e['gm_notes'][:600]}"
                return z
            liste = "\n".join(eintrag(e) for e in ein["bibel"]) or "(leer)"
            d = self.zaehler.aufruf(self.klient, system, f"{kopf}\n\nBibel:\n{liste}\n\nUnterlage „{ein['titel']}“, "
                                                         f"Teil {i + 1} von {len(teile)}:\n{teil}")
            alle += dokument_pruefen(d.get("proposals") or [], bibel)
            if ein["art"] != "gm" and str(d.get("worldInfo") or "").strip():
                welt.append(str(d["worldInfo"]).strip())
            fortschritt(0.9 * (i + 1) / len(teile))
        welt_text = None
        if len(welt) == 1:
            welt_text = welt[0][:1500]
        elif welt:
            d = self.zaehler.aufruf(self.klient, SYSTEM_WELT.replace("{sprache}", _sprache(ein)), "\n\n---\n\n".join(welt))
            welt_text = str(d.get("worldInfo") or "").strip()[:1500] or None
        fortschritt(1.0)
        return {"proposals": _zusammenlegen(alle), "worldInfoSuggestion": welt_text, "model": self.klient.modell,
                "tokensIn": self.zaehler.tokens_in, "tokensOut": self.zaehler.tokens_out}


def _kopf_dokument(ein: dict) -> str:
    zeilen = [f"Kampagne: {ein['kampagne']}"]
    if ein.get("system_name") or ein.get("system"):
        zeilen.append(f"Regelsystem: {ein.get('system_name') or ein.get('system')}")
    if ein.get("welt"):
        zeilen.append(f"Welt (bisher bekannt):\n{ein['welt'][:3000]}")
    return "\n".join(zeilen)
