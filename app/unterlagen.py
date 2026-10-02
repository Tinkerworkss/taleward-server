"""SL-Unterlagen: hochladen, Text auslesen, Bibel-Vorschläge erzeugen, übernehmen.

Ablauf: Hochladen → Text auslesen (sofort, in der Zentrale) → Auftrag „document“ (Fähigkeit llm) → Sprachmodell
(Zentrale per API/Testmodus oder Worker mit Ollama, wie bei der Zusammenfassung) → Vorschläge zur Prüfung →
Übernehmen in die Bibel.

Trennung SL-/Spielerwissen (verbindlich, hier erzwungen – nicht dem Sprachmodell überlassen):
- handout: Vorschläge sind public, ohne gmNotes.
- gm: jeder Vorschlag gm_only, publicSuggested immer false.
- mixed: gm_only; publicSuggested nur mit Begründung.
- Ergänzt ein Vorschlag aus gm/mixed einen öffentlichen Eintrag, kommt alles Neue in gmNotes (detail bleibt leer,
  der öffentliche Text des Eintrags bleibt, wie er ist).
- Text aus markierten Abschnitten („[SL]“, „[GM]“, „Geheim:“, „Secret:“) im öffentlichen Teil → in gmNotes.
Unterlagen und ihre Vorschläge sieht nur die SL. Die Datei bleibt, bis die SL die Unterlage löscht.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import errors, storage
from app.config import get_settings
from app.db import utcnow
from app.models import Campaign, CampaignDocument, Entry, Job, Proposal, UsageLog

MAX_BYTES = 50 * 1024 * 1024
MAX_ZEICHEN = 1_500_000  # darüber wird abgeschnitten (≈ 450 000 Tokens)
MAX_SEITEN = 2000
MAX_DOCX_XML = 100 * 1024 * 1024
ENDUNGEN = {".pdf": "pdf", ".docx": "docx", ".txt": "text", ".md": "text", ".markdown": "text"}
MARKER = re.compile(r"^\s*(\[(SL|GM)\]|(Geheim|Secret|SL-Info|GM-Info)\s*:)", re.I)


def ordner(doc_id: str) -> Path:
    return get_settings().data_dir / "unterlagen" / doc_id


# ---------------------------------------------------------------- Text auslesen
@dataclass
class Extrakt:
    abschnitte: list[dict]  # {seite: int | None, text}
    seiten: int | None
    leere_seiten: int = 0
    abgeschnitten_bei: int | None = None  # Seite (oder Abschnitt), ab der nicht mehr ausgewertet wurde


def art_der_datei(dateiname: str, daten: bytes) -> str:
    art = ENDUNGEN.get(Path(dateiname or "").suffix.lower())
    if art == "pdf" and daten.startswith(b"%PDF"):
        return art
    if art == "docx" and daten.startswith(b"PK"):
        return art
    if art == "text" and b"\x00" not in daten[:65536]:
        return art
    raise errors.bad_request("unsupported_document")


def _begrenzen(abschnitte: list[dict]) -> tuple[list[dict], int | None]:
    summe, out = 0, []
    for a in abschnitte:
        if summe + len(a["text"]) > MAX_ZEICHEN:
            return out, a.get("seite") or len(out) + 1
        summe += len(a["text"])
        out.append(a)
    return out, None


def _pdf(daten: bytes) -> Extrakt:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        leser = PdfReader(io.BytesIO(daten))
        if leser.is_encrypted and not leser.decrypt(""):
            raise errors.bad_request("unsupported_document", "unsupported_document.password")
        seiten = leser.pages
        abschnitte, leer = [], 0
        for i, seite in enumerate(seiten):
            if i >= MAX_SEITEN:
                break
            try:
                text = (seite.extract_text() or "").strip()
            except Exception:  # einzelne kaputte Seite
                text = ""
            if len(text) < 3:  # nichts oder nur Seitenzahl – vermutlich eingescannt
                leer += 1
                continue
            abschnitte.append({"seite": i + 1, "text": text})
        return Extrakt(abschnitte, len(seiten), leer)
    except errors.ApiError:
        raise
    except (PdfReadError, Exception):
        raise errors.bad_request("unsupported_document") from None


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx(daten: bytes) -> Extrakt:
    try:
        with zipfile.ZipFile(io.BytesIO(daten)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML:
                raise errors.bad_request("unsupported_document")
            wurzel = ElementTree.fromstring(z.read(info))
    except errors.ApiError:
        raise
    except Exception:
        raise errors.bad_request("unsupported_document") from None
    seite, abschnitte, aktuell = 1, [], []
    umbrueche = False
    for p in wurzel.iter(f"{W}p"):
        teile = []
        for el in p.iter():
            if el.tag == f"{W}t" and el.text:
                teile.append(el.text)
            elif el.tag == f"{W}tab":
                teile.append("\t")
            elif (el.tag == f"{W}br" and el.get(f"{W}type") == "page") or el.tag == f"{W}lastRenderedPageBreak":
                if aktuell:
                    abschnitte.append({"seite": seite, "text": "\n".join(aktuell)})
                    aktuell = []
                seite += 1
                umbrueche = True
        zeile = "".join(teile).strip()
        if zeile:
            aktuell.append(zeile)
    if aktuell:
        abschnitte.append({"seite": seite, "text": "\n".join(aktuell)})
    if not umbrueche:  # keine Seiteninformation im Dokument
        for a in abschnitte:
            a["seite"] = None
    return Extrakt(abschnitte, seite if umbrueche else None)


def _text(daten: bytes) -> Extrakt:
    try:
        text = daten.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = daten.decode("cp1252", errors="replace")
    # Absätze zu Abschnitten von etwa 4000 Zeichen bündeln
    abschnitte, puffer = [], []
    for absatz in re.split(r"\n\s*\n", text):
        absatz = absatz.strip()
        if not absatz:
            continue
        puffer.append(absatz)
        if sum(len(x) for x in puffer) > 4000:
            abschnitte.append({"seite": None, "text": "\n\n".join(puffer)})
            puffer = []
    if puffer:
        abschnitte.append({"seite": None, "text": "\n\n".join(puffer)})
    return Extrakt(abschnitte, None)


def auslesen(art: str, daten: bytes) -> Extrakt:
    x = {"pdf": _pdf, "docx": _docx, "text": _text}[art](daten)
    x.abschnitte, x.abgeschnitten_bei = _begrenzen(x.abschnitte)
    return x


def text_laden(doc_id: str) -> list[dict]:
    try:
        return json.loads((ordner(doc_id) / "text.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def geheime_abschnitte(abschnitte: list[dict]) -> list[str]:
    """Absätze, die ausdrücklich als geheim markiert sind (für die Prüfung der Vorschläge)."""
    out = []
    for a in abschnitte:
        for absatz in re.split(r"\n\s*\n|\n(?=\s*(?:\[(?:SL|GM)\]|Geheim:|Secret:))", a["text"]):
            if MARKER.match(absatz):
                out.append(absatz)
    return out


# ---------------------------------------------------------------- Hochladen und Auftrag
def hochladen(db: Session, campaign_id: str, member_id: str, dateiname: str, daten: bytes, kind: str,
              titel: str | None) -> CampaignDocument:
    from app.queue import status_message

    if kind not in ("handout", "gm", "mixed", "character_sheet"):
        raise errors.bad_request("validation_error", "validation_error.value", field="kind")
    if len(daten) > MAX_BYTES:
        raise errors.ApiError(413, "document_too_large")
    art = art_der_datei(dateiname, daten)
    if kind == "character_sheet":
        # 0.4.7: Charakterbogen – nur gespeichert, nie ausgelesen oder ausgewertet, fließt nie in Recaps/Vorschläge
        name = Path(dateiname).name[:300] or "charakterbogen"
        doc = CampaignDocument(campaign_id=campaign_id, title=(titel or "").strip()[:300] or Path(name).stem[:300],
                               file_name=name, kind=kind, size_bytes=len(daten), page_count=None,
                               uploaded_by_member_id=member_id, state="done")
        db.add(doc)
        db.flush()
        storage.write_atomic(ordner(doc.id) / f"datei{Path(name).suffix.lower()}", daten)
        return doc
    x = auslesen(art, daten)
    name = Path(dateiname).name[:300] or "unterlage"
    doc = CampaignDocument(campaign_id=campaign_id, title=(titel or "").strip()[:300] or Path(name).stem[:300],
                           file_name=name, kind=kind, size_bytes=len(daten), page_count=x.seiten,
                           uploaded_by_member_id=member_id, state="queued")
    db.add(doc)
    db.flush()
    d = ordner(doc.id)
    storage.write_atomic(d / f"datei{Path(name).suffix.lower()}", daten)
    storage.write_atomic(d / "text.json", json.dumps(x.abschnitte, ensure_ascii=False).encode())
    if not x.abschnitte:
        doc.state, doc.message = "failed", status_message("doc.no_text")
        return doc
    if x.abgeschnitten_bei:
        doc.message = status_message("doc.truncated", seite=x.abgeschnitten_bei)
    elif x.leere_seiten:
        doc.message = status_message("doc.scanned_pages", n=x.leere_seiten, gesamt=x.seiten)
    db.add(Job(type="document", document_id=doc.id, required_capability="llm", engine="local"))
    return doc


def loeschen(db: Session, doc: CampaignDocument) -> None:
    """Unterlage samt Datei, Text, Vorschlägen und Aufträgen löschen. Übernommene Einträge bleiben."""
    db.execute(delete(Proposal).where(Proposal.document_id == doc.id))
    db.execute(delete(Job).where(Job.document_id == doc.id))
    shutil.rmtree(ordner(doc.id), ignore_errors=True)
    db.delete(doc)


def neu_starten(db: Session, doc: CampaignDocument) -> None:
    if doc.state != "failed":
        raise errors.conflict("invalid_state", "invalid_state.document")
    if not text_laden(doc.id):
        raise errors.conflict("invalid_state", "invalid_state.document_no_text")
    db.execute(delete(Proposal).where(Proposal.document_id == doc.id))
    doc.state, doc.progress, doc.message = "queued", None, None
    db.add(Job(type="document", document_id=doc.id, required_capability="llm", engine="local"))


# ---------------------------------------------------------------- Eingabe für das Sprachmodell
def eingabe(db: Session, doc: CampaignDocument) -> dict:
    """Unterlage + ganze Bibel inkl. gmNotes (Schnittstelle: „mit der vorhandenen Bibel (inkl. gmNotes) als
    Kontext“). Vorschläge und Unterlagen sieht nur die SL."""
    c = db.get(Campaign, doc.campaign_id)
    bibel = [{"id": e.id, "typ": e.type, "name": e.name, "zusammenfassung": e.summary or "",
              "gm_notes": e.gm_notes or None,
              # teilweise verborgen = nicht allgemein bekannt („Wer weiß was“) → im Aufruf als geheim
              "sichtbarkeit": "teilweise" if e.visibility == "public" and e.hidden_member_ids else e.visibility}
             for e in db.scalars(select(Entry).where(Entry.campaign_id == c.id).order_by(Entry.name))]
    return {"sprache": c.language, "kampagne": c.title, "system": c.system, "system_name": c.system_name,
            "welt": c.world_info, "art": doc.kind, "titel": doc.title, "abschnitte": text_laden(doc.id),
            "bibel": bibel}


def attrappe(ein: dict) -> dict:
    """Testmodus ohne Sprachmodell: zeigt jede Art Vorschlag einmal."""
    en = ein["sprache"] == "en"
    erste = ein["abschnitte"][0] if ein["abschnitte"] else {"seite": None, "text": ""}
    beleg = {"quote": erste["text"][:120]} | ({"page": erste["seite"]} if erste.get("seite") else {})
    v = [{"entryType": "npc", "action": "create", "targetEntryId": None,
          "title": "Placeholder NPC (test mode)" if en else "Platzhalter-NSC (Testmodus)",
          "detail": "What players would know." if en else "Was die Spieler wissen würden.",
          "gmNotes": "Secret part." if en else "Geheimer Teil.", "publicSuggested": ein["art"] == "mixed",
          "visibilityReason": ("Test mode" if en else "Testmodus") if ein["art"] == "mixed" else None,
          "confidence": 0.7, "flags": [], "evidence": [beleg]}]
    if ein["bibel"]:
        ziel = ein["bibel"][0]
        v.append({"entryType": ziel["typ"], "action": "update", "targetEntryId": ziel["id"], "title": ziel["name"],
                  "detail": "New from the document (test mode)." if en else "Neues aus der Unterlage (Testmodus).",
                  "gmNotes": None, "publicSuggested": False, "visibilityReason": None, "confidence": 0.5,
                  "flags": ["contradicts_bible"], "evidence": [beleg]})
    welt = None if ein["art"] == "gm" else ("World background (test mode)." if en else "Welt-Hintergrund (Testmodus).")
    return {"proposals": v, "worldInfoSuggestion": welt, "model": "attrappe", "tokensIn": 0, "tokensOut": 0}


# ---------------------------------------------------------------- Ergebnis speichern
def speichern(db: Session, doc: CampaignDocument, d: dict, engine: str, worker_id: str | None, rechenzeit: float,
              kosten_cent: int = 0) -> None:
    """Vorschläge anlegen – mit den festen Regeln zur Trennung von SL- und Spielerwissen."""
    from app.sprachmodell import dokument_pruefen
    from app.zusammenfassung import _geheimes_im_detail, geheime_bibeltexte

    eintraege = {e.id: e for e in db.scalars(select(Entry).where(Entry.campaign_id == doc.campaign_id))}
    # pc-Einträge (0.4.7) pflegt die App – keine Vorschläge dafür, auch nicht als neuer Eintrag gleichen Namens
    spieler = {e.name.casefold() for e in eintraege.values() if e.type == "pc"}
    vorschlaege = [v for v in dokument_pruefen(d.get("proposals") or [],
                                               {i: e.name for i, e in eintraege.items() if e.type != "pc"})
                   if v["title"].casefold() not in spieler]
    # als geheim markierte Absätze der Unterlage und – außer bei Handouts, die die SL ohnehin zeigt – was in der
    # Bibel nicht alle Spieler kennen
    markiert = geheime_abschnitte(text_laden(doc.id))
    if doc.kind != "handout":
        markiert += geheime_bibeltexte(eintraege.values())
    db.execute(delete(Proposal).where(Proposal.document_id == doc.id))
    for pos, v in enumerate(vorschlaege):
        detail, gm_notes = v["detail"], v["gmNotes"]
        oeffentlich_vorgeschlagen = v["publicSuggested"]
        grund = v["visibilityReason"]
        ziel = eintraege.get(v["targetEntryId"] or "")
        if markiert and detail and _geheimes_im_detail(detail, markiert):
            # als geheim markierter Text im öffentlichen Teil → alles in den geheimen Teil
            gm_notes, detail = "\n\n".join(t for t in (detail, gm_notes) if t), ""
            oeffentlich_vorgeschlagen = False
        if doc.kind == "handout":
            sichtbar, gm_notes, oeffentlich_vorgeschlagen = "public", None, False
        else:
            sichtbar = "gm_only"
            if doc.kind == "gm" or not grund:
                oeffentlich_vorgeschlagen = False
            if v["action"] == "update" and ziel is not None and ziel.visibility == "public" and detail:
                # Neues aus SL-Unterlagen zu öffentlichem Eintrag: nur in den geheimen Teil
                gm_notes, detail = "\n\n".join(t for t in (detail, gm_notes) if t), ""
        if v["action"] == "create" and sichtbar == "gm_only" and not detail and not gm_notes:
            continue
        db.add(Proposal(
            campaign_id=doc.campaign_id, session_id=None, document_id=doc.id, position=pos,
            entry_type=v["entryType"], action=v["action"], target_entry_id=v["targetEntryId"], title=v["title"],
            detail=detail, gm_notes=gm_notes, suggested_visibility=sichtbar, public_suggested=oeffentlich_vorgeschlagen,
            visibility_reason=grund if oeffentlich_vorgeschlagen or sichtbar == "public" else None,
            confidence=v["confidence"], flags=json.dumps(v["flags"]),
            evidence=json.dumps(v["evidence"], ensure_ascii=False),
        ))
    welt = (d.get("worldInfoSuggestion") or "").strip()[:1500] or None
    doc.world_info_suggestion = None if doc.kind == "gm" else welt
    doc.state, doc.progress = "awaiting_review", 1.0
    if doc.message and '"doc.failed"' in doc.message:
        doc.message = None
    tokens_in, tokens_out = int(d.get("tokensIn") or 0), int(d.get("tokensOut") or 0)
    db.add(UsageLog(campaign_id=doc.campaign_id, document_id=doc.id, kind="document", engine=engine,
                    model=d.get("model"), worker_id=worker_id, compute_seconds=rechenzeit, tokens_in=tokens_in,
                    tokens_out=tokens_out, cost_cents=kosten_cent))


def verarbeiter(db: Session):
    """Was die Zentrale selbst ausführt (Testmodus oder API): fn(db, doc) → (Ergebnis, engine, kosten). None, wenn
    ein Worker mit Sprachmodell zuständig ist oder nichts eingestellt ist."""
    from app.einstellungen import llm_konfig

    k = llm_konfig(db)
    if k.art == "attrappe":
        return lambda db_, doc: (attrappe(eingabe(db_, doc)), "local", 0)
    if k.art == "api" and k.api_key:
        from app.sprachmodell import DokumentAblauf
        from app.zusammenfassung import api_klient

        def ueber_api(db_, doc):
            klient = api_klient(k)
            d = DokumentAblauf(klient).ausfuehren(eingabe(db_, doc))
            return d, "external", klient.kosten_cent(d["tokensIn"], d["tokensOut"])
        return ueber_api
    return None


def auftrag_ausfuehren(db: Session, job: Job, holder) -> None:
    """In der Zentrale: einen „document“-Auftrag bearbeiten (Lease hält `holder`)."""
    from app import queue
    from app.sprachmodell import SprachmodellFehler

    doc = db.get(CampaignDocument, job.document_id)
    fn = verarbeiter(db)
    if doc is None or fn is None:
        queue.fail_job(db, job, "document_gone" if doc is None else "no_llm", "–", retryable=doc is not None)
        db.commit()
        return
    t0 = time.monotonic()
    try:
        d, engine, kosten = fn(db, doc)
        db.refresh(job)
        if not queue.holds_lease(job, holder):
            db.rollback()
            return
        speichern(db, doc, d, engine, holder.id, time.monotonic() - t0, kosten)
        job.state, job.finished_at, job.progress, job.lease_expires_at = "done", utcnow(), 1.0, None
        db.commit()
    except Exception as e:
        db.rollback()
        job = db.get(Job, job.id)
        text = str(e) if isinstance(e, SprachmodellFehler) else f"{type(e).__name__}: {e}"
        queue.fail_job(db, job, "document_error", text[:500], retryable=getattr(e, "erneut", True))
        db.commit()


# ---------------------------------------------------------------- Übernehmen
def uebernehmen(db: Session, doc: CampaignDocument, welt_uebernehmen: bool) -> None:
    from app.routers.pruefung import _kampagnen_mitglieder, _uebernehmen

    if doc.state != "awaiting_review":
        raise errors.conflict("invalid_state", "invalid_state.document_review")
    mitglieder = _kampagnen_mitglieder(db, doc.campaign_id)
    for p in db.scalars(select(Proposal).where(Proposal.document_id == doc.id).order_by(Proposal.position)):
        if p.decision == "accepted":
            _uebernehmen(db, doc.campaign_id, None, p, mitglieder)
        elif p.decision == "open":
            p.decision = "rejected"
    if welt_uebernehmen and doc.world_info_suggestion:
        c = db.get(Campaign, doc.campaign_id)
        c.world_info = "\n\n".join(t for t in ((c.world_info or "").strip(), doc.world_info_suggestion) if t)
    doc.state = "done"
