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
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

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

SYSTEM_RECAP_PLAN = """Du planst den Recap einer Pen-&-Paper-Session. Du schreibst noch keine Geschichte und formulierst nichts um. Du siehst genau einen Zeitabschnitt mit Szenennotizen; jede Notiz hat eine feste ID N001, N002 usw.
Wähle 2 bis {max} Notizen, die in einem Recap dieses Zeitabschnitts zwingend vorkommen sollten. Maßstab ist ausschließlich, ob ein Spieler die Information vor der nächsten Runde braucht: Wendepunkt oder Orts-/Lagewechsel; bleibender Zustand (verletzt, gerettet, gefangen, befreit, tot); Beziehung oder Identität; Besitz oder Übergabe; Ziel/Auftrag; Versprechen, Schuld oder Abmachung; entscheidende Information oder offene Handlungsmöglichkeit.
Nicht auswählen, solange keine bleibende Folge entsteht: Regeln und Würfe, Smalltalk, Witze, Essen/Schlafen, einzelne gescheiterte Versuche, reine Atmosphäre und beiläufige Details.
Gib ausschließlich IDs zurück, die in diesem Abschnitt stehen. Keine neuen IDs, keine Texte, keine Umformulierungen. Reihenfolge wie in den Notizen. Wenn weniger als zwei wirklich relevante Notizen vorhanden sind, wähle entsprechend weniger.
Antworte nur mit JSON: {"required": ["N001", "N004"]}. Sprache: {sprache}."""

PLAN_PRO_TEIL = 4


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
ERGAENZEN_RAENGE = ("kritisch",)  # 0.4.48: post-hoc nur noch kritische Punkte; der Pflichtplan trägt Wichtiges
RELATION_FENSTER_S = 60.0  # Transkript ± so viele Sekunden um die belegten Stellen eines Absatzes
RELATION_ZEICHEN = 6000  # höchstens so viel Transkript je Absatz

SYSTEM_RELATIONEN = """Du prüfst genau EINEN Absatz des Recaps einer Pen-&-Paper-Session gegen kurze Ausschnitte des ORIGINALTRANSKRIPTS (automatisch erkannt, mit Fehlern; „Spielleitung“ spricht dort für Nichtspielercharaktere). Prüfe nur atomare Beziehungen und Zustände, nicht Stil oder Vollständigkeit.
Zerlege den Absatz in die kleinsten relevanten Behauptungen: Wer tut was wem? Wer gibt wem was und wer besitzt es danach? War jemand bereits verletzt oder wird er verletzt? Wer kennt wen und seit wann? Wer verspricht wem was gegen welche Gegenleistung? Sind zwei Figuren verwechselt oder verschmolzen? Ist eine Figur anwesend oder nur erwähnt? Ist etwas beobachtet, behauptet, vermutet, geplant, erinnert oder eine Vision?
Für jede solche Behauptung:
- claim: kopiere die kürzeste passende Textstelle aus dem Recap WÖRTLICH. Keine Paraphrase.
- urteil: "stimmt", "widerspricht" oder "unklar". "widerspricht" NUR, wenn das Transkript ausdrücklich eine unvereinbare Beziehung oder einen anderen Zustand zeigt. Fehlt die Information im Ausschnitt, ist das "unklar", niemals ein Widerspruch.
- korrektur: nur bei "widerspricht" eine minimale Ersatzformulierung für genau claim, die das Transkript belegt. Keine zusätzlichen Tatsachen. Wenn keine sichere minimale Korrektur möglich ist, leer lassen.
- begruendung: ein kurzer Satz.
- zitat: kurzes wörtliches Zitat aus dem Transkript, das den Widerspruch belegt; bei "stimmt"/"unklar" optional.
Antworte nur mit JSON: {"claims": [{"claim": "…", "urteil": "…", "korrektur": "…", "begruendung": "…", "zitat": "…"}]}. Sprache: {sprache}."""

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
    letzter_plan: list = field(default_factory=list)  # Pflichtpunkte vor der Prosagenerierung (Modellvergleich)
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
        """1.5c: Vor der Prosa pro Zeitabschnitt nur vorhandene Szenennotizen als Pflichtpunkte auswählen.
        Das Modell darf keine Ereignisse formulieren, nur feste IDs wählen; ungültige IDs werden verworfen."""
        zeilen = [z.strip() for z in notizen.split("\n") if z.strip() and _zeit_vorn(z) is not None]
        if not zeilen:
            return []
        ids = {z: f"N{i + 1:03d}" for i, z in enumerate(zeilen)}
        teile = _teile_nach_zeit(zeilen)
        system = (SYSTEM_RECAP_PLAN.replace("{sprache}", _sprache(ein))
                  .replace("{max}", str(PLAN_PRO_TEIL)))
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
                    f"{_kopf(ein)}\n\nZeitabschnitt {i + 1} von {len(teile)}:\n{liste}")
            except SprachmodellFehler as e:
                log.warning("Recap-Plan: Abschnitt %d übersprungen: %s", i + 1, e)
                continue
            gewaehlt = d.get("required") or d.get("pflicht") or []
            if not isinstance(gewaehlt, list):
                continue
            wanted = {str(x).strip() for x in gewaehlt[:PLAN_PRO_TEIL * 2]}
            im_teil = 0
            for nid, z in erlaubt.items():
                if nid in wanted and im_teil < PLAN_PRO_TEIL:
                    aus.append({"id": nid, "zeit": _zeit_vorn(z), "notiz": z, "teil": i + 1})
                    im_teil += 1
        return aus

    def recap(self, ein: dict, titel: str, grundlage: str,
              pflichtplan: list[dict] | None = None) -> dict:
        bibel = "\n".join(f"- [{e['typ']}] {e['name']}" + (f": {e['zusammenfassung'][:500]}"
                                                           if _erwaehnt(e["name"], grundlage) else "")
                          for e in ein["bibel"])
        pflicht = ""
        if pflichtplan:
            pz = "\n".join(f"- Abschnitt {p['teil']}: {p['id']} {p['notiz']}" for p in pflichtplan)
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
        wird nur übernommen, wenn Claim UND Belegzitat im tatsächlichen Text wiedergefunden werden."""
        zeilen = [(_zeit_vorn(z), z) for z in transkript_zeilen(ein.get("transkript") or [])]
        zeilen = [(t, z) for t, z in zeilen if t is not None]
        teile = absaetze(text)
        aus = []
        for b in befund:
            i = b.get("index", -1)
            zeiten = sorted({e["start"] for e in b.get("evidence") or [] if e.get("start") is not None})[:3]
            if not (0 <= i < len(teile)) or not zeiten:
                continue
            spans = [z for t, z in zeilen if any(abs(t - zt) <= RELATION_FENSTER_S for zt in zeiten)]
            text_spans = "\n".join(spans)[:RELATION_ZEICHEN]
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

            span_kern = _notizkern(text_spans)
            span_woerter = set(span_kern.split())
            claims = []
            for c in d.get("claims") or d.get("behauptungen") or []:
                if not isinstance(c, dict):
                    continue
                claim = klartext(c.get("claim") or c.get("behauptung") or "")[:500]
                if not claim:
                    continue
                urteil = str(c.get("urteil") or c.get("verdict") or "").strip().lower()
                urteil = {"stimmt": "stimmt", "ok": "stimmt", "supported": "stimmt",
                           "widerspricht": "widerspricht", "contradicted": "widerspricht",
                           "contradiction": "widerspricht", "unklar": "unklar", "unclear": "unklar"}.get(urteil, "unklar")
                zitat = klartext(c.get("zitat") or c.get("quote") or "")[:300]
                zkern = _notizkern(zitat)
                zwoerter = set(zkern.split())
                zitat_belegt = bool(zkern and (zkern in span_kern or
                                    (zwoerter and len(zwoerter & span_woerter) / len(zwoerter) >= 0.6)))
                exakt = claim in teile[i]
                # Automatisch eingreifen nur mit zwei harten Ankern: exakter Recap-Claim + Zitat aus dem Fenster.
                if urteil == "widerspricht" and (not exakt or not zitat_belegt):
                    urteil = "unklar"
                claims.append({
                    "claim": claim,
                    "urteil": urteil,
                    "korrektur": klartext(c.get("korrektur") or c.get("correction") or "")[:500],
                    "begruendung": klartext(c.get("begruendung") or c.get("reason") or "")[:400],
                    "zitat": zitat,
                    "zitatBelegt": zitat_belegt,
                    "exakt": exakt,
                    "gepatcht": False,
                })
            widerspruch = [c for c in claims if c["urteil"] == "widerspricht" and c["exakt"] and c["zitatBelegt"]]
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
                        or not c.get("zitatBelegt")):
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
