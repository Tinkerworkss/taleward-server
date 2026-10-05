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

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx
import jsonschema

log = logging.getLogger("worker")

ENTRY_TYPES = ("npc", "location", "quest", "item", "faction", "other")
FLAGS = ("joke_suspected", "low_confidence", "contradicts_bible")
MAX_VORSCHLAEGE = 15
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
    done_reason: str | None = None


class Klient(Protocol):
    modell: str

    def chat(self, system: str, nutzer: str) -> Antwort: ...


class OpenAIKlient:
    """OpenAI-kompatible Schnittstelle (Mistral, OpenAI, viele andere): POST {url}/chat/completions."""

    def __init__(self, url: str, api_key: str, modell: str, client: httpx.Client | None = None,
                 cent_pro_mio: tuple[float, float] | None = None):
        self.url, self.modell = url.rstrip("/"), modell
        self.client = client or httpx.Client(timeout=httpx.Timeout(600.0, connect=20.0))
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.cent_pro_mio = cent_pro_mio or PREISE.get(modell, (0, 0))

    def chat(self, system: str, nutzer: str) -> Antwort:
        body = {"model": self.modell, "temperature": 0.3, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": nutzer}]}
        for versuch in range(3):
            try:
                r = self.client.post(f"{self.url}/chat/completions", json=body, headers=self.headers)
            except httpx.HTTPError as e:
                if versuch == 2:
                    raise SprachmodellFehler(f"Sprachmodell nicht erreichbar ({type(e).__name__}).") from e
                time.sleep(2 * (versuch + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if versuch == 2:
                    raise SprachmodellFehler(f"Der Anbieter antwortet mit {r.status_code}.")
                time.sleep(5 * (versuch + 1))
                continue
            if r.status_code in (401, 403):
                raise SprachmodellFehler("Der Anbieter lehnt den API-Schlüssel ab.", erneut=False)
            if r.status_code >= 400:
                raise SprachmodellFehler(f"Der Anbieter lehnt die Anfrage ab ({r.status_code}): {r.text[:200]}",
                                         erneut=False)
            d = r.json()
            u = d.get("usage") or {}
            return Antwort(d["choices"][0]["message"]["content"] or "", int(u.get("prompt_tokens") or 0),
                           int(u.get("completion_tokens") or 0))
        raise SprachmodellFehler("Sprachmodell nicht erreichbar.")

    def chat_strukturiert(self, system: str, nutzer: str, schema: dict) -> Antwort:
        """OpenAI-kompatibel: Schema im Prompt validieren wir immer lokal. Anbieter unterscheiden sich bei json_schema,
        deshalb bleibt serverseitig json_object für maximale Kompatibilität."""
        return self.chat(system, nutzer)

    def kosten_cent(self, tokens_in: int, tokens_out: int) -> int:
        ein, aus = self.cent_pro_mio
        return round((tokens_in * ein + tokens_out * aus) / 1_000_000)


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
        return self._chat_mit_schema(system, nutzer, None)

    def chat_strukturiert(self, system: str, nutzer: str, schema: dict) -> Antwort:
        vorher = self.temperatur
        deterministisch = (system.startswith("Du hilfst bei der Nachbereitung")
                           or system.startswith("Du klassifizierst")
                           or system.startswith("Du vergleichst")
                           or system.startswith("Du prüfst")
                           or system.startswith("Du extrahierst")
                           or system.startswith("Du suchst")
                           or system.startswith("Du ordnest"))
        if deterministisch:
            self.temperatur = 0.0
        try:
            return self._chat_mit_schema(system, nutzer, schema)
        finally:
            self.temperatur = vorher

    def _chat_mit_schema(self, system: str, nutzer: str, schema: dict | None) -> Antwort:
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
                body["format"] = schema or "json"
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
        return Antwort(d.get("inhalt", ""), int(d.get("prompt_eval_count") or 0), tokens_aus,
                       str(d.get("done_reason") or "") or None)

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


def transkript_zeilen_mit_ids(transkript: list[dict]) -> list[tuple[str, str]]:
    """Wie transkript_zeilen, aber mit stabilen IDs L0001… für evidenzkritische Modellaufrufe."""
    return [(f"L{i + 1:04d}", z) for i, z in enumerate(transkript_zeilen(transkript))]


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


_ZEIT_VORN = re.compile(r"^\s*\[?(\d{1,2}(?::\d{2}){1,2})\]\s*")  # „[12:34] “, zur Not auch „12:34] “
_REGELN = re.compile(r"\b(?:Würfel\w*|würfel\w*|\w*[Pp]robe\b|\w*[Pp]roben\b|\w*attacke\b|Kampfrunde\w*|Qualitätsstufe"
                     r"|QS\s?\d|Lebenspunkt\w*|Karmapunkt\w*|Astralpunkt\w*|Zauberpunkt\w*|Schadenspunkt\w*|LeP|KaP|AsP|ASP"
                     r"|Initiative|erleichtert\w*|erschwert\w*|kritisch\w* (?:erfolgreich|Erfolg\w*|Patzer|Wurf\w*|Treffer|bei)|Patzer"
                     r"|Wurf\b|Wurfs\b|Würfe\w*|[bB]20\b|\d+ Schaden\b|Schaden(?:spunkte)?\b"
                     r"|\d+\s?[wW]\d+|[wW]20\b|\d-\d{1,2}-\d{1,2}|Schicksalsmarker"
                     r"|Spielleitung (?:verlangt|fordert|erlaubt|bittet|kündigt|informiert|bestätigt|stellt fest|teilt mit)"
                     r"|Wiederholung aus Bisher|Gewinnspiel|Pause)\b")
_SPIELLEITUNG = re.compile(r"\b(?:Spielleitung|Spielleiter\w*|game master|GM)\b", re.I)
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


def woerter(ein: dict) -> str:
    """Länge des Recaps nach Länge der Runde: Ein langer Abend hat mehr Wendepunkte als eine kurze Szene."""
    zeilen = ein.get("transkript") or []
    minuten = max((float(z.get("start") or 0) for z in zeilen), default=0.0) / 60
    if minuten > 120:
        return "600–1200"
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
…“, „mein Bruder“, „schuldet mir“), Eigennamen von Gegenständen und Orten wörtlich (den konkreten Namen statt \
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
des Spiels ganz weg. Offensichtliche Witze markierst du mit „(Witz?)“. Erfinde nichts. Steht vor dem Abschnitt \
„Bisher“, ist das nur zur Orientierung – nicht wiederholen.
Antworte nur mit JSON: {"notizen": ["[m:ss] …", …]}. Sprache der Notizen: {sprache}."""

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
- Erzählstimme in der Vergangenheit, lebendig und vorlesbar, im Ton der Kampagne und ihrer Welt. {woerter} Wörter, \
Absätze durch Leerzeilen getrennt.
- Alle Wendepunkte der Grundlage in ihrer Reihenfolge, jeder mit seinem Ausgang – lieber knapp erzählt als \
weggelassen. Ausgänge genau wie in der Grundlage: Wer verletzt ist, ist nicht tot; was angedroht war, ist nicht \
geschehen; wer etwas wofür gibt, steht so in der Grundlage.
- Steht in der Eingabe ein „Pflichtplan“, muss jeder dort ausgewählte Punkt im Recap vorkommen. Der Pflichtplan wählt ausschließlich aus der Grundlage aus und erlaubt keine neuen Tatsachen. Pflichtpunkte knapp erzählen, aber nicht durch allgemeinere Formulierungen ersetzen oder weglassen.
- Die Figuren heißen nach ihren Charakteren, nicht nach den Menschen am Tisch. Die Spielleitung ist keine Figur: \
Was sie sagt, sagt ein Nichtspielercharakter oder die Erzählung; das Wort „Spielleitung“ kommt im Recap nicht vor. \
Regeln, Würfe, Punkte und Gespräche außerhalb des Spiels kommen nicht vor.
- Offensichtliche Witze sind kein Spielgeschehen.
- Titel: „Kapitel {nummer}: “ und ein kurzer, stimmungsvoller Titel.
- Offene Fäden: 0 bis 6 kurze Sätze zu ungelösten Fragen, Versprechen und Zielen der Gruppe.
- Reiner Text ohne Markdown: keine Sternchen, keine Rauten, keine Zwischenüberschriften, keine Listen.
Antworte nur mit JSON: {"title": "…", "text": "…", "openThreads": ["…"]}. Sprache: {sprache}."""

SYSTEM_RECAP_PLAN = """Du klassifizierst die Szenennotizen eines kurzen Zeitabschnitts einer Pen-&-Paper-Session für den späteren Recap. Du schreibst noch keine Geschichte und formulierst nichts um. Jede Notiz hat eine feste ID N001, N002 usw.
Ordne JEDE ID genau einer Klasse zu. Es gibt keine Höchstzahl und keine Rangliste: Relevante Notizen dürfen nicht gegeneinander ausgespielt werden.
- kritisch: Wendepunkt; Tod oder Rettung; Gefangennahme/Befreiung; entscheidender Orts- oder Lagewechsel; Abmachung, Versprechen oder Verpflichtung; zentrale Enthüllung/Identität; entscheidender Hinweis oder offene Handlungsmöglichkeit.
- wichtig: bleibender Zustand; Beziehung oder bestehende Verbindung; Besitzwechsel; benannter Gegenstand, der erhalten, übergeben, verloren oder gesucht wird; Ziel/Auftrag; benannter Ort oder NSC mit Bedeutung für das weitere Handeln.
- nebensächlich: vorübergehende Details ohne bleibende Folge, Wiederholungen, Regeln/Würfe, Smalltalk, Witze, Essen/Schlafen, reine Atmosphäre, einzelne gescheiterte Versuche.
Wichtig: Klassifiziere ALLE IDs. Wenn in einem Abschnitt fünf oder zehn relevante Dinge passieren, markiere alle als kritisch oder wichtig. Gib ausschließlich vorhandene IDs zurück, keine Texte, keine Umformulierungen und keine neuen IDs.
Antworte nur mit JSON: {"critical": ["N001"], "important": ["N002"], "minor": ["N003"]}. Sprache: {sprache}."""

PLAN_MINUTEN = 15


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
- detail und gmNotes sind reiner Text ohne Markdown (keine Sternchen, keine Rauten); mehrere Punkte als eigene Zeilen. \
Keine Vermutungen – nur, was gesagt wurde.
Antworte nur mit JSON: {"proposals": [{"entryType": "…", "action": "…", "targetEntryId": null, "title": "…", \
"detail": "…", "gmNotes": null, "suggestedVisibility": "…", "visibilityReason": "…", "confidence": 0.7, \
"flags": [], "evidence": [{"start": "m:ss", "quote": "…"}]}]}. Sprache der Texte: {sprache}."""


SYSTEM_PRUEFUNG = """Du prüfst den Recap einer Pen-&-Paper-Session gegen seine Grundlage (Transkript oder \
Szenennotizen; Zeitangaben stehen vorn in eckigen Klammern, z. B. „[12:34]“). Du schreibst nichts um, du bewertest nur.
Für jeden nummerierten Absatz:
- urteil: "belegt" (jede Aussage steht in der Grundlage), "teilweise" (einiges belegt, anderes nicht), "unbelegt" \
(kommt in der Grundlage nicht vor), "widerspricht" (die Grundlage sagt etwas anderes) oder "witz" (stammt aus einem \
Witz oder einem Gespräch außerhalb des Spiels).
- Streng sein: Ausschmückungen, die so nicht vorkamen (Gefühle, Aussehen, Gerüche, Wetter, Gedanken), machen einen \
Absatz höchstens "teilweise".
- Prüfe jede Beziehung einzeln, nicht nur, ob die Wörter vorkommen: Wer tut was wem? Wer gibt wem was, und wer hat es \
danach? War jemand schon verletzt, oder wird er es durch die erzählte Handlung? Wer verspricht wem was, gegen welche \
Gegenleistung? Ist eine Figur in der Szene anwesend oder wird nur über sie gesprochen? Ist etwas beobachtet, behauptet, \
vermutet, geplant, erinnert oder eine Vision? Eine vertauschte Richtung („A gibt B“ statt „B gibt A“), ein Zustand, der \
umgedreht ist (verletzt → tot), eine Vermutung als Tatsache oder eine nur erwähnte Figur als anwesend: "widerspricht".
- stellen: 1 bis 3 Stellen der Grundlage, auf die sich der Absatz stützt – Zeitangabe "m:ss" und ein kurzes \
wörtliches Zitat daraus (höchstens 20 Wörter). Leer, wenn nichts belegt ist.
- begruendung: ein kurzer Satz, was fehlt, abweicht oder erfunden ist. Leer bei "belegt".
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

FEHLEND_HOECHSTENS = 5  # so viele fehlende Ereignisse darf die Vollständigkeitsprüfung nennen
ERGAENZEN_RAENGE = ("kritisch",)  # 0.4.50: post-hoc nur kritische Punkte; Klassifikationsplan trägt Wichtiges
RELATION_FENSTER_S = 60.0  # Transkript ± so viele Sekunden um die belegten Stellen eines Absatzes
RELATION_ZEICHEN = 6000  # höchstens so viel Transkript je Absatz
PRUEF_BATCH = 4  # Gegenprüfung in kleinen Paketen: ein abgeschnittener Call darf nicht den ganzen Review vernichten
LEDGER_STUECK_TOKEN = 3500  # Originaltranskript je Ledger-Aufruf; kleine Antworten sind wichtiger als wenige Calls
LEDGER_REVIEW_BATCH = 32  # nur Recovery-Grenze; normal prüft 0.4.53 genau einmal je Quellblock
LEDGER_RECONCILE_BATCH = 12  # nach lokalem History-Filter passen mehr relevante Events sicher in einen Abgleich
LEDGER_RELEVANCE_BATCH = 24
LEDGER_PARSER_VERSION = "0.4.57"

SYSTEM_RELATIONEN = """Du prüfst genau EINEN Absatz des Recaps einer Pen-&-Paper-Session gegen kurze Ausschnitte des ORIGINALTRANSKRIPTS (automatisch erkannt, mit Fehlern; „Spielleitung“ spricht dort für Nichtspielercharaktere). Prüfe nur atomare Beziehungen und Zustände, nicht Stil oder Vollständigkeit.
Zerlege den Absatz in die kleinsten relevanten Behauptungen: Wer tut was wem? Wer gibt wem was und wer besitzt es danach? War jemand bereits verletzt oder wird er verletzt? Wer kennt wen und seit wann? Wer verspricht wem was gegen welche Gegenleistung? Sind zwei Figuren verwechselt oder verschmolzen? Ist eine Figur anwesend oder nur erwähnt? Ist etwas beobachtet, behauptet, vermutet, geplant, erinnert oder eine Vision?
Für jede solche Behauptung:
- claim: kopiere die kürzeste passende Textstelle aus dem Recap WÖRTLICH. Keine Paraphrase.
- urteil: "stimmt", "widerspricht" oder "unklar". "widerspricht" NUR, wenn das Transkript ausdrücklich eine unvereinbare Beziehung oder einen anderen Zustand zeigt. Fehlt die Information im Ausschnitt, ist das "unklar", niemals ein Widerspruch.
- korrektur: nur bei "widerspricht" eine minimale Ersatzformulierung für genau claim, die das Transkript belegt. Keine zusätzlichen Tatsachen. Wenn keine sichere minimale Korrektur möglich ist, leer lassen.
- begruendung: ein kurzer Satz.
- sourceIds: die IDs Lxxxx der Transkriptzeilen, die dein Urteil direkt belegen. Verwende ausschließlich IDs, die im Ausschnitt stehen. Keine Zitate und keine Zeitstempel erfinden.
Antworte nur mit JSON: {"claims": [{"claim": "…", "urteil": "…", "korrektur": "…", "begruendung": "…", "sourceIds": ["L0001"]}]}. Sprache: {sprache}."""


SYSTEM_LEDGER_EVENTS = """Du extrahierst ein Ereignis-Ledger aus EINEM Abschnitt des ORIGINALTRANSKRIPTS einer Pen-&-Paper-Session. Jede Zeile hat eine unveränderliche Source-ID Lxxxx. Schreibe keine Chronik und keine Prosa, sondern atomare, belegte Ereignisse.
Erfasse besonders Wendepunkte, Handlungen mit Folgen, Rettung/Tod/Verletzung, Orts- und Besitzwechsel, Entdeckungen, Abmachungen, Ziele, Identitäten und Transformationen. Erfasse lieber einen wichtigen Fakt zusätzlich, als ihn wegen unsicherer späterer Verwendung wegzulassen. Atmosphäre, Routine und folgenlose Kleinschritte gehören nicht in den Ledger. Ein Ereignis darf mehrere kinds haben.
Für Zustände nutze assertions: subject = betroffene Entität, property = eine der universellen Eigenschaften life_status, physical_condition, location, possession, relationship, identity, knowledge, allegiance, goal, obligation, reputation, control, role_status oder other; value = der konkrete Zustand. epistemic hält fest, ob etwas beobachtet, nur gesagt/berichtet/geglaubt/vermutet/erinnert, Vision/Traum oder unklar ist.
Wichtig: "für tot gehalten" ist NICHT dasselbe wie tatsächlich tot. Geplant ist nicht geschehen, versucht ist nicht gelungen. Eine spätere Enthüllung darf einem früheren Eindruck widersprechen; beide Ereignisse bleiben im Ledger. Sprecherlabels stammen aus automatischer Erkennung und können falsch sein; erfinde deshalb keine Identität nur aus einem Label.
Bei längeren Konflikten, Kämpfen oder Verfolgungen NICHT jede Runde, jeden Angriff, Wurf, Treffer oder folgenlosen Positionswechsel als eigenes Event erfassen. Eine Einzelaktion gehört nur dann in den Welt-Ledger, wenn sie Zustand, Ziel, Kontrolle, Besitz, Wissen, Beteiligte, verfügbare Route oder den Ausgang relevant verändert. Reine Regelmechanik bleibt draußen.
sourceIds müssen die Aussage direkt tragen und dürfen ausschließlich aus diesem Abschnitt stammen. Keine Source-ID erfinden. Lieber zwei kleine Events als ein vermischtes.
Antworte nur mit JSON {"events": [...]} nach dem vorgegebenen Schema. Sprache: {sprache}."""

SYSTEM_LEDGER_CONTINUITY = """Du extrahierst aus EINEM Abschnitt des ORIGINALTRANSKRIPTS die leicht übersehenen Kontinuitätsfakten einer Pen-&-Paper-Session. Jede Zeile hat eine Source-ID Lxxxx.
Suche ausdrücklich nach Beziehungen ("wir kennen uns seit …", Verwandtschaft, Loyalität, Feindschaft), Besitz und Übergaben mit Richtung, Versprechen/Schulden/Deals, Wissen und Enthüllungen, Identität/Verkleidung, scheinbaren oder behaupteten Zuständen, benannten Gegenständen sowie Unterschieden zwischen Person A und Person B. Erfasse auch einen kurzen Nebensatz, wenn er später wichtig werden kann.
Nutze dieselbe Event-Struktur wie das Ereignis-Ledger. assertions tragen subject/property/value und den epistemischen Status. Erfinde nichts und leite keine Weltwahrheit aus einem bloßen Gerücht ab. sourceIds müssen die Aussage direkt belegen und im Abschnitt vorhanden sein.
Antworte nur mit JSON {"events": [...]} nach dem vorgegebenen Schema. Sprache: {sprache}."""

SYSTEM_LEDGER_REVIEW = """Du prüfst Ledger-Kandidaten einer Pen-&-Paper-Session gegen EINEN Abschnitt des ORIGINALTRANSKRIPTS. Die Kandidaten stammen aus dem primären Ereignis-Pass. Prüfe vorhandene Kandidaten UND suche im selben Schritt nach wenigen wichtigen Kontinuitätsfakten, die der Primärpass ganz übersehen hat.
Melde in reviews NUR Kandidaten, die geändert werden müssen; nicht genannte Kandidaten gelten als akzeptiert.
Prüfe besonders:
- Wer tut was wem? actor/target und Besitzrichtung niemals vertauschen.
- Pronomen nur auflösen, wenn der lokale Kontext es trägt; sonst die Entität allgemeiner lassen.
- Sprecherlabels stammen aus automatischer Erkennung und können falsch sein. Nutze deshalb zusätzlich die GESPRÄCHSROLLE: Fragt ein Spieler unmittelbar nach Zustand/Handlung eines NPCs oder der Welt und folgt genau eine unbestrittene, autoritativ formulierte Weltantwort, darfst du einen widersprechenden Sprecherlabel als wahrscheinlich falsch behandeln. Markiere das Event dann mit tag "speaker_conflict". Bei mehreren konkurrierenden Antworten oder bloßer Meinung bleibt der epistemische Status unsicher/stated.
- "Spielleitung", "Game Master" und vergleichbare Tischrollen sind keine Figuren der Spielwelt. Sie erzählen oder sprechen für NPCs. Verwende eine Tischrolle niemals als actor, target oder assertion.subject eines Weltfakts.
- Personen nicht verschmelzen. Eine Anrede an Person A unmittelbar vor "ich bin B" macht A nicht zu B.
- beobachtet/gesagt/geglaubt/erinnert/Vision sauber trennen; geplant/versucht ist nicht automatisch geschehen.
- reine Spielmechanik (Würfel, Initiative, Regelwerte, Schadenszahlen) ist kein Weltfakt, solange daraus keine erzählerische Folge entsteht.
- gleiche oder nahezu gleiche Kandidaten zusammenführen.
Für jeden Fehler: originIds = betroffene C-IDs; verdict = "repair", "merge" oder "reject". Bei repair/merge MUSS replacement ein vollständig source-belegtes Event im normalen Ledger-Schema sein. Bei reject ist replacement null. replacement.sourceIds dürfen ausschließlich aus dem bereitgestellten Abschnitt stammen und müssen die Aussage direkt tragen.

coverage ist das Sicherheitsnetz für KOMPLETT FEHLENDE, später relevante Ereignisse. Gib dort ausschließlich importance "critical" oder "important" zurück; keine Umformulierungen vorhandener Kandidaten und keinen Kleinkram. Suche gezielt nach Tod/Überleben, Rettung, schwerer Verletzung/Heilung, Transformation, Besitzübergabe mit Richtung, Identität/Verwechslung, Beziehung, Deal/Verpflichtung, entscheidender Entdeckung oder Wissensänderung sowie plotrelevantem Ortswechsel. Normale Dialogakte, Fragen, Zurufe, Routinehandlungen und folgenlose Bewegungen gehören NICHT in coverage.

anchors sind NUR Prüfhinweise, KEINE kanonische Wahrheit und dürfen Events später nicht automatisch überschreiben. Gib höchstens 4 Anchors pro Abschnitt aus und nur für besonders folgenschwere life_status, identity, possession, relationship, obligation oder eine critical physical_condition. Jeder Anchor ist atomar: subject + property + value, source-belegt, importance nur critical/important. originIds enthält die C-IDs der Kandidaten, die genau diesen Fakt abzubilden versuchen; wenn er komplett fehlt, ist originIds leer. Keine Regelmechanik, keine Atmosphäre, keine Routine. Bei unsicherer Auflösung keinen Anchor erzeugen.

encounters beschreibt nur RECAP-RELEVANTE, SUBSTANTIELLE zusammenhängende Konflikte/Verfolgungen/Kämpfe im Abschnitt, höchstens 1 Fragment pro Abschnitt. Kein einzelner Angriff, keine Tür-/Routineprobe und keine Würfelabfolge. Ein Fragment fasst eine erzählerische Phase zusammen: Beteiligte, Orte, Ziele, frei benannte domains (z. B. unterschiedliche Schauplätze/Ebenen), Wendepunkte, Ausgang/Folgen/offene Punkte. boundary ist start/middle/end/complete/unknown. Bei keinem substanziellen Encounter: leere Liste. Die Kategorien sind systemagnostisch; erfinde keine systemspezifischen Ebenen.

Für coverage, anchors und encounters dürfen sourceIds nur aus diesem Abschnitt stammen und müssen die jeweilige Aussage direkt tragen. Behauptet/geglaubt/Vision ist nicht beobachtete Weltwahrheit.
Antworte nur mit JSON {"reviews": [...], "coverage": [...], "anchors": [...], "encounters": [...]} nach dem vorgegebenen Schema. Sprache: {sprache}."""

SYSTEM_LEDGER_CRITICAL = """Du sicherst wenige HOCHRISIKO-FAKTEN eines Pen-&-Paper-Ledgers direkt gegen EINEN Abschnitt des ORIGINALTRANSKRIPTS ab. Du bekommst zusätzlich bereits geprüfte Events mit temporären IDs Txxxx.
Suche ausschließlich critical/important Fakten dieser Klassen:
- life_status: Tod, Überleben, Wiederkehr.
- physical_condition: schwere/anhaltende Verletzung oder deutliche Heilung.
- rescue_aid: Rettung, medizinische Behandlung, Befreiung aus akuter Gefahr.
- possession_transfer: benannter oder plotrelevanter Gegenstand wechselt Besitzer/Kontrolle.
- identity_role: Identität, Verwechslung, dauerhafte Rolle.
- plot_location: plotrelevanter Aufenthaltsort einer benannten Person/eines benannten Gegenstands.
- goal_obligation: wichtiges Ziel, Schwur, Deal, Schuld oder Verpflichtung.

WICHTIG für lokale Gesprächsauflösung:
- Kurze Antworten und Pronomen beziehen sich häufig auf die unmittelbar zuvor erfragte/besprochene Figur. Nutze den lokalen Turn-Kontext, aber rate nicht über unklare Referenten.
- Sprecherlabels sind automatisch erkannt und können falsch sein. Fragt ein Spieler unmittelbar nach Zustand/Handlung eines NPCs oder der Welt und folgt genau eine unbestrittene autoritative Weltantwort, darf das Sprecherlabel als fehlerhaft gelten; markiere dann tag "speaker_conflict".
- Bei rescue_aid gilt: actor = rettende/behandelnde Figur, target = gerettete/behandelte Figur.
- Bei possession_transfer gilt zwingend: actor = Geber/Verlierer, target = Empfänger, object = Gegenstand UND assertion: subject=Gegenstand, property=possession oder control, value=Empfänger/neuer Besitzer.
- Summary, actors/targets/objects und assertions müssen dieselbe Relation ausdrücken.

replaceRefs enthält NUR T-IDs bereits vorhandener Events, die denselben source-belegten Hochrisiko-Fakt falsch abbilden (z. B. falscher Referent oder invertierte Übergabe). Verwandte, aber eigenständige Events nicht ersetzen.
Gib maximal 6 Fakten pro Abschnitt aus. sourceIds nur aus dem Abschnitt. Keine Routine, keine Spielmechanik, keine bloße Atmosphäre.
Antworte nur mit JSON {"facts":[{"factClass":"…","replaceRefs":["T0001"],"event":{...}}]}. Sprache: {sprache}."""

SYSTEM_LEDGER_RELEVANCE = """Du klassifizierst bereits geprüfte Ledger-Fakten ausschließlich nach ihrer späteren Verwendung. Ändere KEINEN Fakt, keine Relation und keine Epistemik.
Für jedes Event:
- recap=true, wenn es für den verständlichen Sitzungsverlauf, Wendepunkt, Ergebnis, wichtige Entscheidung/Entdeckung oder eine folgenreiche Zustandsänderung gebraucht wird.
- openThread=true, wenn es später gebraucht wird, um einen offenen Faden zu erkennen ODER als gelöst zu markieren: Ziel, Verpflichtung, Gefahr, ungelöste Frage, gesuchte Person/Gegenstand oder fortwirkende Folge.
- bible=true, wenn es dauerhaftes Wissen über eine benannte Person, Ort, Fraktion, Gegenstand, Identität, Rolle, Beziehung, Zugehörigkeit oder bedeutenden Besitz darstellt.
Routine, reine Regelmechanik und folgenlose Kleindetails dürfen alle drei false sein. Mehrere true sind ausdrücklich erlaubt.
Antworte für jede bereitgestellte R-ID genau einmal. Nur JSON {"classifications":[{"ref":"R0001","recap":true,"openThread":false,"bible":true,"reason":"…"}]}. Sprache: {sprache}."""

SYSTEM_LEDGER_ANCHOR_RESOLVE = """Du löst NUR wenige strittige oder fehlende Hochrisiko-Fakten eines Pen-&-Paper-Ledgers gegen kurze Ausschnitte des ORIGINALTRANSKRIPTS auf. Ein HINWEIS ist ausdrücklich KEINE Wahrheit, sondern nur ein Prüfauftrag.
Für jeden Hinweis:
- verdict = confirmed nur wenn der Quellausschnitt die atomare Relation bzw. den Zustand belastbar trägt.
- verdict = rejected wenn der Hinweis dem Ausschnitt widerspricht.
- verdict = unclear wenn weder sicher bestätigt noch verworfen werden kann.
- event: nur bei confirmed ein vollständiges, source-belegtes Ledger-Event mit relevance; sonst null.
Sprecherlabels sind automatisch erkannt und können falsch sein. Berücksichtige die Gesprächsrolle: Fragt ein Spieler unmittelbar nach Zustand/Handlung eines NPCs oder der Spielwelt und folgt genau eine unbestrittene autoritative Weltantwort, kann deren Sprecherlabel falsch sein. Markiere ein bestätigtes Event dann mit tag "speaker_conflict". Bei konkurrierenden Antworten, Scherz/Meinung oder unklarer Gesprächsrolle nicht zur Weltwahrheit hochstufen.
Wer gibt wem was, wer ist wer und wer schuldet wem was niemals umdrehen. Eine Anrede macht Angesprochenen und Sprecher nicht identisch. Reine Regelmechanik ist keine Weltwahrheit.
Antworte nur mit JSON {"resolutions":[{"anchorId":"A0001","verdict":"confirmed|rejected|unclear","event":{...}|null,"reason":"..."}]}. Sprache: {sprache}."""

SYSTEM_LEDGER_COVERAGE = """Du suchst im ORIGINALTRANSKRIPT eines Abschnitts nach WICHTIGEN Ledger-Ereignissen, die in der Liste "BEREITS ERFASST" fehlen. Gib ausschließlich echte Lücken zurück, keine Umformulierungen bereits erfasster Events und keinen Kleinkram.
Priorität: Tod/Überleben, Rettung, schwere Verletzung/Heilung, Transformation, Besitzübergabe mit Richtung, Identität/Verwechslung, Beziehung, Deal/Verpflichtung, entscheidende Entdeckung oder Wissensänderung, Ortswechsel mit Plotfolge. importance nur "critical" oder "important".
"Spielleitung" ist keine Figur; löse NPCs nur aus dem lokalen Kontext auf. Behauptet/geglaubt/Vision ist nicht beobachtete Weltwahrheit. sourceIds dürfen nur aus diesem Abschnitt stammen und müssen die Aussage direkt tragen.
Wenn nichts Relevantes fehlt, events leer. Antworte nur mit JSON {"events": [...]} nach dem vorgegebenen Schema. Sprache: {sprache}."""

SYSTEM_LEDGER_HISTORY = """Du ordnest aktuelle, source-belegte Ledger-Events in eine bereits bekannte Kampagnenhistorie ein. Die aktuellen Events sind Wahrheit über diese Session nur in dem epistemischen Status, der dort steht. Historische Quellen haben IDs Hxxxx und stammen aus früheren Recaps oder bestätigter öffentlicher Bibel.
Für jedes aktuelle Event mit passenden historischen Quellen entscheide:
- confirms: bestätigt denselben Zustand/Fakt.
- extends: ergänzt denselben bereits begonnenen Zustand, dieselbe Beziehung, Verpflichtung oder denselben konkreten Handlungsfaden.
- contradicts: aktuelles und historisches Wissen sind unvereinbar, aber es ist keine klare spätere Auflösung.
- revises: das neue Ereignis erklärt oder korrigiert einen früher geglaubten/berichteten Zustand (z. B. jemand galt als tot und erscheint lebend; eine frühere Aussage wird als Lüge entlarvt).
- none: keine belastbare Beziehung.
WICHTIG: Dass in beiden Texten nur dieselbe Person oder derselbe Ort vorkommt, ist KEINE Beziehung und immer "none". Eine Rollenbeschreibung in der Bibel wird nicht durch irgendeine spätere Handlung derselben Figur "erweitert". Bei einem echten Zustandswechsel (verletzt → geheilt, Ort A → Ort B) normalerweise extends, nicht contradicts. Ein früherer Recap ist historische Quelle, keine absolute Weltwahrheit.
historyIds nur aus den bereitgestellten H-IDs. Keine neuen Fakten. Antworte nur mit JSON {"links": [{"eventId":"E0001","relation":"…","historyIds":["H0001"],"reason":"…"}]}. Sprache: {sprache}."""

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


_RELATION_KURZ = {"tot", "lebt", "leben", "gab", "gibt", "nahm", "kennt", "kind", "bruder", "vater", "mutter",
                  "besuch", "lüge", "luege", "lügt", "rettet", "rettete", "heilt", "stirbt", "gruppe"}


def _relation_anker(text: str) -> set[str]:
    """Billiger positiver Gegenbeleg: ein Widerspruch braucht im echten Source-Text wenigstens ein inhaltliches
    Ankerwort aus Claim/Korrektur. Das verhindert, dass bloße Abwesenheit im lokalen Fenster als Widerspruch patcht."""
    return {w for w in _notizkern(text).split()
            if (len(w) >= 5 and w not in _FUELL) or w in _RELATION_KURZ}


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
    return re.sub(r"[ \t]+\n", "\n", t).strip()


RECAP_SCHLUESSEL = ("text", "recap", "summary", "zusammenfassung", "body", "content", "story", "inhalt")



def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


S_STR = {"type": "string"}
S_BOOL = {"type": "boolean"}
S_ARR_STR = {"type": "array", "items": S_STR}
S_LEDGER_RELEVANCE = _obj({
    "recap": S_BOOL,
    "openThread": S_BOOL,
    "bible": S_BOOL,
}, ["recap", "openThread", "bible"])
S_LEDGER_ASSERTION = _obj({
    "subject": S_STR,
    "property": {"type": "string", "enum": ["life_status", "physical_condition", "location", "possession",
                                               "relationship", "identity", "knowledge", "allegiance", "goal",
                                               "obligation", "reputation", "control", "role_status", "other"]},
    "value": S_STR,
    "epistemic": {"type": "string", "enum": ["observed", "stated", "reported", "believed", "suspected",
                                                "remembered", "vision", "dream", "inferred", "unknown"]},
    "certainty": {"type": "string", "enum": ["high", "medium", "low"]},
}, ["subject", "property", "value", "epistemic", "certainty"])
S_LEDGER_EVENT = _obj({
    "sourceIds": {"type": "array", "minItems": 1, "maxItems": 6,
                  "items": {"type": "string", "pattern": "^L[0-9]{4,6}$"}},
    "summary": S_STR,
    "kinds": {"type": "array", "minItems": 1, "items": {"type": "string", "enum": [
        "action", "interaction", "state_change", "relationship", "possession", "knowledge", "goal", "commitment",
        "location_change", "identity", "status", "condition", "creation_destruction", "resource_change",
        "discovery", "conflict", "transformation", "death_return", "travel", "scene_change", "other"
    ]}},
    "actors": S_ARR_STR, "targets": S_ARR_STR, "objects": S_ARR_STR, "locations": S_ARR_STR, "factions": S_ARR_STR,
    "assertions": {"type": "array", "items": S_LEDGER_ASSERTION},
    "epistemic": {"type": "string", "enum": ["observed", "stated", "reported", "believed", "suspected",
                                                "remembered", "vision", "dream", "inferred", "unknown"]},
    "modality": {"type": "string", "enum": ["actual", "attempted", "planned", "hypothetical", "alleged", "unknown"]},
    "importance": {"type": "string", "enum": ["critical", "important", "minor"]},
    "relevance": S_LEDGER_RELEVANCE,
    "tags": S_ARR_STR,
}, ["sourceIds", "summary", "kinds", "actors", "targets", "objects", "locations", "factions", "assertions",
    "epistemic", "modality", "importance", "tags"])
S_LEDGER_ANCHOR = _obj({
    "originIds": {"type": "array", "items": {"type": "string", "pattern": "^C[0-9]{4,6}$"}},
    "sourceIds": {"type": "array", "minItems": 1, "maxItems": 6,
                  "items": {"type": "string", "pattern": "^L[0-9]{4,6}$"}},
    "subject": S_STR,
    "property": {"type": "string", "enum": ["life_status", "physical_condition", "location", "possession",
                                               "relationship", "identity", "knowledge", "allegiance", "goal",
                                               "obligation", "reputation", "control", "role_status"]},
    "value": S_STR,
    "epistemic": {"type": "string", "enum": ["observed", "stated", "reported", "believed", "suspected",
                                                "remembered", "vision", "dream", "inferred", "unknown"]},
    "certainty": {"type": "string", "enum": ["high", "medium", "low"]},
    "importance": {"type": "string", "enum": ["critical", "important"]},
}, ["originIds", "sourceIds", "subject", "property", "value", "epistemic", "certainty", "importance"])
S_LEDGER_CRITICAL_FACT = _obj({
    "factClass": {"type": "string", "enum": ["life_status", "physical_condition", "rescue_aid",
                                                  "possession_transfer", "identity_role", "plot_location",
                                                  "goal_obligation"]},
    "replaceRefs": {"type": "array", "items": {"type": "string", "pattern": "^T[0-9]{4,6}$"}},
    "event": S_LEDGER_EVENT,
}, ["factClass", "replaceRefs", "event"])
S_LEDGER_CRITICAL = _obj({"facts": {"type": "array", "maxItems": 6, "items": S_LEDGER_CRITICAL_FACT}}, ["facts"])
S_LEDGER_RELEVANCE_CLASS = _obj({
    "ref": {"type": "string", "pattern": "^R[0-9]{4,6}$"},
    "recap": S_BOOL, "openThread": S_BOOL, "bible": S_BOOL, "reason": S_STR,
}, ["ref", "recap", "openThread", "bible", "reason"])
S_LEDGER_RELEVANCE_RESULT = _obj({
    "classifications": {"type": "array", "items": S_LEDGER_RELEVANCE_CLASS}
}, ["classifications"])
S_LEDGER_ANCHOR_RESOLUTION = _obj({
    "resolutions": {"type": "array", "items": _obj({
        "anchorId": {"type": "string", "pattern": "^A[0-9]{4,6}$"},
        "verdict": {"type": "string", "enum": ["confirmed", "rejected", "unclear"]},
        "event": {"anyOf": [S_LEDGER_EVENT, {"type": "null"}]},
        "reason": S_STR,
    }, ["anchorId", "verdict", "event", "reason"])},
}, ["resolutions"])
S_LEDGER_ENCOUNTER_FRAGMENT = _obj({
    "sourceIds": {"type": "array", "minItems": 1, "maxItems": 12,
                  "items": {"type": "string", "pattern": "^L[0-9]{4,6}$"}},
    "kind": {"type": "string", "enum": ["combat", "chase", "conflict", "social_conflict", "other"]},
    "boundary": {"type": "string", "enum": ["start", "middle", "end", "complete", "unknown"]},
    "participants": S_ARR_STR, "locations": S_ARR_STR, "objectives": S_ARR_STR, "domains": S_ARR_STR,
    "summary": S_STR,
    "turningPoints": S_ARR_STR, "outcomes": S_ARR_STR, "consequences": S_ARR_STR, "unresolved": S_ARR_STR,
}, ["sourceIds", "kind", "boundary", "participants", "locations", "objectives", "domains", "summary",
    "turningPoints", "outcomes", "consequences", "unresolved"])
S_SCHEMAS = {
    "notes": _obj({"notizen": {"type": "array", "items": S_STR}, "_gerettet": {"type": "boolean"}}, ["notizen"]),
    "plan": _obj({
        "critical": {"type": "array", "items": S_STR},
        "important": {"type": "array", "items": S_STR},
        "minor": {"type": "array", "items": S_STR},
    }, ["critical", "important", "minor"]),
    "recap": _obj({
        "title": S_STR, "text": S_STR, "openThreads": {"type": "array", "items": S_STR}
    }, ["title", "text", "openThreads"]),
    "missing": _obj({"fehlend": {"type": "array", "items": _obj({
        "zeit": S_STR, "wichtigkeit": {"type": "string", "enum": ["kritisch", "wichtig", "nebensächlich"]},
        "notiz": S_STR
    }, ["zeit", "notiz"])}}, ["fehlend"]),
    "paragraphs": _obj({"absaetze": {"type": "array", "items": _obj({
        "nr": {"type": "integer"}, "text": S_STR
    }, ["nr", "text"])}}, ["absaetze"]),
    "review": _obj({"absaetze": {"type": "array", "items": _obj({
        "nr": {"type": "integer"},
        "urteil": {"type": "string", "enum": ["belegt", "teilweise", "unbelegt", "widerspricht", "witz"]},
        "stellen": {"type": "array", "items": _obj({"zeit": S_STR, "zitat": S_STR}, ["zeit", "zitat"])},
        "begruendung": S_STR
    }, ["nr", "urteil"])}}, ["absaetze"]),
    "part": _obj({"zusammenfassung": S_STR}, ["zusammenfassung"]),
    "proposals": _obj({"proposals": {"type": "array", "items": _obj({
        "entryType": S_STR,
        "action": S_STR,
        "targetEntryId": {"type": ["string", "null"]},
        "title": S_STR, "detail": S_STR,
        "gmNotes": {"type": ["string", "null"]},
        "suggestedVisibility": S_STR,
        "visibilityReason": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "flags": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": _obj({"start": S_STR, "quote": S_STR}, ["start", "quote"])}
    }, ["entryType", "action", "title"])}}, ["proposals"]),
    "ledger": _obj({"events": {"type": "array", "items": S_LEDGER_EVENT}}, ["events"]),
    "ledger_review": _obj({
        "reviews": {"type": "array", "items": _obj({
            "originIds": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^C[0-9]{4,6}$"}},
            "verdict": {"type": "string", "enum": ["repair", "merge", "reject"]},
            "reason": S_STR,
            "replacement": {"anyOf": [S_LEDGER_EVENT, {"type": "null"}]},
        }, ["originIds", "verdict", "reason", "replacement"])},
        "coverage": {"type": "array", "items": S_LEDGER_EVENT},
        "anchors": {"type": "array", "maxItems": 4, "items": S_LEDGER_ANCHOR},
        "encounters": {"type": "array", "maxItems": 1, "items": S_LEDGER_ENCOUNTER_FRAGMENT},
    }, ["reviews", "coverage", "anchors", "encounters"]),
    "ledger_critical": S_LEDGER_CRITICAL,
    "ledger_relevance": S_LEDGER_RELEVANCE_RESULT,
    "ledger_anchor_resolution": S_LEDGER_ANCHOR_RESOLUTION,
    "ledger_history": _obj({"links": {"type": "array", "items": _obj({
        "eventId": S_STR,
        "relation": {"type": "string", "enum": ["confirms", "extends", "contradicts", "revises", "none"]},
        "historyIds": {"type": "array", "items": {"type": "string", "pattern": "^H[0-9]{4,6}$"}},
        "reason": S_STR,
    }, ["eventId", "relation", "historyIds", "reason"])}}, ["links"]),
    "relations": _obj({"claims": {"type": "array", "items": _obj({
        "claim": S_STR,
        "urteil": {"type": "string", "enum": ["stimmt", "widerspricht", "unklar"]},
        "korrektur": S_STR, "begruendung": S_STR,
        "sourceIds": {"type": "array", "items": {"type": "string", "pattern": "^L[0-9]{4,6}$"}}
    }, ["claim", "urteil", "korrektur", "begruendung", "sourceIds"])}}, ["claims"]),
}


def _schema_fuer(system: str) -> dict | None:
    if system.startswith("Du hilfst bei der Nachbereitung einer langen"):
        return S_SCHEMAS["part"]
    if system.startswith("Du hilfst bei der Nachbereitung") and "Schreibe Szenennotizen" in system:
        return S_SCHEMAS["notes"]
    if system.startswith("Du klassifizierst die Szenennotizen"):
        return S_SCHEMAS["plan"]
    if "Was bisher geschah" in system:
        return S_SCHEMAS["recap"]
    if system.startswith("Du pflegst die Kampagnen-Bibel"):
        return S_SCHEMAS["proposals"]
    if system.startswith("Du vergleichst den Recap"):
        return S_SCHEMAS["missing"]
    if system.startswith("Du sicherst wenige HOCHRISIKO-FAKTEN"):
        return S_SCHEMAS["ledger_critical"]
    if system.startswith("Du klassifizierst bereits geprüfte Ledger-Fakten"):
        return S_SCHEMAS["ledger_relevance"]
    if system.startswith("Du extrahierst") or system.startswith("Du suchst im ORIGINALTRANSKRIPT"):
        return S_SCHEMAS["ledger"]
    if system.startswith("Du prüfst Ledger-Kandidaten"):
        return S_SCHEMAS["ledger_review"]
    if system.startswith("Du löst NUR wenige strittige"):
        return S_SCHEMAS["ledger_anchor_resolution"]
    if system.startswith("Du ordnest aktuelle"):
        return S_SCHEMAS["ledger_history"]
    if system.startswith("Du prüfst genau EINEN Absatz"):
        return S_SCHEMAS["relations"]
    if system.startswith("Du prüfst den Recap"):
        return S_SCHEMAS["review"]
    if system.startswith("Du ergänzt den Recap") or system.startswith("Du überarbeitest einzelne Absätze"):
        return S_SCHEMAS["paragraphs"]
    return None


def _schema_pruefen(d: dict, schema: dict | None) -> list[str]:
    if not schema:
        return []
    fehler = sorted(jsonschema.Draft202012Validator(schema).iter_errors(d), key=lambda e: list(e.path))
    aus = []
    for e in fehler[:8]:
        pfad = ".".join(str(x) for x in e.path) or "(Wurzel)"
        aus.append(f"{pfad}: {e.message}")
    return aus


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
    def als_text(v) -> str:
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, list) and v and all(isinstance(a, str) for a in v):
            return "\n\n".join(a.strip() for a in v if a.strip())
        return ""

    for k in RECAP_SCHLUESSEL:
        t = als_text(d.get(k))
        if t:
            return t
    for v in d.values():  # eine Ebene verschachtelt: {"recap": {"title": …, "text": …}}
        if isinstance(v, dict):
            for k in RECAP_SCHLUESSEL:
                t = als_text(v.get(k))
                if t:
                    return t
    lang = [als_text(v) for v in d.values() if len(als_text(v)) >= 200]
    return lang[0] if len(lang) == 1 else ""


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

    def aufruf(self, klient: Klient, system: str, nutzer: str, retten: bool = False) -> dict:
        schema = _schema_fuer(system)
        letzter_fehler = ""
        for versuch in range(2):
            extra = ""
            if versuch:
                extra = ("\n\nDie vorige Ausgabe wurde vom Harness abgelehnt. Korrigiere NUR die Ausgabe nach "
                         "dem verlangten Schema. Fehler:\n- " + letzter_fehler.replace("\n", "\n- "))
            a = self._chat(klient, system, nutzer + extra, schema)
            if a.done_reason == "length" and not retten:
                letzter_fehler = "Antwort wurde wegen der Ausgabelänge abgeschnitten"
                continue
            try:
                d = _json(a, retten)
            except SprachmodellFehler as e:
                letzter_fehler = str(e)
                continue
            fehler = _schema_pruefen(d, schema)
            if not fehler:
                return d
            letzter_fehler = "\n".join(fehler)
        raise AntwortFehler(f"Strukturierte Ausgabe nach Reparatur weiter ungültig: {letzter_fehler[:600]}")

    def _chat(self, klient: Klient, system: str, nutzer: str, schema: dict | None = None) -> Antwort:
        strukturiert = getattr(klient, "chat_strukturiert", None)
        a = strukturiert(system, nutzer, schema) if schema and callable(strukturiert) else klient.chat(system, nutzer)
        self.letzte_antwort = a.text
        self.tokens_in += a.tokens_in
        self.tokens_out += a.tokens_out
        self.aufrufe += 1
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


def _plan_teile_nach_zeit(notizen: list[str]) -> list[str]:
    """Szenennotizen für den Pflichtplan in feste PLAN_MINUTEN-Fenster teilen. Anders als _teile_nach_zeit werden
    dichte Ereignisfolgen nicht auf wenige größere Blöcke gerundet: Hinrichtung, Rettung, Tod und Besitzwechsel sollen
    nicht gegeneinander um wenige Planplätze konkurrieren."""
    zeiten = [t for t in (_zeit_vorn(n) for n in notizen) if t is not None]
    if not zeiten:
        return stuecke(notizen, TEIL_TOKEN)
    von = min(zeiten)
    fenster_s = PLAN_MINUTEN * 60
    gruppen: dict[int, list[str]] = {}
    letzte = 0
    for n in notizen:
        t = _zeit_vorn(n)
        if t is not None:
            letzte = max(0, int((t - von) // fenster_s))
        gruppen.setdefault(letzte, []).append(n)
    teile = []
    for nr in sorted(gruppen):
        teile += stuecke(gruppen[nr], TEIL_TOKEN)
    return teile


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
    letzter_plan: list = field(default_factory=list)  # Klassifikation aller Plan-Notizen vor der Prosa (Modellvergleich)
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
    ledger_shadow: bool = False  # 0.4.52: geprüfter Ledger bleibt Schattenmodus; beeinflusst Recap/Bibel niemals
    letztes_ledger: dict = field(default_factory=dict)

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
        if tokens(text) <= self.max_transkript_tokens:
            return "Transkript", text
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
            d = self.zaehler.aufruf(self.klient, system, vorspann + "\n".join(zeilen), retten=True)
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
        kopien = _zeilenkerne(zeilen)
        notizen, zuletzt = [], None
        for n in d.get("notizen") or []:
            n = _notiz_normieren(str(n))
            m = _ZEIT_VORN.match(n)
            if m:
                zuletzt = f"[{m.group(1)}]"
            elif zuletzt is not None:
                n = f"{zuletzt} {n}"  # Notiz ohne Zeit gehört zur Stelle davor
            if not n or _REGELN.search(n) or _bruchstueck(n):
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

    def planen(self, ein: dict, notizen: str) -> list[dict]:
        """0.4.50: Jede Szenennotiz in kurzen Zeitfenstern klassifizieren statt Top-N auszuwählen. Kritisch und
        wichtig werden später Pflichtpunkte; nebensächlich bleibt in plan.json sichtbar. Fehlt eine gültige ID in
        der Modellantwort, fällt sie auf 'wichtig' zurück, damit eine Auslassung des Klassifizierers keine Information
        still entfernt."""
        zeilen = [z.strip() for z in notizen.split("\n") if z.strip() and _zeit_vorn(z) is not None]
        if not zeilen:
            return []
        ids = {z: f"N{i + 1:03d}" for i, z in enumerate(zeilen)}
        teile = _plan_teile_nach_zeit(zeilen)
        system = SYSTEM_RECAP_PLAN.replace("{sprache}", _sprache(ein))
        aus = []
        for i, teil in enumerate(teile):
            teil_zeilen = [z.strip() for z in teil.split("\n") if z.strip() and z.strip() in ids]
            if not teil_zeilen:
                continue
            erlaubt = {ids[z]: z for z in teil_zeilen}
            liste = "\n".join(f"{ids[z]} | {z}" for z in teil_zeilen)
            try:
                d = self.zaehler.aufruf(
                    self.klient, system,
                    f"{_kopf(ein)}\n\nZeitabschnitt {i + 1} von {len(teile)} (etwa {PLAN_MINUTEN} Minuten):\n{liste}")
            except SprachmodellFehler as e:
                log.warning("Recap-Plan: Abschnitt %d übersprungen: %s", i + 1, e)
                d = {}

            def ids_aus(*namen: str) -> set[str]:
                werte = []
                for name in namen:
                    v = d.get(name)
                    if isinstance(v, list):
                        werte += [str(x).strip() for x in v]
                return {x for x in werte if x in erlaubt}

            kritisch = ids_aus("critical", "kritisch")
            wichtig = ids_aus("important", "wichtig") - kritisch
            neben = ids_aus("minor", "nebensächlich", "nebensaechlich") - kritisch - wichtig
            klassifiziert = kritisch | wichtig | neben

            # Recall vor Kürze: Nicht klassifizierte gültige IDs werden Pflicht statt still verloren zu gehen.
            fehlend = set(erlaubt) - klassifiziert
            wichtig |= fehlend

            for nid, z in erlaubt.items():  # chronologische Reihenfolge aus der Grundlage
                if nid in kritisch:
                    rang = "kritisch"
                elif nid in wichtig:
                    rang = "wichtig"
                else:
                    rang = "nebensächlich"
                aus.append({
                    "id": nid,
                    "zeit": _zeit_vorn(z),
                    "notiz": z,
                    "teil": i + 1,
                    "wichtigkeit": rang,
                    "fallback": nid in fehlend,
                })
        return aus

    def recap(self, ein: dict, titel: str, grundlage: str,
              pflichtplan: list[dict] | None = None) -> dict:
        bibel = "\n".join(f"- [{e['typ']}] {e['name']}" + (f": {e['zusammenfassung'][:500]}"
                                                           if _erwaehnt(e["name"], grundlage) else "")
                          for e in ein["bibel"])
        pflicht = ""
        if pflichtplan:
            punkte = [p for p in pflichtplan if p.get("wichtigkeit") in ("kritisch", "wichtig")]
            if punkte:
                pz = "\n".join(
                    f"- [{p['wichtigkeit']}] Abschnitt {p['teil']}: {p['id']} {p['notiz']}" for p in punkte)
                pflicht = ("\n\nPflichtplan (nur aus der Grundlage ausgewählt; JEDER Punkt muss im Recap vorkommen):\n"
                           + pz)
        nutzer = (f"{_kopf(ein)}\n\nBekannt aus früheren Sessions (Spielerwissen):\n{bibel or '(noch nichts)'}"
                  f"{pflicht}\n\n{titel}:\n{grundlage}")
        system = (SYSTEM_RECAP.replace("{sprache}", _sprache(ein)).replace("{nummer}", str(ein["session_nummer"]))
                  .replace("{woerter}", woerter(ein)))
        self._schritt("recap")
        d = self.zaehler.aufruf(self.klient, system, nutzer)
        text = recap_text(d)
        if not text:
            # Nur Schlüssel und Längen ins Protokoll – nie Inhalte
            form = ", ".join(f"{k}:{_form(v)}" for k, v in d.items()) or "leer"
            log.warning("Sprachmodell: Recap-Antwort ohne text (%s)", form)
            raise SprachmodellFehler(f"Das Sprachmodell hat keinen Recap geliefert (Antwort: {form}).")
        faeden = [klartext(f)[:300] for f in (d.get("openThreads") or d.get("open_threads") or []) if klartext(f)][:10]
        text = klartext(text)
        if "\n\n" not in text and "\n" in text:  # Absätze nur mit einfachem Umbruch – für App und Prüfung trennen
            text = re.sub(r"\n+", "\n\n", text)
        return {"title": klartext(d.get("title") or d.get("titel"))[:300], "text": text, "openThreads": faeden}

    def vollstaendigkeit(self, ein: dict, titel: str, grundlage: str, text: str) -> list[dict]:
        """Was fehlt im Recap, obwohl es in der Grundlage steht? Liefert [{zeit, notiz, belegt}]; nur belegte Punkte
        (in der Grundlage wiedergefunden) werden ergänzt."""
        system = (SYSTEM_VOLLSTAENDIGKEIT.replace("{sprache}", _sprache(ein))
                  .replace("{hoechstens}", str(FEHLEND_HOECHSTENS)))
        nutzer = f"{_kopf(ein)}\n\n{titel}:\n{grundlage}\n\nRecap:\n{text}"
        self._schritt("review")
        return fehlend_lesen(self.zaehler.aufruf(self.klient, system, nutzer), grundlage)

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

    def relationen(self, ein: dict, text: str, befund: list[dict]) -> list[dict]:
        """Atomare Beziehungen je Absatz gegen kurze Ausschnitte des Originaltranskripts prüfen. Ein Widerspruch
        wird nur übernommen, wenn Claim UND echte Source-ID(s) aus genau diesem Ausschnitt belegt sind."""
        zeilen = [(_zeit_vorn(z), lid, z) for lid, z in transkript_zeilen_mit_ids(ein.get("transkript") or [])]
        zeilen = [(t, lid, z) for t, lid, z in zeilen if t is not None]
        teile = absaetze(text)
        aus = []
        for b in befund:
            i = b.get("index", -1)
            zeiten = sorted({e["start"] for e in b.get("evidence") or [] if e.get("start") is not None})[:3]
            if not (0 <= i < len(teile)) or not zeiten:
                continue
            spans = [(lid, z) for t, lid, z in zeilen if any(abs(t - zt) <= RELATION_FENSTER_S for zt in zeiten)]
            text_spans = "\n".join(f"{lid} | {z}" for lid, z in spans)[:RELATION_ZEICHEN]
            erlaubte_ids = {lid: z for lid, z in spans if f"{lid} |" in text_spans}
            if not text_spans.strip():
                continue
            fenster = [_zeit(zt) for zt in zeiten]
            nutzer = (f"{_kopf(ein)}\n\nAbsatz {i + 1}:\n{teile[i]}\n\n"
                      f"Transkript dazu ({', '.join(fenster)}):\n{text_spans}")
            self._schritt("review")
            try:
                d = self.zaehler.aufruf(self.klient, SYSTEM_RELATIONEN.replace("{sprache}", _sprache(ein)), nutzer)
            except SprachmodellFehler as e:
                log.warning("Relationsprüfung Absatz %d übersprungen: %s", i + 1, e)
                continue

            claims = []
            for c in d.get("claims") or []:
                if not isinstance(c, dict):
                    continue
                claim = klartext(c.get("claim") or "")[:500]
                if not claim:
                    continue
                urteil = str(c.get("urteil") or "").strip().lower()
                urteil = {"stimmt": "stimmt", "widerspricht": "widerspricht", "unklar": "unklar"}.get(urteil, "unklar")
                exakt = claim in teile[i]
                source_ids = [str(x).strip() for x in (c.get("sourceIds") or [])
                              if str(x).strip() in erlaubte_ids][:3]
                zitat = " | ".join(erlaubte_ids[x] for x in source_ids)[:600]
                belegt = bool(source_ids)
                korrektur = klartext(c.get("korrektur") or "")[:500]
                claim_anker = _relation_anker(claim)
                korrektur_anker = _relation_anker(korrektur)
                # Für einen automatischen Patch muss der Beleg mindestens einen Inhalt tragen, der in der Korrektur
                # neu ist. Bloß dieselbe Figur oder dasselbe Thema zu erwähnen reicht nicht als Gegenbeleg.
                gegenbeleg = bool(belegt and korrektur
                                  and (_relation_anker(zitat) & (korrektur_anker - claim_anker)))
                # Automatisch eingreifen nur mit drei harten Ankern: exakter Recap-Claim, echte Source-ID(s) und
                # positiver inhaltlicher Gegenbeleg. "Im Fenster nicht gefunden" ist ausdrücklich kein Widerspruch.
                if urteil == "widerspricht" and (not exakt or not gegenbeleg):
                    urteil = "unklar"
                claims.append({
                    "claim": claim,
                    "urteil": urteil,
                    "korrektur": korrektur,
                    "begruendung": klartext(c.get("begruendung") or "")[:400],
                    "sourceIds": source_ids,
                    "zitat": zitat,
                    "zitatBelegt": belegt,
                    "gegenbelegBelegt": gegenbeleg,
                    "exakt": exakt,
                    "gepatcht": False,
                })
            widerspruch = [c for c in claims if c["urteil"] == "widerspricht" and c["exakt"]
                            and c.get("gegenbelegBelegt")]
            if widerspruch:
                urteil = "widerspricht"
            elif any(c["urteil"] == "unklar" for c in claims):
                urteil = "unklar"
            else:
                urteil = "stimmt"
            erster = widerspruch[0] if widerspruch else {}
            eintrag = {"index": i, "urteil": urteil, "begruendung": erster.get("begruendung", ""),
                       "zitat": erster.get("zitat", ""), "fenster": fenster, "claims": claims}
            aus.append(eintrag)
            if widerspruch:
                b["relation_contradicted"] = True
                b["verdict"] = "contradicted"
                b["note"] = ("Transkript: " + (erster.get("begruendung") or "Beziehung anders als im Recap")
                             + (f" („{erster.get('zitat')}“)" if erster.get("zitat") else ""))[:600]
        return aus

    def relationen_patchen(self, text: str, relationen: list[dict]) -> str | None:
        """Nur den exakt beanstandeten Claim ersetzen. Unsichere oder nicht sicher korrigierbare Claims bleiben
        unverändert und damit für die SL sichtbar."""
        teile = absaetze(text)
        geaendert = False
        for r in relationen:
            i = r.get("index", -1)
            if not (0 <= i < len(teile)):
                continue
            for c in r.get("claims") or []:
                if (c.get("urteil") != "widerspricht" or not c.get("exakt")
                        or not c.get("gegenbelegBelegt")):
                    continue
                alt = str(c.get("claim") or "").strip()
                neu = klartext(c.get("korrektur") or "")
                if (len(alt) < 8 or not neu or teile[i].count(alt) != 1
                        or len(neu) > max(500, len(alt) * 2 + 120) or alt == neu):
                    continue
                teile[i] = teile[i].replace(alt, neu, 1)
                c["gepatcht"] = True
                geaendert = True
        return "\n\n".join(teile) if geaendert else None

    def pruefen(self, ein: dict, titel: str, grundlage: str, text: str) -> list[dict]:
        """Gegenprüfung (Stufe 3) in kleinen Absatzpaketen. Ein abgeschnittener strukturierter Call macht nur sein
        Paket 'unchecked', nicht mehr die gesamte Gegenprüfung."""
        teile = absaetze(text)
        if not teile:
            return []
        system = SYSTEM_PRUEFUNG.replace("{sprache}", _sprache(ein))
        aus = []
        for ab in range(0, len(teile), PRUEF_BATCH):
            paket = teile[ab:ab + PRUEF_BATCH]
            # Lokal 1..N nummerieren hält Schema und Antwort klein; danach auf globale Indizes zurücksetzen.
            liste = "\n\n".join(f"Absatz {i + 1}:\n{a}" for i, a in enumerate(paket))
            nutzer = f"{_kopf(ein)}\n\n{titel}:\n{grundlage}\n\nRecap, Absatz für Absatz:\n{liste}"
            try:
                teil = pruefung_lesen(self.zaehler.aufruf(self.klient, system, nutzer), len(paket))
            except SprachmodellFehler as e:
                log.warning("Gegenprüfung Absätze %d–%d übersprungen: %s", ab + 1, ab + len(paket), e)
                teil = [{"index": i, "verdict": "unchecked", "note": None, "evidence": []}
                        for i in range(len(paket))]
            for b in teil:
                b["index"] += ab
            aus += teil
        return aus

    def nachbessern(self, ein: dict, titel: str, grundlage: str, text: str, befund: list[dict]) -> str | None:
        """Beanstandete Absätze einmal neu schreiben lassen. None, wenn nichts zu tun war oder nichts kam."""
        teile = absaetze(text)
        schlecht = [b for b in befund if b["verdict"] in BEANSTANDET and b["index"] < len(teile)
                    and not b.get("relation_contradicted")]
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
        """Faktenprüfung plus transcript-backed Relationsprüfung. Relationsfehler werden ausschließlich claimweise
        gepatcht; ein Kandidat gilt erst nach einem zweiten transcript-backed Prüflauf als akzeptiert."""
        pruefung = {"model": self.klient.modell, "revised": False, "paragraphs": []}
        try:
            def prueflauf(text: str) -> tuple[list[dict], list[dict]]:
                self._schritt("review")
                b = spielleitung_beanstanden(self.pruefen(ein, titel, grundlage, text), text)
                rel = self.relationen(ein, text, b)
                return b, rel

            befund, relationen = prueflauf(r["text"])
            self.letzte_pruefung_vorher, self.letzte_pruefung_nachher = befund, []
            self.letzte_relationen_vorher, self.letzte_relationen_nachher = relationen, []

            kandidat = self.relationen_patchen(r["text"], relationen)
            if kandidat:
                gepatchte = {x.get("index") for x in relationen
                             if any(c.get("gepatcht") for c in x.get("claims") or [])}
                b2, rel2 = prueflauf(kandidat)
                r2 = {x.get("index"): x for x in rel2}
                b2_map = {x.get("index"): x for x in b2}
                sicher = bool(gepatchte) and all(
                    r2.get(i, {}).get("urteil") == "stimmt"
                    and b2_map.get(i, {}).get("verdict") not in BEANSTANDET
                    for i in gepatchte
                )
                if sicher:
                    r["text"], pruefung["revised"] = kandidat, True
                    befund, relationen = b2, rel2
                    self.letzte_pruefung_nachher = befund
                    self.letzte_relationen_nachher = relationen
                else:
                    # False Positive oder weiterhin unsicher: Originaltext bleibt vollständig erhalten.
                    for x in self.letzte_relationen_vorher:
                        for c in x.get("claims") or []:
                            if c.get("gepatcht"):
                                c["gepatcht"] = False
                                c["zurueckgenommen"] = True

            # Andere harte Fehler (unbelegt/off-game) dürfen wie bisher einmal paragraphenweise überarbeitet werden.
            # Ein offener transcript-backed Relationswiderspruch ist davon ausdrücklich ausgeschlossen.
            if any(b["verdict"] in BEANSTANDET and not b.get("relation_contradicted") for b in befund):
                self._schritt("revision")
                neu = self.nachbessern(ein, titel, grundlage, r["text"], befund)
                if neu:
                    r["text"], pruefung["revised"] = neu, True
                    befund, relationen = prueflauf(r["text"])
                    self.letzte_pruefung_nachher = befund
                    self.letzte_relationen_nachher = relationen

            pruefung["paragraphs"] = befund
        except SprachmodellFehler as e:
            log.warning("Gegenprüfung übersprungen: %s", e)
            pruefung["paragraphs"] = [{"index": i, "verdict": "unchecked", "note": None, "evidence": []}
                                      for i in range(len(absaetze(r["text"])))]
        return pruefung

    @staticmethod
    def _ledger_event_text(e: dict) -> str:
        teile = [e.get("summary", "")] + list(e.get("actors") or []) + list(e.get("targets") or [])
        teile += list(e.get("objects") or []) + list(e.get("locations") or []) + list(e.get("factions") or [])
        teile += [f"{a.get('subject', '')} {a.get('property', '')} {a.get('value', '')}"
                  for a in e.get("assertions") or [] if isinstance(a, dict)]
        return " ".join(str(x) for x in teile if x)

    def _ledger_event_normalisieren(self, e: dict, erlaubte_ids: set[str],
                                  quelle: dict[str, str]) -> dict | None:
        """Ein Modell-Event auf echte Source-IDs reduzieren und Beleg/Zeit deterministisch aus dem Original ableiten."""
        if not isinstance(e, dict):
            return None
        ids = [str(x) for x in e.get("sourceIds") or [] if str(x) in erlaubte_ids and str(x) in quelle][:6]
        summary = klartext(e.get("summary") or "")[:500]
        if not ids or not summary:
            return None
        relevance = e.get("relevance") if isinstance(e.get("relevance"), dict) else {}
        relevance = {"recap": relevance.get("recap") is True,
                     "openThread": relevance.get("openThread") is True,
                     "bible": relevance.get("bible") is True}
        event = {**e, "sourceIds": ids, "summary": summary, "relevance": relevance}
        def evidence(lid: str) -> dict:
            text = quelle[lid]
            m = re.match(r"^\s*\[[^\]]+\]\s*([^:]+):", text)
            return {
                "sourceType": "TRANSCRIPT",
                "sourceId": lid,
                "sourceLocation": lid,
                "relation": "SUPPORTS",
                "timestampStart": _zeit_vorn(text),
                "speakerLabel": klartext(m.group(1)) if m else None,
                "excerptRef": lid,
                "text": text,
            }
        event["evidence"] = [evidence(lid) for lid in ids]
        zeiten = [_zeit_vorn(quelle[lid]) for lid in ids]
        event["time"] = min((t for t in zeiten if t is not None), default=None)
        return event

    @staticmethod
    def _ledger_quellteile(zeilen: list[tuple[str, str]]) -> list[list[str]]:
        """Dieselben stabilen Quellblöcke für Extraktion, Review und Coverage."""
        return [teil.splitlines() for teil in stuecke([f"{lid} | {z}" for lid, z in zeilen], LEDGER_STUECK_TOKEN)]

    def _ledger_pass(self, ein: dict, system: str, zeilen: list[tuple[str, str]]) -> list[dict]:
        """Ein Ledger-Pass über Originalzeilen. Strukturfehler/Truncation teilen nur den betroffenen Block rekursiv."""
        quelle = {lid: z for lid, z in zeilen}
        aus = []
        teile = self._ledger_quellteile(zeilen)
        phase = "events" if system == SYSTEM_LEDGER_EVENTS else "continuity"

        def verarbeiten(block: list[str], label: str, tiefe: int = 0) -> None:
            if not block:
                return
            im_teil = {m.group(1) for z in block if (m := re.match(r"^(L\d{4,6}) \|", z))}
            self._schritt(f"ledger.{phase} {label}/{len(teile)}")
            try:
                d = self.zaehler.aufruf(
                    self.klient, system.replace("{sprache}", _sprache(ein)),
                    f"{_kopf(ein)}\n\nAbschnitt {label} von {len(teile)}:\n" + "\n".join(block))
            except AntwortFehler as e:
                if len(block) >= 4 and tiefe < 5:
                    mitte = len(block) // 2
                    log.info("Ledger-%s Abschnitt %s wird nach Strukturfehler geteilt: %s", phase, label, e)
                    verarbeiten(block[:mitte], label + "a", tiefe + 1)
                    verarbeiten(block[mitte:], label + "b", tiefe + 1)
                    return
                log.warning("Ledger-%s Abschnitt %s übersprungen: %s", phase, label, e)
                return
            except SprachmodellFehler as e:
                log.warning("Ledger-%s Abschnitt %s übersprungen: %s", phase, label, e)
                return
            for roh in d.get("events") or []:
                event = self._ledger_event_normalisieren(roh, im_teil, quelle)
                if event is not None:
                    event["extractionPass"] = phase
                    aus.append(event)

        for nr, teil in enumerate(teile, 1):
            verarbeiten(teil, str(nr))
        return aus

    @staticmethod
    def _ledger_fingerprint(data) -> str:
        """Stabiler Diagnose-Fingerprint; niemals als Wahrheits- oder Merge-Entscheidung benutzen."""
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _ledger_semantik(e: dict) -> dict:
        def normliste(name):
            return sorted(_notizkern(klartext(x)) for x in e.get(name) or [] if klartext(x))
        assertions = sorted(
            (_notizkern(klartext(a.get("subject"))), str(a.get("property") or ""),
             _notizkern(klartext(a.get("value"))), str(a.get("epistemic") or ""),
             str(a.get("certainty") or ""))
            for a in e.get("assertions") or [] if isinstance(a, dict)
        )
        return {
            "kinds": sorted(str(x) for x in e.get("kinds") or []),
            "actors": normliste("actors"), "targets": normliste("targets"), "objects": normliste("objects"),
            "locations": normliste("locations"), "factions": normliste("factions"),
            "assertions": assertions, "epistemic": str(e.get("epistemic") or ""),
            "modality": str(e.get("modality") or ""), "relevance": e.get("relevance") or {},
        }

    @staticmethod
    def _ledger_candidate_text(e: dict) -> str:
        """Kompakte Kandidatendarstellung für den Reviewer; Evidence-Text steht separat als Originalquelle."""
        d = {
            "sourceIds": e.get("sourceIds") or [], "summary": e.get("summary") or "",
            "kinds": e.get("kinds") or [], "actors": e.get("actors") or [], "targets": e.get("targets") or [],
            "objects": e.get("objects") or [], "locations": e.get("locations") or [], "factions": e.get("factions") or [],
            "assertions": e.get("assertions") or [], "epistemic": e.get("epistemic"),
            "modality": e.get("modality"), "importance": e.get("importance"),
            "relevance": e.get("relevance") or {},
            "pass": e.get("extractionPass"),
        }
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _ledger_meta_entitaet(wert: str) -> bool:
        """Tischrollen sind niemals Entitäten der Spielwelt; Zusätze wie „Spielleitung (NPC)“ zählen ebenfalls."""
        k = klartext(wert)
        return bool(re.search(r"\b(?:Spielleitung|Game\s*Master|Gamemaster|Dungeon\s*Master)\b", k, re.IGNORECASE))

    def _ledger_review(self, ein: dict, kandidaten: list[dict], zeilen: list[tuple[str, str]]) -> tuple[
            list[dict], dict, list[dict], list[dict]]:
        """0.4.55: Ein source-grounded Review je Quellblock. Zusätzlich zu Reparatur/Coverage liefert derselbe Call
        kanonische Zustands-/Relationsanker und systemagnostische Encounter-Fragmente; kein weiterer Vollpass."""
        quelle = {lid: z for lid, z in zeilen}
        teile = self._ledger_quellteile(zeilen)
        chunk_von = {}
        for nr, block in enumerate(teile):
            for z in block:
                m = re.match(r"^(L\d{4,6}) \|", z)
                if m:
                    chunk_von[m.group(1)] = nr
        gruppen: dict[int, list[dict]] = {}
        for e in kandidaten:
            ids = e.get("sourceIds") or []
            if ids and ids[0] in chunk_von:
                gruppen.setdefault(chunk_von[ids[0]], []).append(e)

        ersetzt: dict[str, dict | None] = {}
        coverage_neu: list[dict] = []
        anchors_neu: list[dict] = []
        encounter_fragmente: list[dict] = []
        coverage_seen: set[tuple] = set()
        anchor_seen: set[tuple] = set()
        encounter_seen: set[tuple] = set()
        vorhandene_keys = {
            (tuple(e.get("sourceIds") or []), _notizkern(e.get("summary") or "")) for e in kandidaten
        }
        diag = {"state": "ok", "candidates": len(kandidaten), "accepted": 0, "repaired": 0,
                "merged": 0, "rejected": 0, "reviewErrors": 0, "coverageErrors": 0,
                "coverageAdded": 0, "reviewCalls": 0, "sourceChunks": len(teile), "actions": []}
        system = SYSTEM_LEDGER_REVIEW.replace("{sprache}", _sprache(ein))
        anchor_props = {"life_status", "identity", "possession", "relationship", "obligation", "physical_condition"}

        def pruefen(batch: list[dict], chunk_nr: int, suffix: str = "") -> None:
            block = teile[chunk_nr]
            erlaubte_ids = {m.group(1) for z in block if (m := re.match(r"^(L\d{4,6}) \|", z))}
            kandidaten_text = "\n".join(
                f"{e['candidateId']} | {self._ledger_candidate_text(e)}" for e in batch) or "(keine)"
            if len(batch) > LEDGER_REVIEW_BATCH and tokens(kandidaten_text) > 3500:
                mitte = len(batch) // 2
                pruefen(batch[:mitte], chunk_nr, suffix + "a")
                pruefen(batch[mitte:], chunk_nr, suffix + "b")
                return
            self._schritt(f"ledger.review {chunk_nr + 1}{suffix}/{len(teile)}")
            diag["reviewCalls"] += 1
            try:
                d = self.zaehler.aufruf(
                    self.klient, system,
                    f"{_kopf(ein)}\n\nORIGINALTRANSKRIPT:\n" + "\n".join(block)
                    + f"\n\nKANDIDATEN:\n{kandidaten_text}")
            except AntwortFehler as e:
                if len(batch) > 1:
                    mitte = len(batch) // 2
                    pruefen(batch[:mitte], chunk_nr, suffix + "a")
                    pruefen(batch[mitte:], chunk_nr, suffix + "b")
                    return
                if batch:
                    diag["reviewErrors"] += 1
                    log.warning("Ledger-Review Kandidat %s übersprungen: %s", batch[0].get("candidateId"), e)
                else:
                    diag["coverageErrors"] += 1
                    log.warning("Ledger-Review/Coverage Abschnitt %d übersprungen: %s", chunk_nr + 1, e)
                return
            except SprachmodellFehler as e:
                diag["reviewErrors"] += len(batch)
                diag["coverageErrors"] += 1
                log.warning("Ledger-Review Abschnitt %d übersprungen: %s", chunk_nr + 1, e)
                return

            erlaubt_c = {e["candidateId"] for e in batch}
            benutzt: set[str] = set()
            for r in d.get("reviews") or []:
                if not isinstance(r, dict):
                    continue
                origin = [str(x) for x in r.get("originIds") or []
                          if str(x) in erlaubt_c and str(x) not in benutzt]
                verdict = str(r.get("verdict") or "")
                if not origin or verdict not in ("repair", "merge", "reject"):
                    continue
                if verdict == "merge" and len(origin) < 2:
                    continue
                replacement = None
                if verdict in ("repair", "merge"):
                    replacement = self._ledger_event_normalisieren(
                        r.get("replacement") or {}, erlaubte_ids, quelle)
                    if replacement is None:
                        continue
                    replacement["_review"] = {"verdict": verdict, "originIds": origin,
                                              "reason": klartext(r.get("reason") or "")[:500]}
                    vorhandene_keys.add((tuple(replacement.get("sourceIds") or []),
                                         _notizkern(replacement.get("summary") or "")))
                for cid in origin:
                    ersetzt[cid] = None
                    benutzt.add(cid)
                if replacement is not None:
                    ersetzt[origin[0]] = replacement
                diag[{"repair": "repaired", "merge": "merged", "reject": "rejected"}[verdict]] += 1
                diag["actions"].append({
                    "verdict": verdict, "originIds": origin,
                    "reason": klartext(r.get("reason") or "")[:500],
                    "replacement": ({"sourceIds": replacement.get("sourceIds") or [],
                                     "summary": replacement.get("summary") or ""}
                                    if replacement is not None else None),
                })

            for roh in d.get("coverage") or []:
                event = self._ledger_event_normalisieren(roh, erlaubte_ids, quelle)
                if event is None or event.get("importance") not in ("critical", "important"):
                    continue
                key = (tuple(event.get("sourceIds") or []), _notizkern(event.get("summary") or ""))
                if key in vorhandene_keys or key in coverage_seen:
                    continue
                coverage_seen.add(key)
                event["extractionPass"] = "review_coverage"
                event["_review"] = {"verdict": "coverage_added", "originIds": [],
                                    "reason": "fehlte nach Primärpass"}
                coverage_neu.append(event)
                diag["coverageAdded"] += 1
                diag["actions"].append({
                    "verdict": "coverage_added", "originIds": [], "reason": "fehlte nach Primärpass",
                    "replacement": {"sourceIds": event.get("sourceIds") or [], "summary": event.get("summary") or ""},
                })

            for roh in d.get("anchors") or []:
                if not isinstance(roh, dict):
                    continue
                ids = [str(x) for x in roh.get("sourceIds") or [] if str(x) in erlaubte_ids][:6]
                subject, prop, value = klartext(roh.get("subject")), str(roh.get("property") or ""), klartext(roh.get("value"))
                importance = str(roh.get("importance") or "")
                if not ids or not subject or not value or prop not in anchor_props or importance not in ("critical", "important"):
                    continue
                if prop == "physical_condition" and importance != "critical":
                    continue
                epistemic = str(roh.get("epistemic") or "unknown")
                certainty = str(roh.get("certainty") or "medium")
                origin = [str(x) for x in roh.get("originIds") or [] if str(x) in erlaubt_c]
                key = (tuple(ids), _notizkern(subject), prop, _notizkern(value), epistemic)
                if key in anchor_seen:
                    continue
                anchor_seen.add(key)
                anchors_neu.append({"originIds": origin, "sourceIds": ids, "subject": subject, "property": prop,
                                    "value": value, "epistemic": epistemic, "certainty": certainty,
                                    "importance": importance, "chunk": chunk_nr})

            for roh in d.get("encounters") or []:
                if not isinstance(roh, dict):
                    continue
                ids = [str(x) for x in roh.get("sourceIds") or [] if str(x) in erlaubte_ids][:12]
                summary = klartext(roh.get("summary") or "")[:500]
                kind, boundary = str(roh.get("kind") or ""), str(roh.get("boundary") or "")
                if not ids or not summary or kind not in ("combat", "chase", "conflict", "social_conflict", "other"):
                    continue
                if boundary not in ("start", "middle", "end", "complete", "unknown"):
                    boundary = "unknown"
                def liste(name: str, max_n: int = 12) -> list[str]:
                    return [klartext(x)[:240] for x in roh.get(name) or [] if klartext(x)][:max_n]
                key = (tuple(ids), kind, _notizkern(summary))
                if key in encounter_seen:
                    continue
                encounter_seen.add(key)
                encounter_fragmente.append({
                    "sourceIds": ids, "kind": kind, "boundary": boundary, "participants": liste("participants"),
                    "locations": liste("locations"), "objectives": liste("objectives"), "domains": liste("domains"),
                    "summary": summary, "turningPoints": liste("turningPoints"), "outcomes": liste("outcomes"),
                    "consequences": liste("consequences"), "unresolved": liste("unresolved"), "chunk": chunk_nr,
                })

        for chunk_nr in range(len(teile)):
            pruefen(gruppen.get(chunk_nr, []), chunk_nr)

        aus = []
        for e in kandidaten:
            cid = e["candidateId"]
            if cid in ersetzt:
                if ersetzt[cid] is not None:
                    aus.append(ersetzt[cid])
                continue
            x = {k: v for k, v in e.items() if k != "candidateId"}
            x["_review"] = {"verdict": "accepted", "originIds": [cid], "reason": ""}
            aus.append(x)
            diag["accepted"] += 1
        return aus + coverage_neu, diag, anchors_neu, encounter_fragmente

    def _ledger_anchor_resolve(self, ein: dict, events: list[dict], anchors: list[dict],
                               zeilen: list[tuple[str, str]]) -> tuple[list[dict], dict]:
        """0.4.55: Anchors sind nur Prüfhinweise. Nur fehlende/widersprüchliche Hochrisiko-Fakten bekommen einen
        kleinen source-grounded Micro-Review; kein weiterer Volltranskript-Pass."""
        erlaubt_props = {"life_status", "identity", "possession", "relationship", "obligation", "physical_condition"}
        anchors = [h for h in anchors if h.get("property") in erlaubt_props
                   and not self._ledger_meta_entitaet(h.get("subject") or "")]
        diag = {"hints": len(anchors), "flagged": 0, "calls": 0, "confirmed": 0, "rejected": 0,
                "unclear": 0, "added": 0, "replaced": 0, "errors": 0}
        if not anchors:
            return events, diag

        def norm(x) -> str:
            return _notizkern(klartext(x))

        def exact(a: dict, h: dict) -> bool:
            return (norm(a.get("subject")) == norm(h.get("subject"))
                    and str(a.get("property") or "") == str(h.get("property") or "")
                    and norm(a.get("value")) == norm(h.get("value")))

        flagged = []
        for h in anchors:
            origins = set(h.get("originIds") or [])
            sources = set(h.get("sourceIds") or [])
            relevant = []
            for e in events:
                e_origins = set((e.get("_review") or {}).get("originIds") or [])
                if sources & set(e.get("sourceIds") or []) or origins & e_origins:
                    relevant.append(e)
            if any(exact(a, h) for e in relevant for a in e.get("assertions") or [] if isinstance(a, dict)):
                continue
            aid = f"A{len(flagged) + 1:04d}"
            flagged.append({**h, "anchorId": aid})
        diag["flagged"] = len(flagged)
        if not flagged:
            return events, diag

        pos = {lid: i for i, (lid, _z) in enumerate(zeilen)}
        quelle = {lid: z for lid, z in zeilen}
        system = SYSTEM_LEDGER_ANCHOR_RESOLVE.replace("{sprache}", _sprache(ein))
        aus = list(events)

        for ab in range(0, len(flagged), 8):
            paket = flagged[ab:ab + 8]
            ids = set()
            for h in paket:
                for lid in h.get("sourceIds") or []:
                    if lid not in pos:
                        continue
                    i = pos[lid]
                    for j in range(max(0, i - 2), min(len(zeilen), i + 3)):
                        ids.add(zeilen[j][0])
            ordered_ids = [lid for lid, _z in zeilen if lid in ids]
            original = "\n".join(f"{lid} | {quelle[lid]}" for lid in ordered_ids)
            vorhandene = [e for e in aus if ids & set(e.get("sourceIds") or [])]
            hints = [{k: v for k, v in h.items() if k != "chunk"} for h in paket]
            nutzer = (f"{_kopf(ein)}\n\nORIGINALTRANSKRIPT:\n{original}\n\nHINWEISE:\n"
                      + json.dumps(hints, ensure_ascii=False)
                      + "\n\nBEREITS VORHANDENE EVENTS:\n"
                      + ("\n".join(self._ledger_candidate_text(e) for e in vorhandene) or "(keine)"))
            self._schritt(f"ledger.resolve {ab // 8 + 1}/{(len(flagged) + 7) // 8}")
            diag["calls"] += 1
            try:
                d = self.zaehler.aufruf(self.klient, system, nutzer)
            except SprachmodellFehler as e:
                diag["errors"] += len(paket)
                log.warning("Ledger-Anchor-Resolver übersprungen: %s", e)
                continue

            by_id = {h["anchorId"]: h for h in paket}
            seen = set()
            for r in d.get("resolutions") or []:
                if not isinstance(r, dict) or r.get("anchorId") not in by_id or r["anchorId"] in seen:
                    continue
                seen.add(r["anchorId"])
                verdict = str(r.get("verdict") or "unclear")
                if verdict not in ("confirmed", "rejected", "unclear"):
                    verdict = "unclear"
                diag[verdict] += 1
                if verdict != "confirmed":
                    continue
                h = by_id[r["anchorId"]]
                event = self._ledger_event_normalisieren(r.get("event") or {}, set(ordered_ids), quelle)
                if event is None:
                    diag["unclear"] += 1
                    diag["confirmed"] -= 1
                    continue
                event["extractionPass"] = "anchor_resolution"
                event["_review"] = {"verdict": "anchor_confirmed", "originIds": h.get("originIds") or [],
                                    "reason": klartext(r.get("reason") or "")[:500]}
                h_subject, h_prop = norm(h.get("subject")), str(h.get("property") or "")
                h_sources, h_origins = set(h.get("sourceIds") or []), set(h.get("originIds") or [])
                behalten = []
                ersetzt = 0
                for alt in aus:
                    alt_origins = set((alt.get("_review") or {}).get("originIds") or [])
                    related = bool(h_sources & set(alt.get("sourceIds") or []) or h_origins & alt_origins)
                    conflict = any(
                        isinstance(a, dict)
                        and norm(a.get("subject")) == h_subject
                        and str(a.get("property") or "") == h_prop
                        and norm(a.get("value")) != norm(h.get("value"))
                        for a in alt.get("assertions") or []
                    )
                    if related and conflict:
                        ersetzt += 1
                        continue
                    behalten.append(alt)
                aus = behalten
                if not any(
                    any(isinstance(a, dict) and exact(a, h) for a in e.get("assertions") or [])
                    for e in aus
                ):
                    aus.append(event)
                    diag["added"] += 1
                diag["replaced"] += ersetzt

        return aus, diag

    def _ledger_integrity(self, events: list[dict], anchors: list[dict],
                          zeilen: list[tuple[str, str]]) -> tuple[list[dict], dict]:
        """0.4.55: Tischrollen aus Weltfakten sanitizen. Anchors werden nur diagnostisch geprüft und niemals selbst
        als Wahrheit eingesetzt."""
        diag = {"anchors": len(anchors), "anchorAdded": 0, "anchorConflicts": 0, "anchorMissing": 0,
                "metaSanitized": 0, "metaRejected": 0, "quarantined": []}
        behalten = []

        def meta_text(x) -> bool:
            return bool(re.search(r"\b(?:Spielleitung|Game\s*Master|Gamemaster|Dungeon\s*Master)\b",
                                  klartext(x), re.IGNORECASE))

        for original in events:
            e = {**original}
            geaendert = False
            for feld in ("actors", "targets", "objects", "locations", "factions"):
                alt = list(e.get(feld) or [])
                neu = [x for x in alt if not self._ledger_meta_entitaet(x)]
                if neu != alt:
                    geaendert = True
                e[feld] = neu
            alt_assertions = list(e.get("assertions") or [])
            e["assertions"] = [
                a for a in alt_assertions if isinstance(a, dict)
                and not self._ledger_meta_entitaet(a.get("subject") or "")
                and not self._ledger_meta_entitaet(a.get("value") or "")
            ]
            if len(e["assertions"]) != len(alt_assertions):
                geaendert = True
            if meta_text(e.get("summary") or ""):
                geaendert = True
                if e["assertions"]:
                    e["summary"] = "; ".join(
                        f"{klartext(a.get('subject'))}: {a.get('property')} = {klartext(a.get('value'))}"
                        for a in e["assertions"][:3]
                    ) + "."
                else:
                    welt = [klartext(x) for feld in ("actors", "targets", "objects", "locations", "factions")
                            for x in e.get(feld) or [] if klartext(x)]
                    if welt:
                        e["summary"] = "Weltfakt: " + ", ".join(dict.fromkeys(welt)) + "."
                    else:
                        diag["metaRejected"] += 1
                        diag["quarantined"].append({"reason": "table_role_only",
                                                    "sourceIds": e.get("sourceIds") or [],
                                                    "summary": original.get("summary") or ""})
                        continue
            if geaendert:
                tags = list(e.get("tags") or [])
                if "table_role_sanitized" not in tags:
                    tags.append("table_role_sanitized")
                e["tags"] = tags
                diag["metaSanitized"] += 1
            behalten.append(e)

        def norm(x) -> str:
            return _notizkern(klartext(x))

        for h in anchors:
            if self._ledger_meta_entitaet(h.get("subject") or ""):
                continue
            exact = False
            conflict = False
            hs = set(h.get("sourceIds") or [])
            ho = set(h.get("originIds") or [])
            for e in behalten:
                related = bool(hs & set(e.get("sourceIds") or [])
                               or ho & set((e.get("_review") or {}).get("originIds") or []))
                for a in e.get("assertions") or []:
                    if not isinstance(a, dict):
                        continue
                    if norm(a.get("subject")) != norm(h.get("subject")) or str(a.get("property") or "") != h.get("property"):
                        continue
                    if norm(a.get("value")) == norm(h.get("value")):
                        exact = True
                    elif related:
                        conflict = True
            if not exact:
                if conflict:
                    diag["anchorConflicts"] += 1
                else:
                    diag["anchorMissing"] += 1
        return behalten, diag

    @staticmethod
    def _ledger_encounters(fragmente: list[dict]) -> list[dict]:
        """0.4.55: Nur klar zusammenhängende, recap-relevante Konfliktphasen verbinden. Gemeinsame Spieler allein
        reichen ausdrücklich nicht mehr als Stitching-Kriterium."""
        if not fragmente:
            return []

        def normset(f: dict, feld: str) -> set[str]:
            return {_notizkern(str(x)) for x in f.get(feld) or [] if _notizkern(str(x))}

        fragmente = [
            f for f in fragmente
            if f.get("objectives") or f.get("turningPoints") or f.get("outcomes") or f.get("consequences")
        ]
        fragmente.sort(key=lambda f: (int(f.get("chunk") or 0), (f.get("sourceIds") or [""])[0]))
        gruppen: list[list[dict]] = []
        for f in fragmente:
            if not gruppen:
                gruppen.append([f])
                continue
            g = gruppen[-1]
            prev = g[-1]
            adjacent = int(f.get("chunk") or 0) <= int(prev.get("chunk") or 0) + 1
            abgeschlossen = prev.get("boundary") in ("end", "complete")
            neuer_start = f.get("boundary") in ("start", "complete")
            same_kind = f.get("kind") == prev.get("kind")
            ziel_overlap = bool(normset(prev, "objectives") & normset(f, "objectives"))
            ort_domain_overlap = bool((normset(prev, "locations") & normset(f, "locations"))
                                      or (normset(prev, "domains") & normset(f, "domains")))
            teilnehmer_overlap = bool(normset(prev, "participants") & normset(f, "participants"))
            if (adjacent and same_kind and not abgeschlossen and not neuer_start
                    and ziel_overlap and (ort_domain_overlap or teilnehmer_overlap)):
                g.append(f)
            else:
                gruppen.append([f])

        def uniq(xs):
            return list(dict.fromkeys(x for x in xs if x))

        aus = []
        for nr, g in enumerate(gruppen, 1):
            def sammeln(feld):
                return uniq([x for f in g for x in f.get(feld) or []])
            def belegt(feld):
                return [{"sourceIds": f.get("sourceIds") or [], "text": x}
                        for f in g for x in f.get(feld) or [] if x]
            aus.append({
                "encounterId": f"EN{nr:04d}",
                "kind": g[0].get("kind") or "other",
                "sourceIds": uniq([x for f in g for x in f.get("sourceIds") or []]),
                "participants": sammeln("participants"), "locations": sammeln("locations"),
                "objectives": sammeln("objectives"), "domains": sammeln("domains"),
                "phases": [{"sourceIds": f.get("sourceIds") or [], "summary": f.get("summary") or "",
                            "boundary": f.get("boundary") or "unknown"} for f in g],
                "turningPoints": belegt("turningPoints"), "outcomes": belegt("outcomes"),
                "consequences": belegt("consequences"), "unresolved": belegt("unresolved"),
            })
        return aus

    def _ledger_coverage(self, ein: dict, events: list[dict], zeilen: list[tuple[str, str]], diag: dict) -> list[dict]:
        """Dritter, enger Pass: nur fehlende critical/important Fakten. So kann Review auch reine Auslassungen finden."""
        quelle = {lid: z for lid, z in zeilen}
        teile = self._ledger_quellteile(zeilen)
        chunk_von = {}
        for nr, block in enumerate(teile):
            for z in block:
                m = re.match(r"^(L\d{4,6}) \|", z)
                if m:
                    chunk_von[m.group(1)] = nr
        system = SYSTEM_LEDGER_COVERAGE.replace("{sprache}", _sprache(ein))
        neu: list[dict] = []

        for nr, block in enumerate(teile):
            im_teil = {m.group(1) for z in block if (m := re.match(r"^(L\d{4,6}) \|", z))}
            vorhanden = [e for e in events if any(lid in im_teil for lid in e.get("sourceIds") or [])]
            kompakt = "\n".join(f"- {','.join(e.get('sourceIds') or [])}: {e.get('summary', '')}" for e in vorhanden)
            self._schritt(f"ledger.coverage {nr + 1}/{len(teile)}")
            try:
                d = self.zaehler.aufruf(
                    self.klient, system,
                    f"{_kopf(ein)}\n\nORIGINALTRANSKRIPT:\n" + "\n".join(block)
                    + f"\n\nBEREITS ERFASST:\n{kompakt or '(nichts)'}")
            except SprachmodellFehler as e:
                log.warning("Ledger-Coverage Abschnitt %d übersprungen: %s", nr + 1, e)
                continue
            for roh in d.get("events") or []:
                event = self._ledger_event_normalisieren(roh, im_teil, quelle)
                if event is None or event.get("importance") not in ("critical", "important"):
                    continue
                event["extractionPass"] = "coverage"
                event["_review"] = {"verdict": "coverage_added", "originIds": [], "reason": "fehlte nach Review"}
                key = (tuple(event.get("sourceIds") or []), _notizkern(event.get("summary") or ""))
                if any((tuple(x.get("sourceIds") or []), _notizkern(x.get("summary") or "")) == key
                       for x in events + neu):
                    continue
                neu.append(event)
                diag["coverageAdded"] += 1
                diag.setdefault("actions", []).append({
                    "verdict": "coverage_added", "originIds": [], "reason": "fehlte nach Review",
                    "replacement": {"sourceIds": event.get("sourceIds") or [], "summary": event.get("summary") or ""},
                })
        return events + neu

    @staticmethod
    def _ledger_states(events: list[dict]) -> list[dict]:
        """Zeitabhängige Assertions reduzieren, ohne alte Aussagen zu löschen. 'believed/reported' bleibt Historie;
        eine spätere beobachtete Gegenlage markiert sie als Revision statt aus ihr eine Weltwahrheit zu machen."""
        gruppen: dict[tuple[str, str], list[dict]] = {}
        nicht_kanonisch = {"stated", "reported", "believed", "suspected", "remembered", "vision", "dream",
                           "inferred", "unknown"}
        for e in events:
            for a in e.get("assertions") or []:
                if not isinstance(a, dict):
                    continue
                subject, prop, value = (klartext(a.get("subject")), str(a.get("property") or ""),
                                        klartext(a.get("value")))
                if not subject or not prop or not value:
                    continue
                x = {"eventId": e["eventId"], "time": e.get("time"), "sourceIds": list(e.get("sourceIds") or []),
                     "subject": subject, "property": prop, "value": value,
                     "epistemic": str(a.get("epistemic") or "unknown"),
                     "certainty": str(a.get("certainty") or "medium")}
                gruppen.setdefault((subject.casefold(), prop), []).append(x)
        aus = []
        for _, hist in sorted(gruppen.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            hist.sort(key=lambda x: (x["time"] is None, x["time"] or 0, x["eventId"]))
            vorher = None
            for x in hist:
                if vorher is None:
                    x["resolution"] = "initial"
                elif x["value"].casefold() == vorher["value"].casefold():
                    x["resolution"] = "confirmation"
                elif vorher["epistemic"] in nicht_kanonisch and x["epistemic"] not in nicht_kanonisch:
                    x["resolution"] = "revision"
                    vorher["resolvedBy"] = x["eventId"]
                else:
                    x["resolution"] = "state_change"
                vorher = x
            beobachtet = [x for x in hist if x["epistemic"] not in nicht_kanonisch]
            aus.append({"subject": hist[0]["subject"], "property": hist[0]["property"], "history": hist,
                        "current": (beobachtet[-1] if beobachtet else hist[-1])})
        return aus

    def _ledger_history_sources(self, ein: dict, events: list[dict]) -> tuple[list[dict], dict[str, list[str]]]:
        """Kleines Retrieval statt komplette Kampagnenhistorie im Prompt. Öffentliche Bibel + frühere Recaps werden
        nur dann Quelle, wenn eine aktuell extrahierte Entität darin tatsächlich vorkommt."""
        namen = []
        for e in events:
            for feld in ("actors", "targets", "objects", "locations", "factions"):
                namen += [klartext(x) for x in e.get(feld) or [] if klartext(x)]
            for a in e.get("assertions") or []:
                if isinstance(a, dict) and klartext(a.get("subject")):
                    namen.append(klartext(a["subject"]))
        namen = list(dict.fromkeys(n for n in namen if len(n) >= 2))
        sources, event_map = [], {}
        hid = 1

        def add(typ: str, text: str, meta: dict) -> str:
            nonlocal hid
            sid = f"H{hid:04d}"
            hid += 1
            sources.append({"historyId": sid, "type": typ, "text": text[:1200], **meta})
            return sid

        # Bibel ist Referenz für stabile Identität/Beziehung/Rolle usw., nicht für jede beliebige Handlung einer Figur.
        # Frühere Recaps bleiben breiter, damit echte Zustandswechsel (tot → lebendig, Ort A → B) auffindbar bleiben.
        bibel_props = {"identity", "relationship", "role_status", "allegiance", "reputation", "possession", "life_status"}
        bibel_nach_name = {}
        for b in ein.get("bibel") or []:
            name = klartext(b.get("name"))
            if name:
                bibel_nach_name[name.casefold()] = (name, b)
        bibel_ids = {}
        recap_sources = []
        for h in ein.get("_historie") or []:
            text = "\n".join([str(h.get("title") or ""), str(h.get("text") or ""),
                              " ".join(h.get("openThreads") or [])])
            if text.strip():
                recap_sources.append((h, text))

        history_props = {"life_status", "physical_condition", "location", "possession", "relationship", "identity",
                         "knowledge", "allegiance", "goal", "obligation", "reputation", "control", "role_status"}
        minor_trotzdem = {"life_status", "possession", "relationship", "identity", "allegiance", "obligation",
                          "role_status"}
        for e in events:
            rel = e.get("relevance")
            if isinstance(rel, dict) and not (rel.get("bible") or rel.get("openThread")
                                              or e.get("importance") == "critical"):
                continue
            props = {str(a.get("property") or "") for a in e.get("assertions") or [] if isinstance(a, dict)}
            # Frühere Recaps sind kein Suchindex für jede Handlung derselben Figur. Nur echte Zustands-/Kontinuitäts-
            # Eigenschaften dürfen einen History-Modellcall auslösen; bei minor nur die dauerhaft wichtigen Typen.
            if not (props & history_props):
                continue
            if e.get("importance") == "minor" and not (props & minor_trotzdem):
                continue
            et = self._ledger_event_text(e)
            passende_namen = [n for n in namen if _erwaehnt(n, et)]
            ids = []
            for n in passende_namen:
                key = n.casefold()
                if props & bibel_props and key in bibel_nach_name and key not in bibel_ids:
                    name, b = bibel_nach_name[key]
                    bibel_ids[key] = add("bible", f"{name}: {b.get('zusammenfassung') or ''}",
                                         {"entryId": b.get("id"), "name": name})
                if key in bibel_ids and props & bibel_props:
                    ids.append(bibel_ids[key])
            for h, ht in recap_sources:
                treffer = [n for n in passende_namen if _erwaehnt(n, ht)]
                if not treffer:
                    continue
                klein = ht.casefold()
                pos = min((klein.find(n.casefold()) for n in treffer if klein.find(n.casefold()) >= 0), default=-1)
                snippet = ht[max(0, pos - 350):pos + 850] if pos >= 0 else ht[:1200]
                ids.append(add("recap", snippet, {"session": h.get("session"), "title": h.get("title")}))
            if ids:
                event_map[e["eventId"]] = list(dict.fromkeys(ids))
        return sources, event_map

    def _ledger_history_links(self, ein: dict, events: list[dict], sources: list[dict],
                              event_map: dict[str, list[str]]) -> list[dict]:
        by_id = {x["historyId"]: x for x in sources}
        relevant = [e for e in events if e["eventId"] in event_map]
        aus = []
        system = SYSTEM_LEDGER_HISTORY.replace("{sprache}", _sprache(ein))
        for ab in range(0, len(relevant), LEDGER_RECONCILE_BATCH):
            paket = relevant[ab:ab + LEDGER_RECONCILE_BATCH]
            erlaubte_h = {hid for e in paket for hid in event_map.get(e["eventId"], [])}
            hist = "\n".join(f"{hid} | {by_id[hid]['type']} | {by_id[hid]['text']}" for hid in sorted(erlaubte_h))
            aktuell = "\n".join(
                f"{e['eventId']} | {e['summary']} | epistemic={e.get('epistemic')} | modality={e.get('modality')}"
                for e in paket)
            try:
                d = self.zaehler.aufruf(self.klient, system,
                                         f"{_kopf(ein)}\n\nAKTUELLE EVENTS:\n{aktuell}\n\nHISTORISCHE QUELLEN:\n{hist}")
            except SprachmodellFehler as e:
                log.warning("Ledger-History-Abgleich übersprungen: %s", e)
                continue
            erlaubte_e = {e["eventId"] for e in paket}
            for x in d.get("links") or []:
                if not isinstance(x, dict) or x.get("eventId") not in erlaubte_e:
                    continue
                ids = [str(i) for i in x.get("historyIds") or [] if str(i) in erlaubte_h][:5]
                relation = str(x.get("relation") or "none")
                if relation == "none" or not ids:
                    continue
                aus.append({"eventId": x["eventId"], "relation": relation, "historyIds": ids,
                            "reason": klartext(x.get("reason") or "")[:500]})
        return aus

    def ledger(self, ein: dict) -> dict:
        """0.4.56 Schatten-Ledger v4: 0.4.55-Core unverändert in der Semantik; zusätzlich deterministische
        Provenance-/Fingerprint-Instrumentierung für Harness und Modellvergleich. Noch ohne Einfluss auf Recap/Bibel."""
        zeilen = transkript_zeilen_mit_ids(ein.get("transkript") or [])
        source_fingerprint = self._ledger_fingerprint({"sourceType": "TRANSCRIPT", "lines": zeilen})
        if not zeilen:
            return {"version": 4, "state": "empty", "parserVersion": LEDGER_PARSER_VERSION,
                    "sourceFingerprint": source_fingerprint, "events": [], "states": [], "encounters": [],
                    "historySources": [], "historyLinks": [], "review": {"state": "empty"},
                    "integrity": {"anchors": 0, "anchorAdded": 0, "anchorConflicts": 0, "anchorMissing": 0,
                                  "metaSanitized": 0, "metaRejected": 0},
                    "relevance": {"recap": 0, "openThread": 0, "bible": 0}}
        self._schritt("ledger")
        # Kein zweiter Volltranskript-Pass: Primärpass + source-grounded Review. Nur strittige Hochrisiko-Anchors
        # bekommen danach einen kleinen Micro-Review mit wenigen Nachbarzeilen.
        roh = self._ledger_pass(ein, SYSTEM_LEDGER_EVENTS, zeilen)

        # Nur wirklich identische Kandidaten vor dem Review entfernen. Semantisch ähnliche/konfligierende Kandidaten
        # bleiben bewusst erhalten, damit der Quellen-Review sie zusammenführen oder korrigieren kann.
        gesehen, kandidaten = set(), []
        for e in sorted(roh, key=lambda x: (x.get("time") is None, x.get("time") or 0, x.get("summary", ""))):
            key = (tuple(e.get("sourceIds") or []), _notizkern(e.get("summary") or ""))
            if key in gesehen:
                continue
            gesehen.add(key)
            e["candidateId"] = f"C{len(kandidaten) + 1:04d}"
            kandidaten.append(e)

        events, review, anchors, encounter_fragmente = self._ledger_review(ein, kandidaten, zeilen)
        events, anchor_resolution = self._ledger_anchor_resolve(ein, events, anchors, zeilen)
        events, integrity = self._ledger_integrity(events, anchors, zeilen)
        encounters = self._ledger_encounters(encounter_fragmente)
        events.sort(key=lambda x: (x.get("time") is None, x.get("time") or 0, x.get("summary", "")))

        for nr, e in enumerate(events, 1):
            e.pop("candidateId", None)
            e["eventId"] = f"E{nr:04d}"
            semantik = self._ledger_semantik(e)
            e["semanticFingerprint"] = self._ledger_fingerprint(semantik)
            e["eventFingerprint"] = self._ledger_fingerprint({
                "semantic": semantik, "sourceIds": sorted(e.get("sourceIds") or [])
            })
            e["parser"] = {"version": LEDGER_PARSER_VERSION, "model": getattr(self.klient, "modell", None)}
            e["sourceAuthority"] = None
            e["extractionConfidence"] = None
            for ev in e.get("evidence") or []:
                if isinstance(ev, dict):
                    ev["sourceRevision"] = source_fingerprint
            et = self._ledger_event_text(e)
            e["linkedEntries"] = [b.get("id") for b in ein.get("bibel") or []
                                  if b.get("id") and klartext(b.get("name"))
                                  and _erwaehnt(klartext(b.get("name")), et)]

        states = self._ledger_states(events)
        sources, event_map = self._ledger_history_sources(ein, events)
        links = self._ledger_history_links(ein, events, sources, event_map) if sources else []
        link_map: dict[str, list[dict]] = {}
        for x in links:
            link_map.setdefault(x["eventId"], []).append(x)
        for e in events:
            e["history"] = link_map.get(e["eventId"], [])
        review["anchorsFound"] = len(anchors)
        review["anchorResolution"] = anchor_resolution
        review["encounterFragments"] = len(encounter_fragmente)
        relevance = {k: sum(1 for e in events if (e.get("relevance") or {}).get(k) is True)
                     for k in ("recap", "openThread", "bible")}
        return {"version": 4, "state": "ok", "parserVersion": LEDGER_PARSER_VERSION,
                "sourceFingerprint": source_fingerprint,
                "ledgerFingerprint": self._ledger_fingerprint(sorted(e.get("eventFingerprint") or "" for e in events)),
                "rawCandidates": len(kandidaten), "rawEvents": kandidaten,
                "events": events, "states": states, "encounters": encounters, "relevance": relevance,
                "historySources": sources, "historyLinks": links, "review": review, "integrity": integrity}

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
        system = (SYSTEM_VORSCHLAEGE.replace("{sprache}", _sprache(ein)).replace("{max}", str(MAX_VORSCHLAEGE)))
        self._schritt("proposals")
        d = self.zaehler.aufruf(self.klient, system, nutzer)
        return pruefen(d.get("proposals") or [], {e["id"] for e in ein["bibel"]}, {e["id"] for e in ein["geheim"]},
                       charaktere=[p["charakter"] for p in ein["personen"] if p.get("charakter")],
                       namen={e["id"]: e["name"] for e in ein["bibel"] + ein["geheim"]}, grundlage=grundlage)

    def ausfuehren(self, recap_ein: dict, vorschlag_ein: dict,
                   fortschritt: Callable[[float], None] = lambda _p: None, gegenpruefen: bool = False) -> dict:
        """Das ganze Ergebnis. Die Grundlage (Transkript bzw. Notizen) ist für beide gleich; die Notizen entstehen
        aus der Recap-Eingabe, die nichts Geheimes enthält. Die Gegenprüfung sieht nur, was der Recap sah."""
        titel, grundlage = self.grundlage(recap_ein, fortschritt)
        verlauf = ""
        if titel.startswith("Szenennotizen"):
            # Die Notizen passen fast immer in den Kontext (grundlage() verdichtet so lange). Dann bekommt der Recap
            # sie direkt, nur in Zeitabschnitte gegliedert: Jede weitere Zusammenfassung kostet Fakten, die in den
            # Notizen schon richtig standen. Teil-Zusammenfassungen nur, wenn die Notizen doch zu groß sind.
            zu_gross = tokens(grundlage) > self.max_transkript_tokens
            if self.gliederung == "teile" or (self.gliederung != "direkt" and zu_gross):
                verlauf = self.verlauf(recap_ein, grundlage)
        self.letzter_verlauf = verlauf
        plan = self.planen(recap_ein, grundlage) if titel.startswith("Szenennotizen") else []
        self.letzter_plan = plan
        if verlauf:
            r = self.recap(recap_ein, "Verlauf der Runde in Teilen (jeder Teil gehört in den Recap, in dieser "
                                      "Reihenfolge, jeder mit etwa gleich viel Raum)", verlauf, plan)
        elif titel.startswith("Szenennotizen"):
            r = self.recap(recap_ein, "Szenennotizen der Runde in Zeitabschnitten (jeder Abschnitt gehört in den "
                                      "Recap, in dieser Reihenfolge, mit etwa gleich viel Raum; lieber knapper erzählen "
                                      "als ein Ereignis weglassen)", self.gegliedert(grundlage), plan)
        else:
            r = self.recap(recap_ein, titel, grundlage)
        self.letztes_kapitel1 = r["text"]
        # Erst Fehlendes ergänzen, dann Falsches prüfen – sonst prüft man einen Text, der gleich wieder wächst.
        # Die Vollständigkeitsprüfung sieht dieselbe Grundlage wie der Recap; ihre Punkte zählen nur, wenn sie dort
        # wiederzufinden sind (fehlend_lesen), damit der Prüfer nichts erfindet.
        try:
            grund_titel, grund_text = (("Szenennotizen der Runde in Zeitabschnitten", self.gegliedert(grundlage))
                                       if titel.startswith("Szenennotizen") and not verlauf else (titel, grundlage))
            self.letzter_befund_fehlend = self.vollstaendigkeit(recap_ein, grund_titel, grund_text, r["text"])
            neu = self.ergaenzen(recap_ein, r["text"], self.letzter_befund_fehlend)
            if neu:
                r["text"] = neu
        except SprachmodellFehler as e:
            log.warning("Vollständigkeitsprüfung übersprungen: %s", e)
        self.letztes_kapitel2 = r["text"]
        fortschritt(0.6 if gegenpruefen else 0.8)
        pruefung = self.gegenpruefen(recap_ein, titel, grundlage, r) if gegenpruefen else None
        fortschritt(0.8)
        v = self.vorschlaege(vorschlag_ein, titel, grundlage)
        if self.ledger_shadow:
            try:
                self.letztes_ledger = self.ledger(recap_ein)
            except SprachmodellFehler as e:
                log.warning("Schatten-Ledger übersprungen: %s", e)
                self.letztes_ledger = {"version": 4, "state": "failed", "error": str(e), "events": [], "states": [],
                                       "encounters": [], "relevance": {"recap": 0, "openThread": 0, "bible": 0},
                                       "historySources": [], "historyLinks": [],
                                       "review": {"state": "failed"}, "integrity": {"state": "failed"}}
        fortschritt(1.0)
        aus = {**r, "proposals": v, "model": self.klient.modell, "tokensIn": self.zaehler.tokens_in,
               "tokensOut": self.zaehler.tokens_out}
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
    out = []
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
        flags = [f for f in v.get("flags") or [] if f in FLAGS]
        if sicherheit < 0.4 and "low_confidence" not in flags:
            flags.append("low_confidence")
        out.append({
            "entryType": typ, "action": art, "targetEntryId": ziel, "title": titel[:300], "detail": detail[:4000],
            "gmNotes": (ohne_meta(klartext(v.get("gmNotes")))[:4000] or None) if art == "create" else None,
            "suggestedVisibility": sicht, "visibilityReason": klartext(v.get("visibilityReason"))[:500] or None,
            "confidence": sicherheit, "flags": flags, "evidence": belege[:3],
        })
        if len(out) >= MAX_VORSCHLAEGE:
            break
    return out


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
