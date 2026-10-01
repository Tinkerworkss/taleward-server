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
            raise SprachmodellFehler(f"Das Sprachmodell hat sich in {len(self.VERSUCHE)} Versuchen festgefahren "
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
            runde.append(f"- {p['name']}: Spielleitung")
        else:
            c = p.get("charakter") or "(ohne Charaktername)"
            kurz = f" – {p['charakter_kurz']}" if p.get("charakter_kurz") else ""
            runde.append(f"- {p['name']} spielt {c}{kurz}")
    runde += [f"- {g}: Gast" for g in ein.get("gaeste", [])]
    zeilen.append("Am Tisch:\n" + "\n".join(runde))
    return "\n".join(zeilen)


SYSTEM_NOTIZEN = """Du hilfst bei der Nachbereitung einer Pen-&-Paper-Rollenspielsession. Du bekommst einen Abschnitt \
des Transkripts (automatisch erkannt, mit Fehlern; Sprecher sind Charaktere oder die Spielleitung).
Schreibe knappe Szenennotizen zu diesem Abschnitt: was in der Spielwelt geschieht, Orte, Nichtspielercharaktere mit \
Namen, Gegenstände, Aufträge, Entscheidungen der Gruppe, offene Fragen. Jede Notiz beginnt mit dem Zeitstempel der \
Stelle, z. B. „[12:34]“. Übernimm Namen genau so, wie sie gesagt werden, und wichtige Aussagen wörtlich in \
Anführungszeichen. Lass Regelfragen, Würfelwürfe, Pausen und Gespräche außerhalb des Spiels weg. Offensichtliche \
Witze markierst du mit „(Witz?)“. Erfinde nichts.
Antworte nur mit JSON: {"notizen": ["[m:ss] …", …]}. Sprache der Notizen: {sprache}."""

SYSTEM_RECAP = """Du schreibst den Recap („Was bisher geschah“) einer Pen-&-Paper-Rollenspielsession. Er wird vor der \
nächsten Session allen Spielern vorgelesen.
Regeln:
- Nur, was am Tisch als Spielgeschehen passiert ist. Nichts erfinden, nichts ausschmücken, was nicht vorkam.
- Erzählstimme in der Vergangenheit, lebendig und vorlesbar, im Ton der Kampagne und ihrer Welt. 250–600 Wörter, \
Absätze durch Leerzeilen getrennt.
- Die Figuren heißen nach ihren Charakteren, nicht nach den Menschen am Tisch. Die Spielleitung, Regeln, Würfe und \
Gespräche außerhalb des Spiels kommen nicht vor.
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
- gmNotes nur bei create: was die Spielleitung am Tisch verraten hat, die Spieler aber nicht wissen sollen. Sonst leer.
- suggestedVisibility: public, wenn die Spieler es am Tisch erfahren haben; gm_only, wenn nur die Spielleitung davon \
gesprochen hat. visibilityReason: ein kurzer Satz.
- confidence zwischen 0 und 1. flags: joke_suspected (vermutlich Witz), low_confidence, contradicts_bible (widerspricht \
einem vorhandenen Eintrag).
- evidence: 1 bis 3 Belege {"start": "m:ss", "quote": wörtliches Zitat, höchstens 200 Zeichen}.
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
- stellen: 1 bis 3 Stellen der Grundlage, auf die sich der Absatz stützt – Zeitangabe "m:ss" und ein kurzes \
wörtliches Zitat daraus (höchstens 20 Wörter). Leer, wenn nichts belegt ist.
- begruendung: ein kurzer Satz, was fehlt, abweicht oder erfunden ist. Leer bei "belegt".
Antworte nur mit JSON: {"absaetze": [{"nr": 1, "urteil": "…", "stellen": [{"zeit": "m:ss", "zitat": "…"}], \
"begruendung": "…"}]}. Sprache der Begründungen: {sprache}."""

SYSTEM_NACHBESSERUNG = """Du überarbeitest einzelne Absätze des Recaps einer Pen-&-Paper-Session. Eine Prüfung hat \
sie beanstandet; der Grund steht jeweils dabei.
Schreibe jeden genannten Absatz neu, sodass er nur noch enthält, was die Grundlage belegt. Lass Unbelegtes und \
Ausgeschmücktes weg, statt es umzuformulieren – lieber kürzer. Gleicher Ton, Erzählstimme in der Vergangenheit, Figuren \
nach ihren Charakteren. Bleibt von einem Absatz nichts Belegtes übrig, gib als text "" zurück. Reiner Text ohne Markdown.
Antworte nur mit JSON: {"absaetze": [{"nr": 2, "text": "…"}]}. Sprache: {sprache}."""

URTEILE = {"belegt": "supported", "teilweise": "partial", "unbelegt": "unsupported", "widerspricht": "contradicted",
           "witz": "off_game", "supported": "supported", "partial": "partial", "unsupported": "unsupported",
           "contradicted": "contradicted", "off_game": "off_game"}
BEANSTANDET = ("unsupported", "contradicted", "off_game")


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


def _json(antwort: Antwort) -> dict:
    text = antwort.text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        wert = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise SprachmodellFehler("Das Sprachmodell hat kein gültiges JSON geliefert.") from None
        try:
            wert = json.loads(m.group(0))
        except ValueError:
            raise SprachmodellFehler("Das Sprachmodell hat kein gültiges JSON geliefert.") from None
    if not isinstance(wert, dict):
        raise SprachmodellFehler("Das Sprachmodell hat kein JSON-Objekt geliefert.")
    return wert


# ---------------------------------------------------------------- Ablauf
def _zahl(n: int) -> str:
    return f"{n:,}".replace(",", " ")  # 3 800 mit schmalem Leerzeichen – kein Punkt, der mit dem Komma streitet


@dataclass
class Zaehler:
    tokens_in: int = 0
    tokens_out: int = 0
    aufrufe: int = 0

    token_s: list[float] = field(default_factory=list)  # gemessene Geschwindigkeit je Aufruf (lokales Modell)

    def aufruf(self, klient: Klient, system: str, nutzer: str) -> dict:
        a = self._chat(klient, system, nutzer)
        try:
            return _json(a)
        except SprachmodellFehler:  # ein zweiter Versuch – kleine Modelle stolpern gelegentlich
            a = self._chat(klient, system, nutzer + "\n\nAntworte ausschließlich mit gültigem JSON.")
            return _json(a)

    def _chat(self, klient: Klient, system: str, nutzer: str) -> Antwort:
        a = klient.chat(system, nutzer)
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


@dataclass
class Ablauf:
    klient: Klient
    max_transkript_tokens: int = 90_000  # darüber: erst Szenennotizen
    stueck_tokens: int = 6_000
    zaehler: Zaehler = field(default_factory=Zaehler)
    schritt: Callable[[str], None] | None = None  # Zwischenstand für die App (summarizing.notes, .recap …)

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
        system = SYSTEM_NOTIZEN.replace("{sprache}", _sprache(ein))
        self._schritt("notes")
        for runde in range(3):  # sehr lange Sessions bei kleinem Kontext: Notizen noch einmal verdichten
            teile = stuecke(zeilen, self.stueck_tokens)
            notizen = []
            for i, teil in enumerate(teile):
                was = "des Transkripts" if runde == 0 else "der bisherigen Szenennotizen (bitte weiter verdichten)"
                d = self.zaehler.aufruf(self.klient, system,
                                        f"{kopf}\n\nAbschnitt {i + 1} von {len(teile)} {was}:\n{teil}")
                notizen += [str(n).strip() for n in d.get("notizen") or [] if str(n).strip()]
                fortschritt(min(0.6, 0.6 * (runde * 0.3 + (i + 1) / len(teile) * 0.7)))
            text = "\n".join(notizen)
            if tokens(text) <= self.max_transkript_tokens or not notizen:
                break
            zeilen = notizen
        return "Szenennotizen (aus dem Transkript verdichtet)", text

    def recap(self, ein: dict, titel: str, grundlage: str) -> dict:
        bibel = "\n".join(f"- [{e['typ']}] {e['name']}" + (f": {e['zusammenfassung'][:500]}"
                                                           if _erwaehnt(e["name"], grundlage) else "")
                          for e in ein["bibel"])
        nutzer = (f"{_kopf(ein)}\n\nBekannt aus früheren Sessions (Spielerwissen):\n{bibel or '(noch nichts)'}"
                  f"\n\n{titel}:\n{grundlage}")
        system = SYSTEM_RECAP.replace("{sprache}", _sprache(ein)).replace("{nummer}", str(ein["session_nummer"]))
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
            befund = self.pruefen(ein, titel, grundlage, r["text"])
            if any(b["verdict"] in BEANSTANDET for b in befund):
                self._schritt("revision")
                neu = self.nachbessern(ein, titel, grundlage, r["text"], befund)
                if neu:
                    r["text"], pruefung["revised"] = neu, True
                    self._schritt("review")
                    befund = self.pruefen(ein, titel, grundlage, neu)
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
                  f"\n\nGeheime Einträge (nur Spielleitung):\n{geheim or '(keine)'}\n\n{titel}:\n{grundlage}")
        system = (SYSTEM_VORSCHLAEGE.replace("{sprache}", _sprache(ein)).replace("{max}", str(MAX_VORSCHLAEGE)))
        self._schritt("proposals")
        d = self.zaehler.aufruf(self.klient, system, nutzer)
        return pruefen(d.get("proposals") or [], {e["id"] for e in ein["bibel"]}, {e["id"] for e in ein["geheim"]},
                       charaktere=[p["charakter"] for p in ein["personen"] if p.get("charakter")])

    def ausfuehren(self, recap_ein: dict, vorschlag_ein: dict,
                   fortschritt: Callable[[float], None] = lambda _p: None, gegenpruefen: bool = False) -> dict:
        """Das ganze Ergebnis. Die Grundlage (Transkript bzw. Notizen) ist für beide gleich; die Notizen entstehen
        aus der Recap-Eingabe, die nichts Geheimes enthält. Die Gegenprüfung sieht nur, was der Recap sah."""
        titel, grundlage = self.grundlage(recap_ein, fortschritt)
        r = self.recap(recap_ein, titel, grundlage)
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


def pruefen(roh: list, bibel_ids: set[str], geheim_ids: set[str], charaktere=()) -> list[dict]:
    """Antwort des Modells in Vorschläge nach Schnittstelle übersetzen; Unbrauchbares fällt weg – auch neue
    Einträge für die Charaktere der Spieler, die das Modell trotz Anweisung gern anlegt."""
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
                belege.append({"start": start if start is not None else 0.0,
                               "quote": str(b["quote"]).strip()[:200]})
        flags = [f for f in v.get("flags") or [] if f in FLAGS]
        if sicherheit < 0.4 and "low_confidence" not in flags:
            flags.append("low_confidence")
        out.append({
            "entryType": typ, "action": art, "targetEntryId": ziel, "title": titel[:300], "detail": detail[:4000],
            "gmNotes": (klartext(v.get("gmNotes"))[:4000] or None) if art == "create" else None,
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
