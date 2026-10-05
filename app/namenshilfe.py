"""Dynamischer Terminologie-Resolver für Transkription und Nachkorrektur.

Die globale Systemliste wird nicht pro Kampagne kopiert. Für jeden
Transkriptionsauftrag wird eine kleine, reproduzierbare Auswahl gebaut aus:
- manuellen Korrekturen,
- Namen der tatsächlich anwesenden Runde,
- aktuellen Kampagnen-/Bibel-Namen,
- kontextuell passenden Stufe-B-Begriffen,
- Shared/Hybrid-Systemkern plus Sprach-Overlay.

Shared bleibt immer aktiv. Das ist wichtig, weil deutsche Runden z. B. Chummer,
Nat 20, Critical oder Saving Throw aus englischen Regelwerken benutzen können.

Der beim Auftrag verwendete Stand wird als Snapshot an der Session gespeichert.
Seit Resolver 3 dient diese Auswahl nicht mehr als Whisper-Prompt, sondern als
reproduzierbarer Terminologiestand für Nachkorrektur und Diagnose.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import systembegriffe as begriffslisten
from app.models import Campaign, Entry, EntryMention, GameSession, Member, User

MAX_BEGRIFFE = 80
MAX_ZEICHEN = 700
MAX_PROMOVIERT = 24
MAX_ANZEIGE = 200
MAX_LAENGE = 40
RESOLVER_VERSION = "3"


@dataclass(frozen=True)
class HotwordAuswahl:
    begriffe: tuple[str, ...]
    modus: str
    sprache: str
    system: str | None
    woerterbuch_fingerprint: str | None
    quellen: tuple[tuple[str, int], ...] = ()

    @property
    def fingerprint(self) -> str:
        payload = {
            "resolver": RESOLVER_VERSION,
            "mode": self.modus,
            "language": self.sprache,
            "system": self.system,
            "dictionary": self.woerterbuch_fingerprint,
            "terms": self.begriffe,
        }
        roh = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(roh).hexdigest()

    def snapshot(self) -> dict:
        return {
            "resolverVersion": RESOLVER_VERSION,
            "mode": self.modus,
            "language": self.sprache,
            "system": self.system,
            "dictionaryFingerprint": self.woerterbuch_fingerprint,
            "terms": list(self.begriffe),
            "sourceCounts": dict(self.quellen),
            "fingerprint": self.fingerprint,
        }


def systembegriffe(system: str | None, system_name: str | None = None, sprache: str | None = None) -> tuple[str, ...]:
    """Shared Stufe A plus passendes Sprach-Overlay."""
    liste = begriffslisten.erkennen(system, system_name)
    return liste.a_fuer(sprache) if liste else ()


def _gespeichert(campaign: Campaign) -> dict:
    try:
        roh = json.loads(campaign.namenshilfe or "{}")
    except ValueError:
        roh = {}
    d = {k: [str(x) for x in roh.get(k) or [] if str(x).strip()]
         for k in ("extra", "entfernt", "ignoriert")}
    kor = roh.get("korrekturen") if isinstance(roh, dict) else {}
    d["korrekturen"] = {
        str(k).casefold(): " ".join(str(v).split())[:100]
        for k, v in (kor.items() if isinstance(kor, dict) else [])
        if str(k).strip() and str(v).strip()
    }
    return d


def _speichern(campaign: Campaign, d: dict) -> None:
    campaign.namenshilfe = json.dumps(d, ensure_ascii=False)


def _eindeutig(namen) -> list[str]:
    aus, gesehen = [], set()
    for n in namen:
        n = " ".join(str(n or "").split())[:MAX_LAENGE]
        if n and n.casefold() not in gesehen:
            gesehen.add(n.casefold())
            aus.append(n)
    return aus


def _eintraege_sortiert(db: Session, campaign: Campaign) -> list[Entry]:
    letzte = (select(EntryMention.entry_id, func.max(GameSession.number).label("nr"))
              .join(GameSession, GameSession.id == EntryMention.session_id)
              .group_by(EntryMention.entry_id).subquery())
    return [e for e, _ in db.execute(
        select(Entry, letzte.c.nr).outerjoin(letzte, letzte.c.entry_id == Entry.id)
        .where(Entry.campaign_id == campaign.id)
        .order_by(letzte.c.nr.desc().nulls_last(), Entry.updated_at.desc())
    ).all()]


def abgeleitet(db: Session, campaign: Campaign) -> list[str]:
    """Editierbare Kampagnen-Namenshilfe: Personen, Bibel und freier Systemname."""
    namen: list[str] = []
    for m, u in db.execute(select(Member, User).outerjoin(User, User.id == Member.user_id)
                           .where(Member.campaign_id == campaign.id)).all():
        namen += [m.character_name, m.character_nickname, u.display_name if u else None]
    namen += [e.name for e in _eintraege_sortiert(db, campaign)]
    namen.append(campaign.system_name)
    return _eindeutig(namen)


def _session_namen(db: Session, campaign: Campaign, session: GameSession | None) -> list[str]:
    """Anwesende zuerst; für diese Aufnahme wichtiger als alte Bibel-Namen."""
    if session is None:
        return []
    ids = [a.member_id for a in session.attendees if a.member_id]
    mitglieder: dict[str, tuple[Member, User | None]] = {}
    if ids:
        for m, u in db.execute(select(Member, User).outerjoin(User, User.id == Member.user_id)
                               .where(Member.id.in_(ids), Member.campaign_id == campaign.id)).all():
            mitglieder[m.id] = (m, u)
    namen: list[str] = []
    for a in session.attendees:
        if a.member_id and a.member_id in mitglieder:
            m, u = mitglieder[a.member_id]
            namen += [m.character_name, m.character_nickname, u.display_name if u else None]
        elif a.guest_name:
            namen.append(a.guest_name)
    return _eindeutig(namen)


def anzeige(db: Session, campaign: Campaign) -> list[str]:
    """Nur Kampagnenwissen anzeigen; globale Systemlisten bleiben zentral."""
    d = _gespeichert(campaign)
    raus = {x.casefold() for x in d["entfernt"]}
    return _eindeutig(d["extra"] + [n for n in abgeleitet(db, campaign) if n.casefold() not in raus])[:MAX_ANZEIGE]


def setzen(db: Session, campaign: Campaign, liste: list[str]) -> None:
    """Editierbare Liste ersetzen; gespeichert werden nur Abweichungen von den abgeleiteten Kampagnennamen."""
    neu = _eindeutig(liste)[:MAX_ANZEIGE]
    basis = abgeleitet(db, campaign)
    neu_klein = {n.casefold() for n in neu}
    basis_klein = {n.casefold() for n in basis}
    d = _gespeichert(campaign)
    d["extra"] = [n for n in neu if n.casefold() not in basis_klein]
    d["entfernt"] = [n for n in basis if n.casefold() not in neu_klein]
    _speichern(campaign, d)


def hinzufuegen(db: Session, campaign: Campaign, wort: str) -> None:
    """Manuelle Korrektur: höchste Priorität im nächsten dynamischen Lauf."""
    wort = " ".join(wort.split())[:MAX_LAENGE]
    if not wort:
        return
    d = _gespeichert(campaign)
    d["entfernt"] = [n for n in d["entfernt"] if n.casefold() != wort.casefold()]
    d["extra"] = _eindeutig([wort] + d["extra"])[:MAX_ANZEIGE]
    _speichern(campaign, d)


def korrektur_lernen(campaign: Campaign, gehoert: str, korrekt: str) -> None:
    """Von der SL bestätigte Schreibweise für künftige Nachkorrekturen merken."""
    gehoert = " ".join((gehoert or "").split())[:100]
    korrekt = " ".join((korrekt or "").split())[:100]
    if not gehoert or not korrekt or gehoert.casefold() == korrekt.casefold():
        return
    d = _gespeichert(campaign)
    d["korrekturen"][gehoert.casefold()] = korrekt
    # Begrenze alte Lernpaare; JSON-Reihenfolge entspricht Einfügereihenfolge.
    if len(d["korrekturen"]) > 500:
        d["korrekturen"] = dict(list(d["korrekturen"].items())[-500:])
    d["ignoriert"] = [x for x in d["ignoriert"] if x.casefold() != gehoert.casefold()]
    _speichern(campaign, d)


def korrekturen(campaign: Campaign) -> dict[str, str]:
    return dict(_gespeichert(campaign)["korrekturen"])


def ignorieren(campaign: Campaign, wort: str) -> None:
    d = _gespeichert(campaign)
    d["ignoriert"] = _eindeutig(d["ignoriert"] + [wort])[-500:]
    d["korrekturen"].pop(" ".join((wort or "").split()).casefold(), None)
    _speichern(campaign, d)


def ignoriert(campaign: Campaign) -> set[str]:
    return {x.casefold() for x in _gespeichert(campaign)["ignoriert"]}


def _kontexttexte(db: Session, campaign: Campaign, session: GameSession | None = None) -> list[str]:
    """Nur zur Relevanzwahl; geheime Texte werden nie selbst als Hotword an Whisper geschickt."""
    texte = [campaign.title, campaign.description, campaign.world_info, campaign.system_name,
             session.title if session else None]
    for e in db.scalars(select(Entry).where(Entry.campaign_id == campaign.id)):
        texte += [e.name, e.summary, e.gm_notes]
    for m in db.scalars(select(Member).where(Member.campaign_id == campaign.id)):
        texte += [m.character_name, m.character_nickname, m.character_summary, m.character_backstory]
    return [str(t) for t in texte if t]


def _packen(
    gruppen: list[tuple[str, list[str] | tuple[str, ...]]], *,
    max_begriffe: int = MAX_BEGRIFFE,
) -> tuple[list[str], tuple[tuple[str, int], ...]]:
    """Priorisierte Gruppen in das Whisper-Budget packen.

    max_begriffe ist nur für reproduzierbare Benchmarks variabel; normale
    Produktionsaufrufe bleiben beim bisherigen MAX_BEGRIFFE-Budget.
    """
    max_begriffe = max(0, min(MAX_BEGRIFFE, max_begriffe))
    ergebnis: list[str] = []
    gesehen: set[str] = set()
    laenge = 0
    zaehler: dict[str, int] = {}
    for quelle, gruppe in gruppen:
        for roh in gruppe:
            n = " ".join(str(roh or "").split())
            if len(n) > MAX_LAENGE:
                continue
            k = n.casefold()
            if not n or k in gesehen:
                continue
            zusaetzlich = len(n) + (2 if ergebnis else 0)
            if len(ergebnis) >= max_begriffe or laenge + zusaetzlich > MAX_ZEICHEN:
                continue
            ergebnis.append(n)
            gesehen.add(k)
            laenge += zusaetzlich
            zaehler[quelle] = zaehler.get(quelle, 0) + 1
    return ergebnis, tuple(zaehler.items())


def _liste(system: str | None, system_name: str | None = None):
    return (begriffslisten.lesen(system)
            or begriffslisten.erkennen(system, system_name)
            or begriffslisten.erkennen("other", system_name or system))


def statische_auswahl(
    system: str | None, system_name: str | None, sprache: str, *,
    extra=(), kontext=(), modus: str = "dynamic", hotword_limit: int | None = None,
) -> HotwordAuswahl:
    """Serverloser Resolver für reproduzierbare Null/System/Dynamik-Benchmarks.

    dynamic nutzt extra als kampagnenspezifische Begriffe und kontext nur zur
    Promotion von Stufe B. system liefert ausschließlich den Systemkern.
    """
    if modus not in {"none", "system", "dynamic"}:
        raise ValueError("hotword mode must be none, system or dynamic")
    if hotword_limit is not None and not 1 <= hotword_limit <= MAX_BEGRIFFE:
        raise ValueError(f"hotword limit must be between 1 and {MAX_BEGRIFFE}")
    liste = _liste(system, system_name)
    key = liste.schluessel if liste else None
    dfp = liste.fingerprint(sprache) if liste else None
    if modus == "none":
        gruppen: list[tuple[str, list[str] | tuple[str, ...]]] = []
    elif modus == "system":
        gruppen = [
            ("system_shared", list(liste.stufe_a) if liste else []),
            ("system_language", list(liste.a_overlay(sprache)) if liste else []),
        ]
    else:
        promoviert = list(begriffslisten.promovierte_stufe_b(
            key, None, kontext, limit=MAX_PROMOVIERT, sprache=sprache
        )) if liste else []
        gruppen = [
            ("manual", list(extra)),
            ("context_b", promoviert),
            ("system_shared", list(liste.stufe_a) if liste else []),
            ("system_language", list(liste.a_overlay(sprache)) if liste else []),
        ]
    begriffe, quellen = _packen(
        gruppen, max_begriffe=hotword_limit if hotword_limit is not None else MAX_BEGRIFFE
    )
    return HotwordAuswahl(tuple(begriffe), modus, sprache, key, dfp, quellen)


def aufloesen(
    db: Session, campaign: Campaign, session: GameSession | None = None, modus: str = "dynamic"
) -> HotwordAuswahl:
    """Aktive Kurzliste für genau diese Kampagne bzw. Session."""
    if modus not in {"none", "system", "dynamic"}:
        raise ValueError("hotword mode must be none, system or dynamic")
    liste = begriffslisten.erkennen(campaign.system, campaign.system_name)
    key = liste.schluessel if liste else None
    dfp = liste.fingerprint(campaign.language) if liste else None

    if modus == "none":
        begriffe, quellen = _packen([])
        return HotwordAuswahl(tuple(begriffe), modus, campaign.language, key, dfp, quellen)

    if modus == "system":
        begriffe, quellen = _packen([
            ("system_shared", list(liste.stufe_a) if liste else []),
            ("system_language", list(liste.a_overlay(campaign.language)) if liste else []),
        ])
        return HotwordAuswahl(tuple(begriffe), modus, campaign.language, key, dfp, quellen)

    d = _gespeichert(campaign)
    raus = {x.casefold() for x in d["entfernt"]}
    session_namen = [n for n in _session_namen(db, campaign, session) if n.casefold() not in raus]
    schon = {x.casefold() for x in session_namen}
    basis = [n for n in abgeleitet(db, campaign) if n.casefold() not in raus and n.casefold() not in schon]
    aktuell, rest = basis[:12], basis[12:]
    promoviert = [n for n in begriffslisten.promovierte_stufe_b(
        campaign.system, campaign.system_name, _kontexttexte(db, campaign, session),
        limit=MAX_PROMOVIERT, sprache=campaign.language
    ) if n.casefold() not in raus]

    begriffe, quellen = _packen([
        ("manual", d["extra"]),
        ("session", session_namen),
        ("campaign_recent", aktuell),
        ("context_b", promoviert),
        ("system_shared", list(liste.stufe_a) if liste else []),
        ("system_language", list(liste.a_overlay(campaign.language)) if liste else []),
        ("campaign_rest", rest),
    ])
    return HotwordAuswahl(tuple(begriffe), modus, campaign.language, key, dfp, quellen)


def fuer_kampagne(db: Session, campaign: Campaign, session: GameSession | None = None) -> list[str]:
    """Kompatibilitätshelfer: dynamisch aufgelöste Terminologie als Liste."""
    return list(aufloesen(db, campaign, session).begriffe)


def snapshot_lesen(roh: str | None) -> list[str] | None:
    """Terminologie aus einem bereits gespeicherten Resolver-Snapshot."""
    if not roh:
        return None
    try:
        d = json.loads(roh)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("terms"), list):
        return None
    return [str(x) for x in d["terms"] if str(x).strip()]


def hotwords_fuer_session(db: Session, campaign: Campaign, session: GameSession) -> list[str]:
    """Legacy-/Benchmark-Helfer. Produktions-ASR sendet seit Resolver 3 keine Hotwords mehr."""
    alt = snapshot_lesen(session.hotword_snapshot)
    return alt if alt is not None else fuer_kampagne(db, campaign, session)
