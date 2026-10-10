"""Zusammenfassung einer Session: Recap, offene Fäden und Bibel-Vorschläge.

Wer zusammenfasst, stellt die Verwaltung ein (`einstellungen.llm_konfig`):
- attrappe / api: die Zentrale selbst (Arbeitsprozess unten, Lease-Inhaber „zentrale“).
- lokal: ein Worker mit Ollama holt den Auftrag ab (Fähigkeit `llm`) und schickt das Ergebnis zurück.
- aus: Aufträge warten.

Spoilerschutz – gilt für jede Umsetzung:
- Das Sprachmodell bekommt ausschließlich, was `eingabe_bauen` und `vorschlag_eingabe` liefern. Nur diese
  Funktionen lesen die Datenbank.
- Nie enthalten: SL-Notizen der Sessions, Charakter-Hintergründe, Kommentare.
- Recap-Aufruf (`recap_eingabe`): nur öffentliche Einträge, die niemandem verborgen sind, ohne gmNotes, und keine
  Namen geheimer Einträge.
- Vorschlags-Aufruf (`vorschlag_eingabe`): die ganze Bibel inklusive gmNotes (so in der Schnittstelle festgelegt),
  damit das Modell ergänzt statt doppelt anlegt und „reveal“ erkennt. Vorschläge sieht nur die SL.
- Bei `reveal` füllt der Server den geheimen Teil (gmNotes) selbst aus dem bisherigen Eintrag.
- Übernimmt das Modell Formulierungen aus geheimen Texten in den öffentlichen Teil, wird der Vorschlag als
  gm_only markiert (`_geheimes_im_detail`).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import belege
from app.config import get_settings
from app.db import utcnow
from app.models import (
    Campaign, Entry, EntryMention, GameSession, Job, Member, Proposal, Recap, Speaker, TranscriptSegment, UsageLog,
)
from app.services import set_state

log = logging.getLogger("zusammenfassung")

ZENTRALE = SimpleNamespace(id="zentrale")  # Lease-Inhaber für Aufträge, die die Zentrale selbst bearbeitet
MAX_ZEILEN_FUER_BELEGE = 3


# ---------------------------------------------------------------- Eingabe
@dataclass
class Person:
    member_id: str
    name: str
    rolle: str  # gm | player
    charakter: str | None = None
    charakter_kurz: str | None = None  # characterSummary (für alle sichtbar)


@dataclass
class Zeile:
    start: float
    sprecher: str  # Charaktername, Name oder „Stimme N“
    member_id: str | None
    text: str


@dataclass
class BekannterEintrag:
    id: str
    typ: str
    name: str
    zusammenfassung: str  # nur öffentlicher Text


@dataclass
class GeheimerEintrag:
    id: str
    typ: str
    name: str  # nur der Name – Inhalt bleibt in der Zentrale


@dataclass
class Eingabe:
    sprache: str
    kampagne: str
    system: str | None
    system_name: str | None
    welt: str | None
    session_nummer: int
    session_titel: str | None
    personen: list[Person]
    gaeste: list[str]
    transkript: list[Zeile]
    bibel: list[BekannterEintrag]
    geheim: list[GeheimerEintrag]

    def als_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def als_dict(self) -> dict:
        return asdict(self)


def eingabe_bauen(db: Session, s: GameSession) -> Eingabe:
    c = db.get(Campaign, s.campaign_id)
    mitglieder = {m.id: m for m in db.scalars(select(Member).where(Member.campaign_id == c.id))}
    personen, gaeste = [], []
    for a in s.attendees:
        if a.member_id and a.member_id in mitglieder:
            m = mitglieder[a.member_id]
            personen.append(Person(m.id, m.anzeigename, m.role, m.character_name, m.character_summary))
        elif a.guest_name:
            gaeste.append(a.guest_name)

    def sprecher_name(sp: Speaker | None) -> tuple[str, str | None]:
        if sp is None:
            return "Unbekannt", None
        m = mitglieder.get(sp.assigned_member_id or "")
        if m is None and sp.assigned_guest_name:  # 0.4.7: als Gast benannt
            return f"{sp.assigned_guest_name} ({'guest' if c.language == 'en' else 'Gast'})", None
        if m is None:
            return sp.label, None
        if m.role == "gm":  # ohne Namen: das Modell soll die Spielleitung nie als Figur mit Namen erzählen
            return "Game Master" if c.language == "en" else "Spielleitung", m.id
        return m.character_name or m.anzeigename, m.id

    sprecher = {sp.id: sp for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id))}
    zeilen = []
    for seg in db.scalars(select(TranscriptSegment).where(TranscriptSegment.session_id == s.id)
                          .order_by(TranscriptSegment.position)):
        name, mid = sprecher_name(sprecher.get(seg.speaker_id or ""))
        zeilen.append(Zeile(seg.start, name, mid, seg.text))

    bibel, geheim = [], []
    for e in db.scalars(select(Entry).where(Entry.campaign_id == c.id).order_by(Entry.updated_at.desc())):
        # Teilweise verborgene Einträge („Wer weiß was“) wie gm_only behandeln – den Recap lesen alle (0.3.6)
        if e.visibility == "public" and not e.hidden_member_ids:
            bibel.append(BekannterEintrag(e.id, e.type, e.name, e.summary))
        else:
            geheim.append(GeheimerEintrag(e.id, e.type, e.name))
    return Eingabe(
        sprache=c.language, kampagne=c.title, system=c.system, system_name=c.system_name, welt=c.world_info,
        session_nummer=s.number, session_titel=s.title, personen=personen, gaeste=gaeste,
        transkript=zeilen, bibel=bibel, geheim=geheim,
    )


def recap_eingabe(basis: Eingabe) -> dict:
    """Für den Recap-Aufruf: ohne die Namen geheimer Einträge."""
    d = basis.als_dict()
    d["geheim"] = []
    return d


def vorschlag_eingabe(db: Session, s: GameSession, basis: Eingabe) -> dict:
    """Für den Vorschlags-Aufruf: die ganze Bibel inkl. gmNotes (Schnittstelle: „Die Vorschlags-Pipeline bekommt
    die ganze Bibel inklusive gmNotes“). Sessions-Notizen der SL und Hintergründe bleiben draußen."""
    d = basis.als_dict()
    bibel, geheim = [], []
    for e in db.scalars(select(Entry).where(Entry.campaign_id == s.campaign_id).order_by(Entry.updated_at.desc())):
        eintrag = {"id": e.id, "typ": e.type, "name": e.name, "zusammenfassung": e.summary or "",
                   "gm_notes": e.gm_notes or None}
        # Teilweise verborgene Einträge („Wer weiß was“) nicht als allgemein bekannt ausgeben
        (bibel if e.visibility == "public" and not e.hidden_member_ids else geheim).append(eintrag)
    d["bibel"], d["geheim"] = bibel, geheim
    return d


def geheime_bibeltexte(eintraege) -> list[str]:
    """Texte, die nicht alle Spieler kennen: gmNotes, Zusammenfassungen geheimer und teilweise verborgener Einträge."""
    return [t for e in eintraege for t in (e.gm_notes or "",
                                           e.summary if e.visibility != "public" or e.hidden_member_ids else "") if t]


# ---------------------------------------------------------------- Ergebnis
@dataclass
class Vorschlag:
    entry_type: str
    action: str  # create | update | reveal
    title: str
    detail: str
    target_entry_id: str | None = None
    gm_notes: str | None = None
    suggested_visibility: str = "gm_only"
    visibility_reason: str | None = None
    confidence: float = 0.5
    flags: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)


@dataclass
class Ergebnis:
    titel: str
    text: str
    offene_faeden: list[str]
    vorschlaege: list[Vorschlag]
    modell: str
    tokens_in: int = 0
    tokens_out: int = 0
    kosten_cent: int = 0
    pruefung: dict | None = None  # Antwort der Gegenprüfung (0.4.6); None = nicht geprüft


K_GEGENPRUEFEN = "pruefung.gegenpruefen"


def gegenpruefen_an(db: Session) -> bool:
    """Verwaltung → Zusammenfassung: „Recap gegenprüfen“ (Standard an)."""
    from app.einstellungen import meta_lesen

    return meta_lesen(db, K_GEGENPRUEFEN) != "aus"


def attrappe(e: Eingabe) -> Ergebnis:
    """Platzhalter ohne Sprachmodell – erzeugt je eine Vorschlagsart, damit die App alles testen kann."""
    en = e.sprache == "en"
    spieler = [p.charakter or p.name for p in e.personen if p.rolle == "player"] + e.gaeste
    dauer = int(e.transkript[-1].start // 60) + 1 if e.transkript else 0
    runde = ", ".join(spieler) if spieler else ("die Gruppe" if not en else "the group")
    belege = [{"start": z.start, "quote": z.text[:200]} for z in e.transkript[:MAX_ZEILEN_FUER_BELEGE]]
    if en:
        titel = f"Chapter {e.session_nummer}: Placeholder"
        text = (f"Placeholder (test mode): Here the narrated recap of chapter {e.session_nummer} will appear, "
                f"told in the style of the campaign „{e.kampagne}“.\n\n"
                f"Present: {runde}. The transcript has {len(e.transkript)} lines covering about {dauer} minutes.")
        faeden = ["Placeholder: an unresolved question from this session", "Placeholder: a promise to keep"]
    else:
        titel = f"Kapitel {e.session_nummer}: Platzhalter"
        text = (f"Platzhalter (Testmodus): Hier steht künftig der vorlesbare Recap von Kapitel {e.session_nummer}, "
                f"erzählt im Ton der Kampagne „{e.kampagne}“.\n\n"
                f"Dabei waren: {runde}. Das Transkript hat {len(e.transkript)} Zeilen aus etwa {dauer} Minuten.")
        faeden = ["Platzhalter: eine offene Frage aus dieser Session", "Platzhalter: ein gegebenes Versprechen"]
    v = [
        Vorschlag("npc", "create", "Platzhalter-NSC" if not en else "Placeholder NPC",
                  "Platzhalter (Testmodus): Wer ist das, was weiß die Gruppe über ihn?" if not en
                  else "Placeholder (test mode): who is this, what does the group know?",
                  suggested_visibility="public", visibility_reason="Am Tisch vorgestellt (Testmodus)",
                  confidence=0.8, evidence=belege[:1]),
        Vorschlag("location", "create", "Platzhalter-Ort" if not en else "Placeholder location",
                  "Platzhalter (Testmodus): Ein Ort, von dem nur die SL gesprochen hat." if not en
                  else "Placeholder (test mode): a place only the GM talked about.",
                  gm_notes="Platzhalter: geheimer Teil" if not en else "Placeholder: secret part",
                  suggested_visibility="gm_only", confidence=0.4, flags=["joke_suspected", "low_confidence"],
                  evidence=belege[1:2]),
    ]
    if e.bibel:
        ziel = e.bibel[0]
        v.append(Vorschlag(ziel.typ, "update", ziel.name,
                           "Platzhalter (Testmodus): Neues aus dieser Session." if not en
                           else "Placeholder (test mode): news from this session.",
                           target_entry_id=ziel.id, suggested_visibility="public", confidence=0.6,
                           evidence=belege[2:3]))
    if e.geheim:
        ziel = e.geheim[0]
        v.append(Vorschlag(ziel.typ, "reveal", ziel.name,
                           "Platzhalter (Testmodus): Das weiß die Gruppe jetzt." if not en
                           else "Placeholder (test mode): what the group knows now.",
                           target_entry_id=ziel.id, suggested_visibility="public",
                           visibility_reason="Die Gruppe ist ihm begegnet (Testmodus)", confidence=0.7,
                           evidence=belege[:1]))
    # Prüfteil zum Ausprobieren in der App: erster Absatz belegt, zweiter teilweise
    pruefung = {"model": "attrappe", "revised": False, "paragraphs": [
        {"index": 0, "verdict": "supported", "note": None, "evidence": belege[:1]},
        {"index": 1, "verdict": "partial",
         "note": "Platzhalter: Die Dauer steht so nicht im Transkript." if not en
         else "Placeholder: the duration is not stated in the transcript.", "evidence": belege[1:2]},
    ]}
    return Ergebnis(titel, text, faeden, v, modell="attrappe", pruefung=pruefung)


def ergebnis_aus(d: dict, kosten_cent: int = 0) -> Ergebnis:
    """Ergebnis des Sprachmodells (sprachmodell.Ablauf bzw. Worker) → Ergebnis."""
    v = [Vorschlag(entry_type=p["entryType"], action=p["action"], title=p["title"], detail=p.get("detail") or "",
                   target_entry_id=p.get("targetEntryId"), gm_notes=p.get("gmNotes"),
                   suggested_visibility=p.get("suggestedVisibility") or "gm_only",
                   visibility_reason=p.get("visibilityReason"), confidence=float(p.get("confidence", 0.5)),
                   flags=list(p.get("flags") or []), evidence=list(p.get("evidence") or []))
         for p in d.get("proposals") or []]
    return Ergebnis(titel=d.get("title") or "", text=d["text"], offene_faeden=list(d.get("openThreads") or []),
                    vorschlaege=v, modell=d.get("model") or "?", tokens_in=int(d.get("tokensIn") or 0),
                    tokens_out=int(d.get("tokensOut") or 0), kosten_cent=kosten_cent,
                    pruefung=d.get("review") if isinstance(d.get("review"), dict) else None)


def api_klient(k):
    from app.sprachmodell import OpenAIKlient

    preis = (k.cent_ein, k.cent_aus) if k.cent_ein is not None and k.cent_aus is not None else None
    return OpenAIKlient(k.api_url, k.api_key, k.api_modell, cent_pro_mio=preis)


def api_klient_vorschlaege(k):
    """0.4.61: eigener Klient für die Vorschläge, wenn ein anderes Modell gewählt ist (sonst None). Eigene Preise aus
    der Verwaltung gelten nur für das Kapitel-Modell; hier zählt die Preistabelle."""
    from app.sprachmodell import OpenAIKlient

    if not k.api_modell_vorschlaege or k.api_modell_vorschlaege == k.api_modell:
        return None
    return OpenAIKlient(k.api_url, k.api_key, k.api_modell_vorschlaege)


def api_klient_notizen(k):
    """0.4.73: eigener Klient für Notizen, Stand und Auswahl, wenn ein anderes Modell gewählt ist (sonst None) – zum
    Messen, ob ein günstigeres Modell beim Zuhören mithält."""
    from app.sprachmodell import OpenAIKlient

    if not k.api_modell_notizen or k.api_modell_notizen == k.api_modell:
        return None
    return OpenAIKlient(k.api_url, k.api_key, k.api_modell_notizen)


def zusammenfasser(db: Session):
    """Was die Zentrale selbst ausführt: fn(db, session) → (Ergebnis, engine). None = nicht die Zentrale
    (aus, oder lokal – dann holt ein Worker mit Sprachmodell den Auftrag)."""
    from app.einstellungen import llm_konfig

    k = llm_konfig(db)
    if k.art == "attrappe":
        return lambda db_, s: (attrappe(eingabe_bauen(db_, s)), "local")
    if k.art == "api" and k.api_key:
        from app.sprachmodell import Ablauf

        def ueber_api(db_, s):
            klient = api_klient(k)
            basis = eingabe_bauen(db_, s)
            # 0.4.62: keine Nachbesserung über die Cloud – sie hat in der Messung nur Kosten gebracht und das Kapitel
            # verschlechtert; die Prüfung bleibt als Hinweis für die Spielleitung. 0.4.72: Standard ist „erst Notizen je
            # Abschnitt, dann das Kapitel“ (in der Messung auf zwei Systemen deutlich besser); Rückweg in der Verwaltung
            ablauf = Ablauf(klient, schritt=lambda name: schritt_setzen(db_, s.id, name),
                            vorschlag_klient=api_klient_vorschlaege(k), nachbesserung=False,
                            notizen_zuerst=k.weg == "notizen", notiz_klient=api_klient_notizen(k))
            d = ablauf.ausfuehren(recap_eingabe(basis), vorschlag_eingabe(db_, s, basis),
                                  gegenpruefen=gegenpruefen_an(db_))
            return ergebnis_aus(d, d.get("costCents", klient.kosten_cent(d["tokensIn"], d["tokensOut"]))), "external"
        return ueber_api
    return None


# ---------------------------------------------------------------- Speichern
def _woerter(text: str) -> list[str]:
    import re

    return re.findall(r"\w+", text.lower())


def _geheimes_im_detail(detail: str, geheime_texte: list[str], n: int = 6) -> bool:
    """Übernimmt der öffentliche Teil eine Folge von n Wörtern aus einem geheimen Text (bei kurzen geheimen
    Texten ab 3 Wörtern: den ganzen Text)?"""
    w = _woerter(detail)
    for g in geheime_texte:
        gw = _woerter(g)
        laenge = min(n, len(gw))
        if laenge < 3:
            continue
        folgen = {tuple(w[i:i + laenge]) for i in range(len(w) - laenge + 1)}
        if any(tuple(gw[i:i + laenge]) in folgen for i in range(len(gw) - laenge + 1)):
            return True
    return False


GEHEIM_HINWEIS = {"de": "Enthält Formulierungen aus geheimen Notizen – bitte vor der Freigabe prüfen.",
                  "en": "Contains wording from secret notes – please check before sharing."}


def speichern(db: Session, s: GameSession, erg: Ergebnis, rechenzeit: float, engine: str = "local",
              worker_id: str | None = None) -> None:
    """Ersetzt Recap und Vorschläge dieser Session (bei Neustart) und stellt die Session zur Prüfung bereit."""
    db.execute(delete(Proposal).where(Proposal.session_id == s.id))
    db.execute(delete(Recap).where(Recap.session_id == s.id))
    from app import pruefteil

    sprache = db.get(Campaign, s.campaign_id).language
    mitglied = {sp.id: sp.assigned_member_id for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id))}
    abschnitte = [pruefteil.Abschnitt(seg.start, seg.end, seg.text, mitglied.get(seg.speaker_id or ""))
                  for seg in db.scalars(select(TranscriptSegment).where(TranscriptSegment.session_id == s.id)
                                        .order_by(TranscriptSegment.position))]
    stellen = pruefteil.Stellen(abschnitte)
    text = erg.text.strip()
    db.add(Recap(session_id=s.id, title=erg.titel.strip()[:300] or f"Kapitel {s.number}", text=text,
                 open_threads=json.dumps([f.strip() for f in erg.offene_faeden if f.strip()], ensure_ascii=False),
                 model=erg.modell, review=pruefteil.als_json(pruefteil.bauen(erg.pruefung, text, stellen, sprache))))
    eintraege = {e.id: e for e in db.scalars(select(Entry).where(Entry.campaign_id == s.campaign_id))}
    geheime_texte = geheime_bibeltexte(eintraege.values())
    # Qualitätsprüfung Stufe 1: Belege gegen das Transkript prüfen (erfundene Zitate fallen weg)
    transkript = stellen.woerter
    ohne_beleg = 0
    for pos, v in enumerate(erg.vorschlaege):
        ziel = eintraege.get(v.target_entry_id or "")
        if v.action in ("update", "reveal") and ziel is None:
            continue  # Bezug auf einen Eintrag, den es nicht (mehr) gibt
        if ziel is not None and ziel.type == "pc":
            continue  # Spielercharaktere pflegt die App (0.4.7) – keine Vorschläge dafür
        if v.action == "reveal" and ziel.visibility != "gm_only":
            continue  # schon öffentlich – nichts aufzudecken
        gm_notes = v.gm_notes
        if v.action == "reveal":
            # Der geheime Teil kommt aus dem bisherigen Eintrag, nicht vom Sprachmodell
            gm_notes = "\n\n".join(t for t in (ziel.summary.strip(), (ziel.gm_notes or "").strip()) if t) or None
        sichtbar, grund, flags = v.suggested_visibility, v.visibility_reason, list(v.flags)
        sicherheit = max(0.0, min(1.0, v.confidence))
        geprueft = belege.pruefen(transkript, v.evidence)
        if not geprueft:
            ohne_beleg += 1
            sicherheit = min(sicherheit, belege.KEIN_BELEG_SICHERHEIT)
            for f in ("low_confidence", "evidence_not_found"):  # 0.4.6: die App nennt den Grund
                if f not in flags:
                    flags.append(f)
        if sichtbar == "public" and _geheimes_im_detail(v.detail, geheime_texte):
            sichtbar, grund = "gm_only", GEHEIM_HINWEIS.get(sprache, GEHEIM_HINWEIS["de"])
            if "low_confidence" not in flags:
                flags.append("low_confidence")
        db.add(Proposal(
            campaign_id=s.campaign_id, session_id=s.id, position=pos, entry_type=v.entry_type, action=v.action,
            target_entry_id=v.target_entry_id if v.action != "create" else None, title=v.title.strip()[:300],
            detail=v.detail.strip(), gm_notes=(gm_notes or "").strip() or None,
            suggested_visibility=sichtbar, visibility_reason=grund,
            confidence=sicherheit, flags=json.dumps(flags),
            evidence=json.dumps(geprueft, ensure_ascii=False),
        ))
    if ohne_beleg:
        log.info("Session %s: %d von %d Vorschlägen ohne auffindbaren Beleg im Transkript", s.id, ohne_beleg,
                 len(erg.vorschlaege))
    db.add(UsageLog(campaign_id=s.campaign_id, session_id=s.id, kind="summary", engine=engine, model=erg.modell,
                    worker_id=worker_id, compute_seconds=rechenzeit, tokens_in=erg.tokens_in,
                    tokens_out=erg.tokens_out, cost_cents=erg.kosten_cent))
    set_state(s, "awaiting_review")


# ---------------------------------------------------------------- Arbeitsprozess
def einen_auftrag(db: Session) -> bool:
    """Bearbeitet höchstens einen Auftrag mit Sprachmodell (Zusammenfassung oder SL-Unterlage).
    True, wenn einer bearbeitet wurde."""
    from app import queue

    fn = zusammenfasser(db)
    if fn is None:
        return False
    from app import kosten
    from app.einstellungen import llm_konfig

    api = llm_konfig(db).art == "api"
    if api and kosten.erreicht(db):
        return False  # Monatslimit für Cloud-Dienste erreicht – Aufträge warten
    # Über die Cloud nur Kampagnen, deren SL es erlaubt hat (0.3.10); die anderen warten
    job = queue.claim(db, ZENTRALE, ["llm"], darf=queue.cloud_erlaubt if api else None)
    if job is None:
        return False
    if job.type == "document":  # SL-Unterlage: gleiche Einstellung, eigener Ablauf
        from app.unterlagen import auftrag_ausfuehren

        auftrag_ausfuehren(db, job, ZENTRALE)
        return True
    s = db.get(GameSession, job.session_id)
    t0 = time.monotonic()
    try:
        erg, engine = fn(db, s)
        db.refresh(job)
        if not queue.holds_lease(job, ZENTRALE):
            db.rollback()
            return True  # inzwischen zurückgeholt (z. B. sehr langsames Sprachmodell) – Ergebnis verwerfen
        speichern(db, s, erg, time.monotonic() - t0, engine=engine, worker_id=ZENTRALE.id)
        job.state, job.finished_at, job.progress = "done", utcnow(), 1.0
        job.lease_expires_at = None
        db.commit()
    except Exception as e:  # Sprachmodell nicht erreichbar, unerwartete Antwort …
        db.rollback()
        log.exception("Zusammenfassung für Session %s fehlgeschlagen", job.session_id)
        job = db.get(Job, job.id)
        from app.sprachmodell import SprachmodellFehler

        text = str(e) if isinstance(e, SprachmodellFehler) else f"{type(e).__name__}: {e}"
        queue.fail_job(db, job, "summary_error", text[:500], retryable=getattr(e, "erneut", True))
        db.commit()
    return True


def arbeitsprozess_starten(session_factory) -> threading.Event | None:
    intervall = get_settings().summarizer_interval_seconds
    if intervall <= 0:
        return None
    stop = threading.Event()

    def schleife():
        while not stop.is_set():
            try:
                with session_factory() as db:
                    while einen_auftrag(db) and not stop.is_set():
                        pass
            except Exception:
                log.exception("Fehler im Zusammenfassungs-Prozess")
            stop.wait(intervall)

    threading.Thread(target=schleife, name="zusammenfassung", daemon=True).start()
    return stop


def schritt_setzen(db: Session, session_id: str, name: str) -> None:
    """Zwischenschritt der Zusammenfassung für die App (ProcessingStatus.message = summarizing.<name>). Eigene kurze
    Datenbanksitzung, damit der laufende Auftrag nichts halb Fertiges festschreibt."""
    from sqlalchemy.orm import Session as DbSession

    try:
        with DbSession(bind=db.get_bind()) as eigene:
            s = eigene.get(GameSession, session_id)
            if s is not None and s.state == "summarizing":
                s.status_message = f"summarizing.{name}"
                eigene.commit()
    except Exception:  # noqa: BLE001 – Anzeige ist Beiwerk
        log.debug("Zwischenschritt nicht gespeichert", exc_info=True)


def erwaehnt(db: Session, e: Entry, s: GameSession, notiz: str) -> None:
    """Erwähnung eines Eintrags in einer Session festhalten (höchstens einmal pro Session)."""
    if not any(m.session_id == s.id for m in e.mentions):
        e.mentions.append(EntryMention(entry_id=e.id, session_id=s.id, note=notiz[:500]))
