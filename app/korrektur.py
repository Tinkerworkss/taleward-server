"""Kapitel per Hinweis korrigieren (Schnittstelle 0.4.15): POST /sessions/{id}/recap/revision und …/decision.

Die SL schreibt in eigenen Worten, was nicht stimmt oder fehlt. Ein Auftrag (Art „revise“) lässt das Sprachmodell nur
die betroffenen Absätze neu schreiben – über die Zentrale (Cloud-API, Testmodus) oder einen Worker mit lokalem Modell.
Das Ergebnis ist ein Entwurf in Recap.revision; am Recap ändert sich erst mit der Entscheidung der SL etwas.

Verbindlich:
- Hinweise und Entwurf sind nur für die SL: nie für Spieler, nie in Vorschlägen oder anderen Kapiteln, nie im Umzug
  (die Spalte wird nicht exportiert). Nach der Entscheidung sind sie weg.
- Alle Absätze ohne Änderung bleiben zeichengleich, die Zahl der Absätze ändert sich nie.
- Cloud nur mit allowCloudSummary der Kampagne.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import errors
from app.db import utcnow
from app.models import Campaign, GameSession, Job, Recap, UsageLog

log = logging.getLogger("korrektur")

JE_TAG = 10  # Aufträge je Kapitel in 24 Stunden (409 revision_limit)
HINWEIS_ZEICHEN = 2000
AUSZUG_FENSTER_S = 180  # Abschriftzeilen so weit um die Belegstellen eines Absatzes
AUSZUG_ZEICHEN = 40_000
FAEHIGKEIT = "llm_revise"  # nur Worker, die den Auftrag kennen (ab Server 0.4.82), holen ihn ab

MELDUNG = {
    "cloud": ("Für diese Kampagne ist die Cloud nicht freigegeben, und es gibt kein lokales Modell. Gib die Cloud in "
              "den Einstellungen der Kampagne frei oder korrigiere den Text selbst.",
              "The cloud isn't enabled for this campaign and there is no local model. Enable the cloud in the campaign "
              "settings or edit the text yourself."),
    "aus": ("Auf diesem Server ist gerade kein Sprachmodell eingerichtet. Korrigiere den Text selbst.",
            "No language model is set up on this server right now. Please edit the text yourself."),
    "fehler": ("Das hat nicht geklappt. Versuch es gleich noch einmal oder korrigiere den Text selbst.",
               "That didn't work. Try again in a moment or edit the text yourself."),
}


def _z(t) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _normal(text: str) -> str:
    """Zum Vergleich von baseText: Zeilenenden und Leerraum an den Rändern zählen nicht."""
    return (text or "").replace("\r\n", "\n").strip()


def lesen(r: Recap | None) -> dict | None:
    """Gespeicherter Entwurf (mit Hinweis und Auftrag) oder None."""
    if r is None or not r.revision:
        return None
    try:
        d = json.loads(r.revision)
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("state") in ("running", "ready", "failed") else None


def fuer_app(r: Recap, lang: str = "de") -> dict | None:
    """Recap.revision, wie die App es bekommt (ohne Hinweistext und Auftrag); message in der Sprache der App."""
    d = lesen(r)
    if d is None:
        return None
    meldung = None
    if d["state"] == "failed":
        meldung = MELDUNG.get(d.get("grund") or "fehler", MELDUNG["fehler"])[1 if lang == "en" else 0]
    return {"state": d["state"], "message": meldung, "createdAt": d.get("createdAt"),
            "changes": d.get("changes") or [], "notes": d.get("notes") or []}


def _schreiben(r: Recap, d: dict | None) -> None:
    r.revision = json.dumps(d, ensure_ascii=False) if d else None


def verwerfen(db: Session, r: Recap | None) -> None:
    """Offenen Entwurf verwerfen (Bearbeiten von Hand, Neu schreiben). Ein laufender Auftrag wird abgebrochen; sein
    Ergebnis wird beim Eintreffen nicht mehr übernommen."""
    d = lesen(r)
    if d is None:
        return
    job = db.get(Job, d.get("jobId") or "")
    if job is not None and job.state in ("queued", "leased"):
        job.state, job.finished_at, job.lease_expires_at = "failed", utcnow(), None
        job.error_code, job.error_message = "revision_discarded", "Entwurf verworfen"
    _schreiben(r, None)


def starten(db: Session, s: GameSession, r: Recap, note: str, base_text: str) -> None:
    """POST …/recap/revision. Prüft Zustand, Grenze und baseText und reiht einen Auftrag ein – oder legt gleich einen
    fehlgeschlagenen Entwurf an, wenn es kein passendes Sprachmodell gibt (dann sagt `message`, was zu tun ist)."""
    from app.einstellungen import llm_konfig

    if s.state != "awaiting_review":
        raise errors.conflict("wrong_state", "wrong_state.revision")
    note = (note or "").strip()
    if not note or len(note) > HINWEIS_ZEICHEN:
        raise errors.bad_request("invalid_input", "invalid_input.revision_note")
    alt = lesen(r)
    if alt is not None and alt["state"] == "running":
        raise errors.conflict("revision_running")
    if _normal(base_text) != _normal(r.text):
        raise errors.conflict("recap_changed")
    seit = utcnow() - timedelta(hours=24)
    anzahl = db.scalar(select(func.count()).select_from(Job).where(
        Job.session_id == s.id, Job.type == "revise", Job.created_at >= seit)) or 0
    if anzahl >= JE_TAG:
        raise errors.conflict("revision_limit")
    verwerfen(db, r)  # ein neuer Auftrag ersetzt einen offenen Entwurf
    d = {"state": "running", "createdAt": _z(utcnow()), "changes": [], "notes": [], "note": note}
    k = llm_konfig(db)
    c = db.get(Campaign, s.campaign_id)
    grund = None
    if k.art == "aus" or (k.art == "api" and not k.api_key):
        grund = "aus"
    elif k.art == "api" and not (c and c.allow_cloud_summary):
        grund = "cloud"
    job = Job(type="revise", session_id=s.id, required_capability=FAEHIGKEIT, engine="local")
    db.add(job)
    db.flush()
    if grund:  # nichts einreihen, was nie abgeholt würde – zählt aber zur Grenze wie ein Versuch
        job.state, job.finished_at, job.error_code = "failed", utcnow(), f"revision_{grund}"
        d.update(state="failed", grund=grund)
    d["jobId"] = job.id
    _schreiben(r, d)


def entscheiden(db: Session, s: GameSession, r: Recap, annehmen: bool) -> None:
    """POST …/recap/revision/decision – alles oder nichts."""
    from app import pruefteil
    from app.sprachmodell import absaetze

    if s.state != "awaiting_review":
        raise errors.conflict("wrong_state", "wrong_state.revision")
    d = lesen(r)
    if d is None:
        raise errors.conflict("no_revision")
    if d["state"] == "running":
        raise errors.conflict("revision_running")
    if annehmen:
        if d["state"] != "ready":
            raise errors.conflict("no_revision")
        teile = absaetze(r.text)
        aenderungen = d.get("changes") or []
        for c in aenderungen:
            i = c.get("index")
            if not isinstance(i, int) or not (0 <= i < len(teile)) or teile[i] != c.get("before"):
                raise errors.conflict("recap_changed")
        if aenderungen:
            for c in aenderungen:
                teile[c["index"]] = c["after"]
            r.text = "\n\n".join(teile)
            r.edited_at = utcnow()
            pruefung = pruefteil.lesen(r.review)
            geaendert = {c["index"] for c in aenderungen}
            for p in pruefung.get("paragraphs") or []:
                if p.get("index") in geaendert:
                    p.update(verdict="unchecked", note=None, evidence=[])
            if pruefung.get("paragraphs"):
                bericht = {"total": len(pruefung["paragraphs"]), "supported": 0, "partial": 0, "unsupported": 0,
                           "contradicted": 0, "offGame": 0}
                for p in pruefung["paragraphs"]:
                    if p.get("verdict") in pruefteil.BERICHT:
                        bericht[pruefteil.BERICHT[p["verdict"]]] += 1
                pruefung["report"] = bericht
                r.review = pruefteil.als_json(pruefung)
    _schreiben(r, None)


# ---------------------------------------------------------------- Eingabe und Ergebnis
def _auszuege(ein: dict, r: Recap) -> str:
    """Abschriftzeilen rund um die Belegstellen der Absätze – Orientierung für das Modell, nicht die ganze Abschrift."""
    from app import pruefteil
    from app.sprachmodell import _zeit_vorn, transkript_zeilen

    zeiten = sorted({b["start"] for p in pruefteil.lesen(r.review).get("paragraphs") or []
                     for b in (p.get("evidence") or [])[:3] if isinstance(b.get("start"), (int, float))})
    if not zeiten:
        return ""
    aus = []
    for z in transkript_zeilen(ein.get("transkript") or []):
        t = _zeit_vorn(z)
        if t is not None and any(abs(t - zt) <= AUSZUG_FENSTER_S for zt in zeiten):
            aus.append(z)
    return "\n".join(aus)[:AUSZUG_ZEICHEN]


def eingabe(db: Session, s: GameSession) -> dict:
    """Was das Modell bekommt: Kopf der Runde (ohne Geheimes), Kapiteltext, Hinweis, Abschrift-Auszüge."""
    from app.zusammenfassung import eingabe_bauen, recap_eingabe

    r = db.get(Recap, s.id)
    d = lesen(r) or {}
    ein = recap_eingabe(eingabe_bauen(db, s))
    auszuege = _auszuege(ein, r)
    ein.pop("transkript", None)
    return {"recap": ein, "text": r.text, "note": d.get("note") or "", "auszuege": auszuege}


def ausfuehren(ablauf, z: dict) -> dict:
    """Einen Korrekturdurchgang rechnen (Zentrale oder Worker). Liefert {changes, notes} – noch nicht gespeichert."""
    from app.sprachmodell import absaetze

    teile = absaetze(z["text"])
    text, bericht = ablauf.korrigieren(z["recap"], z["text"], {}, z["note"], z.get("auszuege") or "",
                                        grundlage="Ausschnitte aus der Abschrift")
    neu = absaetze(text) if len(absaetze(text)) == len(teile) else teile
    notes = [{"text": h["text"], "applied": bool(h["umgesetzt"]),
              "indexes": [a["absatz"] - 1 for a in h.get("absaetze") or []]} for h in bericht.get("pruefliste") or []]
    zugeordnet = {i for n in notes for i in n["indexes"]}
    # Nennt das Modell zu einem geänderten Absatz keinen Hinweis, obwohl es andere zuordnet, war die Änderung nicht
    # verlangt – sie fällt weg. Ordnet es gar nichts zu, bleiben die Änderungen (die SL sieht sie ohnehin vorher).
    changes = [{"index": i, "before": teile[i], "after": neu[i]} for i in range(len(teile))
               if neu[i] != teile[i] and (not zugeordnet or i in zugeordnet)]
    return {"changes": changes, "notes": notes}


def attrappe(z: dict) -> dict:
    """Testmodus: hängt an den ersten Absatz eine Markierung, jeder Hinweis gilt als umgesetzt."""
    from app.sprachmodell import absaetze, hinweis_liste

    teile = absaetze(z["text"])
    if not teile:
        return {"changes": [], "notes": []}
    liste = hinweis_liste(z["note"]) or [z["note"].strip()]
    return {"changes": [{"index": 0, "before": teile[0], "after": teile[0] + " (Testmodus: korrigiert)"}],
            "notes": [{"text": h, "applied": True, "indexes": [0]} for h in liste]}


def ergebnis_speichern(db: Session, job: Job, erg: dict, engine: str, worker_id: str | None, modell: str | None,
                       tokens_in: int = 0, tokens_out: int = 0, kosten_cent: int | None = None,
                       rechenzeit: float = 0.0) -> None:
    """Ergebnis prüfen und als Entwurf ablegen – nur, wenn der Entwurf noch auf diesen Auftrag wartet und der Recap
    sich nicht verändert hat. Absätze bleiben Absätze (keine Leerzeile), unbekannte Absätze fallen weg."""
    from app.sprachmodell import absaetze

    job.state, job.finished_at, job.progress, job.lease_expires_at = "done", utcnow(), 1.0, None
    s = db.get(GameSession, job.session_id)
    r = db.get(Recap, job.session_id)
    d = lesen(r)
    if s is None or d is None or d.get("jobId") != job.id or d["state"] != "running":
        return  # inzwischen verworfen oder ersetzt
    db.add(UsageLog(campaign_id=s.campaign_id, session_id=s.id, kind="revision", engine=engine, model=modell,
                    worker_id=worker_id, compute_seconds=rechenzeit, tokens_in=tokens_in, tokens_out=tokens_out,
                    cost_cents=kosten_cent))
    teile = absaetze(r.text)
    changes, gesehen = [], set()
    for c in erg.get("changes") or []:
        i, after = c.get("index"), " ".join(str(c.get("after") or "").split())
        if not isinstance(i, int) or not (0 <= i < len(teile)) or i in gesehen or not after or after == teile[i]:
            continue
        if c.get("before") is not None and c["before"] != teile[i]:
            continue
        gesehen.add(i)
        changes.append({"index": i, "before": teile[i], "after": after[:20000]})
    changes.sort(key=lambda c: c["index"])
    notes = []
    for n in erg.get("notes") or []:
        text = str(n.get("text") or "").strip()[:HINWEIS_ZEICHEN]
        if not text:
            continue
        idx = sorted({i for i in n.get("indexes") or [] if isinstance(i, int) and i in gesehen})
        notes.append({"text": text, "applied": bool(idx), "indexes": idx})
    d.update(state="ready", changes=changes, notes=notes)
    _schreiben(r, d)


def fehlgeschlagen(db: Session, job: Job) -> None:
    """Auftrag endgültig gescheitert: Entwurf auf failed (die App zeigt eine Meldung für Menschen, keine technische).
    Die Runde bleibt, wie sie ist."""
    r = db.get(Recap, job.session_id or "")
    d = lesen(r)
    if d is None or d.get("jobId") != job.id or d["state"] != "running":
        return
    d.update(state="failed", grund="fehler")
    _schreiben(r, d)


def zentrale_ausfuehren(db: Session, job: Job, worker) -> None:
    """Auftrag auf der Zentrale (Cloud-API oder Testmodus)."""
    from app import queue
    from app.einstellungen import llm_konfig

    s = db.get(GameSession, job.session_id or "")
    k = llm_konfig(db)
    t0 = time.monotonic()
    try:
        z = eingabe(db, s)
        if k.art == "attrappe":
            erg, modell, tin, tout, kosten = attrappe(z), "testmodus", 0, 0, 0
        else:
            from app.sprachmodell import Ablauf
            from app.zusammenfassung import api_klient

            klient = api_klient(k)
            ablauf = Ablauf(klient)
            erg = ausfuehren(ablauf, z)
            modell, tin, tout = klient.modell, ablauf.zaehler.tokens_in, ablauf.zaehler.tokens_out
            kosten = ablauf.zaehler.kosten_cent()
        db.refresh(job)
        if not queue.holds_lease(job, worker):
            db.rollback()
            return
        ergebnis_speichern(db, job, erg, "external" if k.art == "api" else "local", worker.id, modell, tin, tout,
                           kosten, time.monotonic() - t0)
        db.commit()
    except Exception as e:  # noqa: BLE001 – Modell nicht erreichbar, unerwartete Antwort …
        db.rollback()
        log.exception("Korrektur für Session %s fehlgeschlagen", job.session_id)
        job = db.get(Job, job.id)
        from app.sprachmodell import SprachmodellFehler

        text = str(e) if isinstance(e, SprachmodellFehler) else f"{type(e).__name__}: {e}"
        queue.fail_job(db, job, "revision_error", text[:500], retryable=getattr(e, "erneut", True))
        db.commit()
