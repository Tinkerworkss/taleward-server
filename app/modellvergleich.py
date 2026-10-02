"""Modellvergleich: mehrere lokale Sprachmodelle schreiben Recap und Vorschläge für dieselbe Session.

Für Testabende am eigenen PC – ohne Datenbank und ohne Release. Das Werkzeug holt sich das fertige Transkript einer
Session als SL über die Schnittstelle des Servers (nichts wird dort geändert), lässt jedes Modell über Ollama genau den
Ablauf der Zusammenfassung durchlaufen (Szenennotizen bei Bedarf, Recap, Gegenprüfung mit einer Nachbesserung,
Vorschläge) und legt alles in einem Ordner ab:

  bericht.html   Übersicht und alle Recaps nebeneinander zum Lesen
  bericht.md     dieselbe Übersicht als Text
  <modell>/      recap.txt, vorschlaege.json, ergebnis.json je Modell

Zusätzlich bewertet ein fester „Richter“ (ein Modell für alle) jeden fertigen Recap Absatz für Absatz gegen dieselbe
Grundlage – sonst würde sich jedes Modell nur selbst prüfen.

Aufruf (Beispiel):  chronik modellvergleich --server https://taleward.example.org --benutzer anna
Ohne --session listet es die Sessions mit Transkript.
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

STANDARD_MODELLE = ("gemma4:e4b", "gemma4:12b")
STANDARD_RICHTER = "gemma4:12b"
OLLAMA_ADRESSEN = ("http://127.0.0.1:11434", "http://127.0.0.1:11435")  # eigenes Ollama, sonst das der Worker-App
APP_KENNUNG = "1.0.0"  # X-Taleward-App, falls der Server eine Mindestversion verlangt und /info nichts nennt


class VergleichFehler(Exception):
    """Fehler mit verständlicher Meldung für die Kommandozeile."""


# ---------------------------------------------------------------- Server
class Server:
    """Nur lesende Abrufe als SL über /api/v1."""

    def __init__(self, adresse: str, client: httpx.Client | None = None):
        self.basis = adresse.rstrip("/")
        if not self.basis.startswith(("http://", "https://")):
            self.basis = "https://" + self.basis
        self.client = client or httpx.Client(timeout=60.0, follow_redirects=True)
        self.kopf: dict[str, str] = {}

    def _url(self, pfad: str) -> str:
        return f"{self.basis}/api/v1{pfad}"

    def anmelden(self, benutzer: str, passwort: str) -> None:
        try:
            info = self.client.get(self._url("/info")).json()
        except (httpx.HTTPError, ValueError) as e:
            raise VergleichFehler(f"Der Server unter {self.basis} antwortet nicht wie ein Taleward-Server ({e}).") from e
        self.kopf["X-Taleward-App"] = info.get("latestAppVersion") or info.get("minAppVersion") or APP_KENNUNG
        r = self.client.post(self._url("/auth/login"), json={"username": benutzer, "password": passwort},
                             headers=self.kopf)
        if r.status_code != 200:
            raise VergleichFehler(f"Anmeldung abgelehnt ({r.status_code}): {self._meldung(r)}")
        self.kopf["Authorization"] = f"Bearer {r.json()['accessToken']}"

    @staticmethod
    def _meldung(r: httpx.Response) -> str:
        try:
            return str(r.json().get("message") or r.text[:200])
        except ValueError:
            return r.text[:200]

    def holen(self, pfad: str):
        r = self.client.get(self._url(pfad), headers=self.kopf)
        if r.status_code != 200:
            raise VergleichFehler(f"GET {pfad}: {r.status_code} {self._meldung(r)}")
        return r.json()

    def sessions_mit_transkript(self) -> list[dict]:
        aus = []
        for c in self.holen("/campaigns"):
            if c.get("myRole") != "gm":
                continue
            for s in self.holen(f"/campaigns/{c['id']}/sessions"):
                if s.get("state") in ("awaiting_review", "published", "summarizing", "failed"):
                    aus.append({**s, "kampagne": c["title"]})
        return aus


# ---------------------------------------------------------------- Eingabe wie in app/zusammenfassung.py
def eingabe_aus_schnittstelle(server: Server, session_id: str) -> tuple[dict, dict, dict]:
    """(recap_ein, vorschlag_ein, info) – gleiche Form wie eingabe_bauen/recap_eingabe/vorschlag_eingabe, nur aus
    den Antworten der Schnittstelle statt aus der Datenbank."""
    from app.zusammenfassung import BekannterEintrag, Eingabe, GeheimerEintrag, Person, Zeile

    s = server.holen(f"/sessions/{session_id}")
    c = server.holen(f"/campaigns/{s['campaignId']}")
    if c.get("myRole") != "gm":
        raise VergleichFehler("Für den Vergleich braucht es ein Konto, das in dieser Kampagne SL ist.")
    zeilen_roh = server.holen(f"/sessions/{session_id}/transcript")
    if not zeilen_roh:
        raise VergleichFehler("Diese Session hat (noch) kein Transkript.")
    try:
        sprecher = {sp["id"]: sp for sp in server.holen(f"/sessions/{session_id}/speakers")}
    except VergleichFehler:
        sprecher = {}
    eintraege = server.holen(f"/campaigns/{c['id']}/entries")
    mitglieder = {m["id"]: m for m in c.get("members") or []}
    en = c.get("language") == "en"

    personen, gaeste = [], []
    for a in s.get("attendees") or []:
        m = mitglieder.get(a.get("memberId") or "")
        if m is not None:
            personen.append(Person(m["id"], m["displayName"], m["role"], m.get("characterName"),
                                   m.get("characterSummary")))
        elif a.get("guestName"):
            gaeste.append(a["guestName"])

    def name(z: dict) -> str:
        m = mitglieder.get(z.get("memberId") or "")
        sp = sprecher.get(z.get("speakerId") or "") or {}
        if m is None and sp.get("assignedGuestName"):
            return f"{sp['assignedGuestName']} ({'guest' if en else 'Gast'})"
        if m is None:
            return sp.get("label") or ("Unknown" if en else "Unbekannt")
        if m["role"] == "gm":
            return f"{m['displayName']} ({'game master' if en else 'Spielleitung'})"
        return m.get("characterName") or m["displayName"]

    zeilen = [Zeile(z["start"], name(z), z.get("memberId"), z["text"]) for z in zeilen_roh]
    sortiert = sorted(eintraege, key=lambda e: e.get("updatedAt") or "", reverse=True)
    bibel, geheim = [], []
    for e in sortiert:
        if e["visibility"] == "public" and not e.get("hiddenFromMemberIds"):
            bibel.append(BekannterEintrag(e["id"], e["type"], e["name"], e.get("summary") or ""))
        else:
            geheim.append(GeheimerEintrag(e["id"], e["type"], e["name"]))
    basis = Eingabe(sprache=c.get("language") or "de", kampagne=c["title"], system=c.get("system"),
                    system_name=c.get("systemName"), welt=c.get("worldInfo"), session_nummer=s["number"],
                    session_titel=s.get("title"), personen=personen, gaeste=gaeste, transkript=zeilen,
                    bibel=bibel, geheim=geheim)
    recap_ein = basis.als_dict()
    recap_ein["geheim"] = []
    vorschlag_ein = basis.als_dict()
    vorschlag_ein["bibel"] = [{"id": e["id"], "typ": e["type"], "name": e["name"],
                               "zusammenfassung": e.get("summary") or "", "gm_notes": e.get("gmNotes") or None}
                              for e in sortiert if e["visibility"] == "public"]
    vorschlag_ein["geheim"] = [{"id": e["id"], "typ": e["type"], "name": e["name"],
                                "zusammenfassung": e.get("summary") or "", "gm_notes": e.get("gmNotes") or None}
                               for e in sortiert if e["visibility"] != "public"]
    info = {"kampagne": c["title"], "kapitel": s["number"], "titel": s.get("title"), "zeilen": len(zeilen),
            "dauer_s": round(max((z["end"] for z in zeilen_roh), default=0))}
    return recap_ein, vorschlag_ein, info


# ---------------------------------------------------------------- Lauf je Modell
@dataclass
class Ergebnis:
    modell: str
    kontext: int
    ok: bool = False
    fehler: str | None = None
    laden_s: float = 0.0
    dauer_s: float = 0.0
    aufrufe: int = 0
    tokens_ein: int = 0
    tokens_aus: int = 0
    grundlage: str = ""  # „Transkript“ oder „Szenennotizen …“
    letzte_antwort: str = ""  # bei einem Fehler: Rohtext der letzten Modellantwort (Fehlersuche, bleibt lokal)
    titel: str = ""
    text: str = ""
    offene_faeden: list[str] = field(default_factory=list)
    vorschlaege: list[dict] = field(default_factory=list)
    nachgebessert: bool = False
    selbst: dict = field(default_factory=dict)   # Gegenprüfung durch das Modell selbst (wie im Betrieb)
    richter: dict = field(default_factory=dict)  # Bewertung des fertigen Textes durch den festen Richter
    richter_absaetze: list[dict] = field(default_factory=list)

    @property
    def token_s(self) -> float:
        return self.tokens_aus / self.dauer_s if self.dauer_s else 0.0


def zaehlen(absaetze: list[dict]) -> dict:
    from app.pruefteil import BERICHT

    z = {"total": len(absaetze), "supported": 0, "partial": 0, "unsupported": 0, "contradicted": 0, "offGame": 0,
         "unchecked": 0}
    for a in absaetze:
        v = a.get("verdict")
        z[BERICHT.get(v, "unchecked")] += 1
    return z


def _ablauf_klasse():
    from app.sprachmodell import Ablauf

    class Merker(Ablauf):
        """Wie im Betrieb, merkt sich aber die Grundlage für den Richter."""
        letzte_grundlage: tuple[str, str] = ("", "")

        def grundlage(self, ein, fortschritt):
            self.letzte_grundlage = super().grundlage(ein, fortschritt)
            return self.letzte_grundlage

    return Merker


def klient(url: str, modell: str, kontext: int, client: httpx.Client | None = None):
    from app.sprachmodell import OllamaKlient

    return OllamaKlient(url, modell, kontext, client=client)


def modell_laufen(url: str, modell: str, kontext: int, recap_ein: dict, vorschlag_ein: dict,
                  melden: Callable[[str], None], client: httpx.Client | None = None) -> tuple[Ergebnis, object]:
    """Ein Modell durch den ganzen Ablauf. Liefert (Ergebnis, Ablauf) – der Ablauf trägt die Grundlage."""
    from app.sprachmodell import SprachmodellFehler

    erg = Ergebnis(modell=modell, kontext=kontext)
    k = klient(url, modell, kontext, client)
    t0 = time.monotonic()
    try:
        k.bereitstellen(melden)
    except SprachmodellFehler as e:
        erg.fehler = f"Laden fehlgeschlagen: {e}"
        return erg, None
    erg.laden_s = time.monotonic() - t0
    ablauf = _ablauf_klasse()(k, max_transkript_tokens=max(2000, kontext - 5000),
                              stueck_tokens=max(1500, (kontext - 4000) // 2), schritt=lambda n: melden(f"  … {n}"))
    t0 = time.monotonic()
    try:
        d = ablauf.ausfuehren(recap_ein, vorschlag_ein, lambda _p: None, gegenpruefen=True)
        erg.ok = True
        erg.titel, erg.text, erg.offene_faeden = d["title"], d["text"], d["openThreads"]
        erg.vorschlaege = d["proposals"]
        erg.nachgebessert = bool(d["review"].get("revised"))
        erg.selbst = zaehlen(d["review"]["paragraphs"])
    except SprachmodellFehler as e:
        erg.fehler = str(e)
        erg.letzte_antwort = ablauf.zaehler.letzte_antwort[-20000:]
    finally:
        erg.dauer_s = time.monotonic() - t0
        erg.aufrufe, erg.tokens_ein, erg.tokens_aus = (ablauf.zaehler.aufrufe, ablauf.zaehler.tokens_in,
                                                       ablauf.zaehler.tokens_out)
        erg.grundlage = ablauf.letzte_grundlage[0]
        k.entladen()
    return erg, ablauf


def richten(url: str, richter: str, kontext: int, recap_ein: dict, ergebnisse: list[tuple[Ergebnis, object]],
            melden: Callable[[str], None], client: httpx.Client | None = None) -> None:
    """Ein festes Modell bewertet jeden fertigen Recap gegen die Grundlage, aus der er entstand."""
    from app.sprachmodell import SprachmodellFehler

    k = klient(url, richter, kontext, client)
    try:
        k.bereitstellen(melden)
    except SprachmodellFehler as e:
        melden(f"Richter {richter} nicht verfügbar: {e}")
        return
    pruefer = _ablauf_klasse()(k)
    try:
        for erg, ablauf in ergebnisse:
            if not erg.ok or ablauf is None:
                continue
            melden(f"Richter {richter} bewertet {erg.modell} …")
            titel, grundlage = ablauf.letzte_grundlage
            try:
                absaetze = pruefer.pruefen(recap_ein, titel, grundlage, erg.text)
            except SprachmodellFehler as e:
                melden(f"  übersprungen: {e}")
                continue
            erg.richter_absaetze = absaetze
            erg.richter = zaehlen(absaetze)
    finally:
        k.entladen()


# ---------------------------------------------------------------- Bericht
def _anteil(z: dict, *schluessel: str) -> str:
    if not z or not z.get("total"):
        return "–"
    return f"{sum(z.get(s, 0) for s in schluessel)}/{z['total']}"


def _zeit(s: float) -> str:
    return f"{s / 60:.1f} min" if s >= 90 else f"{s:.0f} s"


def bericht_md(info: dict, richter: str, ergebnisse: list[Ergebnis]) -> str:
    zeilen = [f"# Modellvergleich – {info['kampagne']}, Kapitel {info['kapitel']}", "",
              f"{info['zeilen']} Transkriptzeilen, Aufnahme {_zeit(info['dauer_s'])}. Richter: {richter or '–'}.", "",
              "| Modell | Ergebnis | Dauer | Token/s | belegt (selbst) | unbelegt/widersprochen (selbst) | "
              "belegt (Richter) | unbelegt/widersprochen (Richter) | Vorschläge | nachgebessert | Grundlage |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for e in ergebnisse:
        zeilen.append(
            f"| {e.modell} (ctx {e.kontext}) | {'ok' if e.ok else 'Fehler'} | {_zeit(e.dauer_s)} | {e.token_s:.1f} | "
            f"{_anteil(e.selbst, 'supported')} | {_anteil(e.selbst, 'unsupported', 'contradicted')} | "
            f"{_anteil(e.richter, 'supported')} | {_anteil(e.richter, 'unsupported', 'contradicted')} | "
            f"{len(e.vorschlaege)} | {'ja' if e.nachgebessert else 'nein'} | {e.grundlage or '–'} |")
    for e in ergebnisse:
        zeilen += ["", f"## {e.modell}", ""]
        if not e.ok:
            zeilen.append(f"Fehler: {e.fehler}")
            continue
        zeilen += [f"### {e.titel}", "", e.text, ""]
        if e.offene_faeden:
            zeilen += ["Offene Fäden:"] + [f"- {f}" for f in e.offene_faeden]
    return "\n".join(zeilen) + "\n"


_FARBE = {"supported": "#5b7f5a", "partial": "#a07a2c", "unsupported": "#9b3b32", "contradicted": "#9b3b32",
          "off_game": "#777", "unchecked": "#999"}


def bericht_html(info: dict, richter: str, ergebnisse: list[Ergebnis]) -> str:
    e_ = html.escape
    kopf = "".join(f"<th>{e_(x)}</th>" for x in (
        "Modell", "Ergebnis", "Dauer", "Token/s", "belegt (selbst)", "unbelegt (selbst)", "belegt (Richter)",
        "unbelegt (Richter)", "Vorschläge", "nachgebessert", "Grundlage"))
    reihen = "".join(
        "<tr>" + "".join(f"<td>{e_(str(x))}</td>" for x in (
            f"{e.modell} (ctx {e.kontext})", "ok" if e.ok else "Fehler", _zeit(e.dauer_s), f"{e.token_s:.1f}",
            _anteil(e.selbst, "supported"), _anteil(e.selbst, "unsupported", "contradicted"),
            _anteil(e.richter, "supported"), _anteil(e.richter, "unsupported", "contradicted"),
            len(e.vorschlaege), "ja" if e.nachgebessert else "nein", e.grundlage or "–")) + "</tr>"
        for e in ergebnisse)

    def spalte(e: Ergebnis) -> str:
        if not e.ok:
            return f"<section><h2>{e_(e.modell)}</h2><p class='fehler'>{e_(e.fehler or '')}</p></section>"
        urteile = {a["index"]: a for a in e.richter_absaetze}
        from app.sprachmodell import absaetze

        teile = []
        for i, a in enumerate(absaetze(e.text)):
            u = urteile.get(i, {})
            farbe = _FARBE.get(u.get("verdict", "unchecked"), "#999")
            notiz = f"<small>{e_(u.get('verdict', ''))}: {e_(u.get('note') or '')}</small>" if u else ""
            teile.append(f"<p style='border-left:4px solid {farbe};padding-left:8px'>{e_(a)}<br>{notiz}</p>")
        faeden = "".join(f"<li>{e_(f)}</li>" for f in e.offene_faeden)
        vorschl = "".join(f"<li><b>{e_(v['action'])} {e_(v['entryType'])}</b> {e_(v['title'])}: "
                          f"{e_(v['detail'][:240])}</li>" for v in e.vorschlaege)
        return (f"<section><h2>{e_(e.modell)}</h2><h3>{e_(e.titel)}</h3>{''.join(teile)}"
                f"{'<h4>Offene Fäden</h4><ul>' + faeden + '</ul>' if faeden else ''}"
                f"<details><summary>Vorschläge ({len(e.vorschlaege)})</summary><ul>{vorschl}</ul></details></section>")

    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Modellvergleich</title>
<style>body{{font:16px/1.5 Georgia,serif;margin:16px;background:#f6f1e7;color:#2b2420}}
table{{border-collapse:collapse;font:14px system-ui,sans-serif;margin-bottom:24px}}td,th{{border:1px solid #c9bda6;padding:4px 8px}}
.spalten{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}}
section{{background:#fffdf8;border:1px solid #d9cfbd;border-radius:8px;padding:12px}}small{{color:#6b5f52;font-family:system-ui}}
.fehler{{color:#9b3b32}}@media (prefers-color-scheme:dark){{body{{background:#1d1a17;color:#e9e1d3}}
section{{background:#26221e;border-color:#3b342d}}td,th{{border-color:#4a4138}}small{{color:#b5a995}}}}</style></head><body>
<h1>Modellvergleich – {e_(info['kampagne'])}, Kapitel {info['kapitel']}</h1>
<p>{info['zeilen']} Transkriptzeilen, Aufnahme {_zeit(info['dauer_s'])}. Richter: {e_(richter or '–')}.
Randfarbe = Urteil des Richters (grün belegt, gelb teilweise, rot unbelegt/widersprochen).</p>
<table><tr>{kopf}</tr>{reihen}</table><div class="spalten">{''.join(spalte(e) for e in ergebnisse)}</div></body></html>
"""


def _ordnername(modell: str) -> str:
    return re.sub(r"[^\w.-]+", "_", modell)


def speichern(ordner: Path, info: dict, richter: str, ergebnisse: list[Ergebnis]) -> None:
    ordner.mkdir(parents=True, exist_ok=True)
    for e in ergebnisse:
        d = ordner / _ordnername(f"{e.modell}-ctx{e.kontext}")  # dasselbe Modell mit zwei Kontexten getrennt
        d.mkdir(exist_ok=True)
        (d / "recap.txt").write_text(f"{e.titel}\n\n{e.text}\n" if e.ok else f"Fehler: {e.fehler}\n", encoding="utf-8")
        if e.letzte_antwort:
            (d / "letzte-antwort.txt").write_text(e.letzte_antwort, encoding="utf-8")
        (d / "vorschlaege.json").write_text(json.dumps(e.vorschlaege, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "ergebnis.json").write_text(json.dumps({**asdict(e), "letzte_antwort": None, "token_s": e.token_s}, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    (ordner / "bericht.md").write_text(bericht_md(info, richter, ergebnisse), encoding="utf-8")
    (ordner / "bericht.html").write_text(bericht_html(info, richter, ergebnisse), encoding="utf-8")


def transkript_speichern(ordner: Path, recap_ein: dict) -> Path:
    """Das Transkript so, wie es die Modelle bekommen – als Grundlage für einen Referenz-Recap (z. B. mit einem
    großen Modell geschrieben) und zum Nachprüfen der Recaps. Nur auf dem eigenen PC, wird nirgends hingeschickt."""
    ordner.mkdir(parents=True, exist_ok=True)
    kopf = [f"{recap_ein.get('kampagne')} – Kapitel {recap_ein.get('session_nummer')}"
            + (f": {recap_ein['session_titel']}" if recap_ein.get("session_titel") else "")]
    for p in recap_ein.get("personen") or []:
        rolle = "Spielleitung" if p.get("rolle") == "gm" else "Spieler"
        kopf.append(f"- {p.get('name')} ({rolle})" + (f" spielt {p['charakter']}" if p.get("charakter") else "")
                    + (f": {p['charakter_kurz']}" if p.get("charakter_kurz") else ""))
    kopf += [f"- {g} (Gast)" for g in recap_ein.get("gaeste") or []]

    def uhr(sek: float) -> str:
        sek = int(sek)
        return f"{sek // 3600}:{sek % 3600 // 60:02d}:{sek % 60:02d}"

    zeilen = [f"[{uhr(z['start'])}] {z['sprecher']}: {z['text']}" for z in recap_ein.get("transkript") or []]
    pfad = ordner / "transkript.txt"
    pfad.write_text("\n".join(kopf) + "\n\n" + "\n".join(zeilen) + "\n", encoding="utf-8")
    return pfad


def modelle_lesen(text: str, kontext: int) -> list[tuple[str, int]]:
    """„a, b@8192“ → [(a, kontext), (b, 8192)]."""
    aus = []
    for teil in (t.strip() for t in text.split(",")):
        if not teil:
            continue
        name, _, ctx = teil.partition("@")
        aus.append((name.strip(), int(ctx) if ctx.strip().isdigit() else kontext))
    return aus


def ollama_finden(wunsch: str | None, client: httpx.Client | None = None) -> str:
    c = client or httpx.Client(timeout=3.0)
    for url in ([wunsch] if wunsch else list(OLLAMA_ADRESSEN)):
        try:
            if c.get(f"{url.rstrip('/')}/api/version", timeout=3.0).status_code == 200:
                return url.rstrip("/")
        except httpx.HTTPError:
            continue
    raise VergleichFehler("Kein Ollama gefunden (127.0.0.1:11434 oder 11435). Läuft die Worker-App mit "
                          "„Recaps auch auf diesem PC schreiben“, oder ein eigenes Ollama?")


def ausfuehren(server: Server, session_id: str, modelle: list[tuple[str, int]], richter: str, richter_kontext: int,
               ollama_url: str, ziel: Path, melden: Callable[[str], None] = print,
               client: httpx.Client | None = None) -> Path:
    recap_ein, vorschlag_ein, info = eingabe_aus_schnittstelle(server, session_id)
    melden(f"{info['kampagne']}, Kapitel {info['kapitel']}: {info['zeilen']} Zeilen, Aufnahme {_zeit(info['dauer_s'])}")
    ordner = ziel / f"modellvergleich-{datetime.now():%Y%m%d-%H%M}"
    melden(f"Transkript: {transkript_speichern(ordner, recap_ein)}")
    paare = []
    for modell, kontext in modelle:
        melden(f"\n== {modell} (Kontext {kontext}) ==")
        erg, ablauf = modell_laufen(ollama_url, modell, kontext, recap_ein, vorschlag_ein, melden, client)
        melden(f"  {'fertig' if erg.ok else 'FEHLER: ' + str(erg.fehler)} nach {_zeit(erg.dauer_s)}")
        paare.append((erg, ablauf))
        speichern(ordner, info, richter, [p[0] for p in paare])  # Zwischenstand, falls es abbricht
    if richter:
        melden(f"\n== Richter {richter} ==")
        richten(ollama_url, richter, richter_kontext, recap_ein, paare, melden, client)
    speichern(ordner, info, richter, [p[0] for p in paare])
    return ordner
