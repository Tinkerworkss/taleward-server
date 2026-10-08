"""Kampagnen-Umzug (Schnittstelle 0.4.8): Export als Datei taleward-kampagne/1, Import auf einem anderen Server,
offene Plätze.

Leitgedanken (verbindlich):
- Server sprechen nie miteinander. Es gibt nur die Datei, und die SL hält sie in der Hand.
- Nie in der Datei: Anzeige- und Benutzernamen, E-Mail-Adressen, Konten, Einwilligungen, Stimmprofile, Transkripte,
  Hörproben, Audio, der Prüfteil des Recaps, Vorschläge, Terminabstimmungen, Lesemarker, Nutzung, Hinweise an die SL.
  Unveröffentlichte Kapitel gehen nicht mit (ihr Entwurf beruht auf dem Transkript).
- Persönliches eines Mitglieds (Kurzbeschreibung, Hintergrund, Spitzname, Status und Fassung des Charakters, Porträt,
  eigene öffentliche Kommentare, Charakterbögen) nur mit seiner Zustimmung (Member.move_consent_at); private
  Kommentare nur, wenn beide zugestimmt haben. Ohne Zustimmung geht nur der Platz mit: Rolle, characterId,
  Charaktername.
- Die Datei enthält gm_only-Inhalte und SL-Notizen – sie geht nur an die SL.
- Auf dem Zielserver beginnen Einwilligungen bei null; die importierende Person wird einzige SL, alle Plätze der
  Datei werden offene Plätze (Ehemalige kommen mit leftAt).

Aufbau der Datei: docs/UMZUG.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import shutil
import threading
import unicodedata
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import errors, schemas, storage
from app.config import get_settings
from app.db import utcnow
from app.models import (
    Attendee, Campaign, CampaignDocument, ChapterPlan, CampaignExport, CampaignImport, Comment, DateOption, DateVote,
    Entry, EntryHidden, EntryMention, GameSession, GmNote, GmNotice, Invite, Member, Proposal, Recap, SessionSeen,
    Speaker, User,
)

log = logging.getLogger("chronik.umzug")

FORMAT = "taleward-kampagne/1"
EXPORT_STUNDEN = 24
AUFBEWAHRUNG_TAGE = 7
MAX_EINTRAEGE_ZIP = 50_000
MAX_JSON_BYTES = 200 * 1024 * 1024
IMPORT_FEHLER = ("import_format", "import_too_large", "import_unsafe", "import_version")

# Läuft im Hintergrund (Thread). Tests schalten auf „sofort“ um.
HINTERGRUND = True
_laufend: set[tuple[str, str]] = set()
_sperre = threading.Lock()


class ImportFehler(Exception):
    def __init__(self, schluessel: str):
        super().__init__(schluessel)
        self.schluessel = schluessel


# ---------------------------------------------------------------- Ablage
def wurzel() -> Path:
    return get_settings().data_dir / "umzug"


def export_pfad(export_id: str) -> Path:
    return wurzel() / "exporte" / f"{export_id}.zip"


def import_ordner(import_id: str) -> Path:
    return wurzel() / "importe" / import_id


def teil_pfad(import_id: str, index: int) -> Path:
    return import_ordner(import_id) / "teile" / f"{index:05d}.part"


def _z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    from app.services import _utc

    return _utc(dt).isoformat().replace("+00:00", "Z")


def _zeit(text: str | None) -> datetime | None:
    if not text:
        return None
    from app.services import _utc

    return _utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


# ---------------------------------------------------------------- Hintergrund
def _starten(art: str, kennung: str) -> None:
    schluessel = (art, kennung)
    with _sperre:
        if schluessel in _laufend:
            return
        _laufend.add(schluessel)

    def lauf():
        from app.db import session_factory

        try:
            with session_factory()() as db:
                (export_ausfuehren if art == "export" else import_ausfuehren)(db, kennung)
        except Exception:
            log.exception("Umzug (%s %s) fehlgeschlagen", art, kennung)
        finally:
            with _sperre:
                _laufend.discard(schluessel)

    if HINTERGRUND:
        threading.Thread(target=lauf, name=f"umzug-{art}", daemon=True).start()
    else:
        lauf()


def aufraeumen(db: Session) -> dict:
    """Wartung: abgelaufene Dateien löschen, alte Aufträge entfernen, liegen gebliebene (Neustart) fortsetzen."""
    jetzt = utcnow()
    alt = jetzt - timedelta(days=AUFBEWAHRUNG_TAGE)
    ergebnis = {"exporte_geloescht": 0, "importe_geloescht": 0, "fortgesetzt": 0}
    weiter: list[tuple[str, str]] = []
    for x in db.scalars(select(CampaignExport)):
        if x.expires_at is not None and x.expires_at <= jetzt and export_pfad(x.id).exists():
            export_pfad(x.id).unlink(missing_ok=True)
            ergebnis["exporte_geloescht"] += 1
        if x.created_at <= alt:
            export_pfad(x.id).unlink(missing_ok=True)
            db.delete(x)
        elif x.state in ("queued", "processing") and ("export", x.id) not in _laufend:
            weiter.append(("export", x.id))
    for imp in db.scalars(select(CampaignImport)):
        if imp.created_at <= alt:
            shutil.rmtree(import_ordner(imp.id), ignore_errors=True)
            db.delete(imp)
            ergebnis["importe_geloescht"] += 1
        elif imp.state == "processing" and ("import", imp.id) not in _laufend:
            weiter.append(("import", imp.id))
    db.commit()
    for art, kennung in weiter:  # nach einem Neustart liegen gebliebene Aufträge
        _starten(art, kennung)
    ergebnis["fortgesetzt"] = len(weiter)
    return ergebnis


def kampagne_entfernt(db: Session, campaign_id: str) -> None:
    """Beim Löschen einer Kampagne: gepackte Dateien mitlöschen."""
    for i in db.scalars(select(CampaignExport.id).where(CampaignExport.campaign_id == campaign_id)):
        export_pfad(i).unlink(missing_ok=True)


def konto_entfernt(db: Session, user_id: str) -> None:
    """Beim Löschen eines Kontos: hochgeladene Teile seiner Importe mitlöschen."""
    for i in db.scalars(select(CampaignImport.id).where(CampaignImport.user_id == user_id)):
        shutil.rmtree(import_ordner(i), ignore_errors=True)


# ---------------------------------------------------------------- Export
def _zugestimmt(db: Session, campaign_id: str) -> list[str]:
    return sorted(m.id for m in db.scalars(select(Member).where(Member.campaign_id == campaign_id))
                  if m.aktiv and m.move_consent_at is not None)


def download_schluessel(export_id: str) -> str:
    """Download-Schlüssel (Parameter t) – an den Export gebunden, aus dem Geheimnis des Servers abgeleitet."""
    return hmac.new(get_settings().jwt_secret.encode(), f"umzug-export:{export_id}".encode(),
                    hashlib.sha256).hexdigest()[:48]


def schluessel_passt(export_id: str, t: str | None) -> bool:
    return bool(t) and hmac.compare_digest(download_schluessel(export_id), t)


def verfuegbar(x: CampaignExport) -> bool:
    return (x.state == "ready" and x.expires_at is not None and x.expires_at > utcnow()
            and export_pfad(x.id).exists())


def export_out(x: CampaignExport) -> schemas.CampaignExportOut:
    url = None
    if verfuegbar(x):
        url = f"/campaigns/{x.campaign_id}/exports/{x.id}/file?t={download_schluessel(x.id)}"
    return schemas.CampaignExportOut(
        id=x.id, state=x.state, progress=x.progress, size_bytes=x.size_bytes if x.state == "ready" else None,
        expires_at=x.expires_at, download_url=url, consented_member_ids=json.loads(x.consented_member_ids or "[]"),
        message=x.message, created_at=x.created_at)


def export_starten(db: Session, c: Campaign, user: User) -> CampaignExport:
    laeuft = db.scalar(select(CampaignExport.id).where(CampaignExport.campaign_id == c.id,
                                                       CampaignExport.state.in_(("queued", "processing"))).limit(1))
    if laeuft is not None:
        raise errors.conflict("export_running")
    x = CampaignExport(campaign_id=c.id, requested_by_user_id=user.id, state="queued", progress=0.0,
                       consented_member_ids=json.dumps(_zugestimmt(db, c.id)))
    db.add(x)
    db.commit()
    db.refresh(x)
    return x


def export_fortsetzen(export_id: str) -> None:
    _starten("export", export_id)


def dateiname(c: Campaign, wann: datetime) -> str:
    titel = c.title.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    titel = titel.replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    titel = unicodedata.normalize("NFKD", titel).encode("ascii", "ignore").decode().lower()
    titel = re.sub(r"[^a-z0-9]+", "-", titel).strip("-")[:50].strip("-") or "kampagne"
    return f"taleward-kampagne-{titel}-{wann.strftime('%Y-%m-%d')}.zip"


def _datei_der_unterlage(doc: CampaignDocument) -> Path | None:
    from app.unterlagen import ordner

    treffer = sorted(ordner(doc.id).glob("datei*"))
    return treffer[0] if treffer else None


def packliste(db: Session, c: Campaign, zugestimmt: set[str]) -> tuple[dict, list[tuple[str, Path]]]:
    """kampagne.json und die Dateien, die in die ZIP gehören (Name in der ZIP, Pfad auf dem Server)."""
    from app import namenshilfe
    from app.bilder import cover_ordner, finden, portrait_ordner
    from app.einstellungen import angaben
    from app.routers.auth import API_VERSION

    dateien: list[tuple[str, Path]] = []
    mitglieder = sorted(c.members, key=lambda m: (m.joined_at, m.id))
    platz = {m.id: f"p{i + 1}" for i, m in enumerate(mitglieder)}
    plaetze = []
    for m in mitglieder:
        ehemalig = not m.open_seat and (m.user_id is None or m.left_at is not None)
        mit = m.id in zugestimmt and m.aktiv
        p = {"key": platz[m.id], "role": m.role, "characterName": m.character_name,
             "characterId": None if ehemalig else m.character_id, "former": ehemalig,
             "leftAt": _z(m.left_at or m.deleted_at) if ehemalig else None, "consented": mit,
             "character": None, "portrait": None, "portraitThumb": None}
        if mit:
            p["character"] = {"version": m.character_version, "status": m.character_status,
                              "nickname": m.character_nickname, "summary": m.character_summary,
                              "backstory": m.character_backstory, "system": m.character_system}
            if m.portrait_updated_at:
                for art, feld in (("full", "portrait"), ("thumb", "portraitThumb")):
                    pfad = finden(portrait_ordner(m.id), art)
                    if pfad is not None:
                        name = f"bilder/{platz[m.id]}-{art}{pfad.suffix}"
                        p[feld] = name
                        dateien.append((name, pfad))
        plaetze.append(p)

    sitzungen = list(db.scalars(select(GameSession).where(GameSession.campaign_id == c.id,
                                                          GameSession.state == "published")
                                .order_by(GameSession.number)))
    kapitel_schluessel = {s.id: f"s{i + 1}" for i, s in enumerate(sitzungen)}
    kapitel = []
    for s in sitzungen:
        r = db.get(Recap, s.id)
        notiz = db.get(GmNote, s.id)
        kommentare = []
        for k in db.scalars(select(Comment).where(Comment.session_id == s.id).order_by(Comment.created_at)):
            if k.author_member_id not in zugestimmt:
                continue
            if k.recipient_member_id is not None and k.recipient_member_id not in zugestimmt:
                continue  # privat: nur, wenn beide zugestimmt haben
            kommentare.append({"author": platz.get(k.author_member_id),
                               "recipient": platz.get(k.recipient_member_id) if k.recipient_member_id else None,
                               "text": k.text, "createdAt": _z(k.created_at), "editedAt": _z(k.edited_at)})
        kapitel.append({
            "key": kapitel_schluessel[s.id], "number": s.number, "title": s.title, "playedAt": _z(s.played_at),
            "publishedAt": _z(s.published_at),
            "attendees": [{"seat": platz[a.member_id]} if a.member_id in platz else {"guest": a.guest_name or "?"}
                          for a in s.attendees],
            "recap": None if r is None else {"title": r.title, "text": r.text,
                                             "openThreads": json.loads(r.open_threads or "[]"),
                                             "editedAt": _z(r.edited_at)},
            "gmNote": (notiz.text or None) if notiz is not None else None,
            "comments": kommentare,
        })

    eintraege, eintrag_schluessel = [], {}
    for i, e in enumerate(db.scalars(select(Entry).where(Entry.campaign_id == c.id).order_by(Entry.created_at,
                                                                                               Entry.id))):
        eintrag_schluessel[e.id] = f"e{i + 1}"
        halter = platz.get(e.holder_member_id or "")
        summary = e.summary or ""
        if e.type == "pc" and e.holder_member_id not in zugestimmt:
            summary = ""  # Kurzbeschreibung des Charakters nur mit Zustimmung
        herkunft = None
        if e.origin_character_id and e.origin_member_id in zugestimmt:
            herkunft = {"characterId": e.origin_character_id, "entryId": e.origin_entry_id,
                        "version": e.origin_version, "seat": platz.get(e.origin_member_id)}
        eintraege.append({
            "key": f"e{i + 1}", "type": e.type, "name": e.name, "summary": summary, "gmNotes": e.gm_notes,
            "status": e.status, "holderSeat": halter, "visibility": e.visibility,
            "hiddenFromSeats": sorted(platz[m] for m in e.hidden_member_ids if m in platz),
            "pcCharacterId": e.pc_character_id, "origin": herkunft,
            "createdAt": _z(e.created_at), "updatedAt": _z(e.updated_at), "publicChangedAt": _z(e.public_changed_at),
            "mentions": [{"session": kapitel_schluessel[mn.session_id], "note": mn.note}
                         for mn in e.mentions if mn.session_id in kapitel_schluessel],
        })

    unterlagen, unterlage_schluessel = [], {}
    for doc in db.scalars(select(CampaignDocument).where(CampaignDocument.campaign_id == c.id)
                          .order_by(CampaignDocument.created_at)):
        if doc.kind == "character_sheet" and doc.uploaded_by_member_id not in zugestimmt:
            continue
        pfad = _datei_der_unterlage(doc)
        if pfad is None:
            continue
        schluessel = f"d{len(unterlagen) + 1}"
        unterlage_schluessel[doc.id] = schluessel
        name = f"unterlagen/{schluessel}/datei{pfad.suffix.lower()}"
        dateien.append((name, pfad))
        unterlagen.append({"key": schluessel, "title": doc.title, "fileName": doc.file_name, "kind": doc.kind,
                           "file": name, "uploadedBySeat": platz.get(doc.uploaded_by_member_id or ""),
                           "createdAt": _z(doc.created_at)})

    # Kapitelpläne der SL (0.4.12) – Verweise als Schlüssel; was nicht mitgeht, fällt heraus
    plaene = []
    for i, pl in enumerate(db.scalars(select(ChapterPlan).where(ChapterPlan.campaign_id == c.id)
                                      .order_by(ChapterPlan.created_at, ChapterPlan.id))):
        szenen = [{"id": sz.get("id"), "title": sz.get("title"), "notes": sz.get("notes"), "state": sz.get("state"),
                   "entryKeys": [eintrag_schluessel[x] for x in sz.get("entryIds") or [] if x in eintrag_schluessel]}
                  for sz in json.loads(pl.scenes or "[]")]
        plaene.append({"key": f"k{i + 1}", "title": pl.title, "sessionNumber": pl.session_number, "state": pl.state,
                       "notes": pl.notes, "scenes": szenen, "names": json.loads(pl.names or "[]"),
                       "documentKeys": [unterlage_schluessel[x] for x in json.loads(pl.document_ids or "[]")
                                        if x in unterlage_schluessel],
                       "createdAt": _z(pl.created_at), "updatedAt": _z(pl.updated_at)})

    titelbild = None
    if c.cover_image_updated_at:
        pfad = finden(cover_ordner(c.id), "cover")
        if pfad is not None:
            titelbild = f"bilder/cover{pfad.suffix}"
            dateien.append((titelbild, pfad))
    a = angaben(db)
    daten = {
        "format": FORMAT, "apiVersion": API_VERSION, "exportedAt": _z(utcnow()),
        "source": {"name": a.server_name, "url": (a.public_url or "").rstrip("/") or None, "campaignId": c.id},
        "campaign": {"title": c.title, "description": c.description, "worldInfo": c.world_info,
                     "language": c.language, "system": c.system, "systemName": c.system_name,
                     "coverPreset": c.cover_preset, "coverImage": titelbild,
                     "hotwords": namenshilfe._gespeichert(c), "nextSessionAt": _z(c.next_session_at),
                     "createdAt": _z(c.created_at)},
        "seats": plaetze, "sessions": kapitel, "entries": eintraege, "documents": unterlagen, "plans": plaene,
    }
    return daten, dateien


def export_ausfuehren(db: Session, export_id: str) -> None:
    x = db.get(CampaignExport, export_id)
    if x is None or x.state not in ("queued", "processing"):
        return
    x.state, x.progress, x.message = "processing", 0.0, None
    db.commit()
    ziel = export_pfad(x.id)
    tmp = ziel.with_suffix(".tmp")
    try:
        c = db.get(Campaign, x.campaign_id)
        # Stand beim Start – wer inzwischen widerrufen hat, ist nicht mehr dabei
        zugestimmt = set(json.loads(x.consented_member_ids or "[]")) & set(_zugestimmt(db, c.id))
        x.consented_member_ids = json.dumps(sorted(zugestimmt))
        daten, dateien = packliste(db, c, zugestimmt)
        ziel.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("kampagne.json", json.dumps(daten, ensure_ascii=False, indent=1))
            for i, (name, pfad) in enumerate(dateien):
                schon_gepackt = pfad.suffix.lower() in (".jpg", ".jpeg", ".webp", ".png", ".pdf", ".docx")
                z.write(pfad, name, compress_type=zipfile.ZIP_STORED if schon_gepackt else zipfile.ZIP_DEFLATED)
                if i % 20 == 19:
                    x.progress = round(0.05 + 0.9 * (i + 1) / len(dateien), 3)
                    db.commit()
        tmp.replace(ziel)
        jetzt = utcnow()
        x.state, x.progress, x.size_bytes = "ready", 1.0, ziel.stat().st_size
        x.file_name = dateiname(c, jetzt)
        x.expires_at = (jetzt + timedelta(hours=EXPORT_STUNDEN)).replace(microsecond=0)
        db.commit()
    except Exception as e:
        log.exception("Export %s fehlgeschlagen", export_id)
        db.rollback()
        tmp.unlink(missing_ok=True)
        x = db.get(CampaignExport, export_id)
        if x is not None:
            x.state, x.progress = "failed", None
            x.message = errors.ApiError(0, "internal_error").message("de") + f" ({type(e).__name__})"
            db.commit()


# ---------------------------------------------------------------- Import: hochladen
def import_out(imp: CampaignImport) -> schemas.ImportStatusOut:
    daten = dict(id=imp.id, state=imp.state, progress=imp.progress, campaign_id=imp.campaign_id,
                 open_seats=imp.open_seats, message=imp.message)
    if imp.state == "uploading":
        fehlend = fehlende_teile(imp)
        daten["missing_chunks"] = fehlend
        daten["progress"] = round((imp.chunk_count - len(fehlend)) / imp.chunk_count, 3) if imp.chunk_count else 0.0
    return schemas.ImportStatusOut(**daten)


def fehlende_teile(imp: CampaignImport) -> list[int]:
    return [i for i in range(imp.chunk_count) if not teil_pfad(imp.id, i).exists()]


def import_beginnen(db: Session, user: User, body: schemas.ImportStartIn) -> CampaignImport:
    grenze = get_settings().import_max_bytes
    if body.size_bytes > grenze:
        raise errors.ApiError(413, "import_too_large", details={"maxBytes": grenze}, mb=grenze // (1024 * 1024))
    # Wer neu anfängt, braucht den alten, nicht abgeschlossenen Upload nicht mehr
    for alt in db.scalars(select(CampaignImport).where(CampaignImport.user_id == user.id,
                                                       CampaignImport.state == "uploading")):
        shutil.rmtree(import_ordner(alt.id), ignore_errors=True)
        db.delete(alt)
    teil = get_settings().chunk_size_bytes
    imp = CampaignImport(user_id=user.id, file_name=Path(body.file_name).name[:300] or "kampagne.zip",
                         size_bytes=body.size_bytes, chunk_size=teil, chunk_count=math.ceil(body.size_bytes / teil),
                         state="uploading")
    db.add(imp)
    db.commit()
    db.refresh(imp)
    return imp


def laden(db: Session, import_id: str, user: User) -> CampaignImport:
    imp = db.get(CampaignImport, import_id)
    if imp is None or imp.user_id != user.id:
        raise errors.not_found("import")
    return imp


def teil_speichern(imp: CampaignImport, index: int, daten: bytes, pruefsumme: str | None) -> None:
    if imp.state != "uploading":
        raise errors.conflict("upload_closed")
    if index < 0 or index >= imp.chunk_count:
        raise errors.bad_request("chunk_index_invalid", index=index)
    if len(daten) > imp.chunk_size:
        raise errors.ApiError(413, "payload_too_large")
    erwartet = imp.chunk_size if index < imp.chunk_count - 1 else imp.size_bytes - imp.chunk_size * (imp.chunk_count - 1)
    if len(daten) != erwartet:
        raise errors.bad_request("chunk_size_mismatch", index=index, got=len(daten), expected=erwartet)
    if pruefsumme and hashlib.sha256(daten).hexdigest() != pruefsumme.strip().lower():
        raise errors.bad_request("chunk_checksum_mismatch", index=index)
    storage.write_atomic(teil_pfad(imp.id, index), daten)


def import_abschliessen(db: Session, imp: CampaignImport) -> None:
    if imp.state != "uploading":
        return  # doppelt abgeschickt – harmlos
    fehlend = fehlende_teile(imp)
    if fehlend:
        raise errors.ApiError(409, "chunks_missing", details={"missingChunks": fehlend}, count=len(fehlend))
    imp.state, imp.progress = "processing", 0.0
    db.commit()


def import_fortsetzen(import_id: str) -> None:
    _starten("import", import_id)


# ---------------------------------------------------------------- Import: Datei lesen
class _Charakter(schemas.ApiModel):
    version: int | None = Field(default=None, ge=1)
    status: schemas.CharacterStatus | None = None
    nickname: str | None = Field(default=None, max_length=64)
    summary: str | None = Field(default=None, max_length=20000)
    backstory: str | None = Field(default=None, max_length=50000)
    system: str | None = Field(default=None, max_length=64)


class _Platz(schemas.ApiModel):
    key: str = Field(min_length=1, max_length=40)
    role: schemas.Role
    character_name: str | None = Field(default=None, max_length=200)
    character_id: str | None = Field(default=None, pattern=schemas.UUID_MUSTER)
    former: bool = False
    left_at: datetime | None = None
    consented: bool = False
    character: _Charakter | None = None
    portrait: str | None = None
    portrait_thumb: str | None = None


class _Anwesend(schemas.ApiModel):
    seat: str | None = None
    guest: str | None = Field(default=None, max_length=200)


class _Recap(schemas.ApiModel):
    title: str = Field(max_length=300)
    text: str = Field(max_length=500_000)
    open_threads: list[str] = Field(default=[], max_length=200)
    edited_at: datetime | None = None


class _Kommentar(schemas.ApiModel):
    author: str
    recipient: str | None = None
    text: str = Field(min_length=1, max_length=20000)
    created_at: datetime
    edited_at: datetime | None = None


class _Kapitel(schemas.ApiModel):
    key: str = Field(min_length=1, max_length=40)
    number: int = Field(ge=1, le=100000)
    title: str | None = Field(default=None, max_length=300)
    played_at: datetime
    published_at: datetime | None = None
    attendees: list[_Anwesend] = Field(default=[], max_length=500)
    recap: _Recap | None = None
    gm_note: str | None = Field(default=None, max_length=500_000)
    comments: list[_Kommentar] = Field(default=[], max_length=20000)


class _Erwaehnung(schemas.ApiModel):
    session: str
    note: str = Field(default="", max_length=20000)


class _Herkunft(schemas.ApiModel):
    character_id: str = Field(pattern=schemas.UUID_MUSTER)
    entry_id: str | None = Field(default=None, max_length=36)
    version: int | None = None
    seat: str | None = None


class _Eintrag(schemas.ApiModel):
    key: str = Field(min_length=1, max_length=40)
    type: schemas.EntryType
    name: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=200_000)
    gm_notes: str | None = Field(default=None, max_length=200_000)
    status: Literal["active", "done"] | None = None
    holder_seat: str | None = None
    visibility: schemas.Visibility = "gm_only"
    hidden_from_seats: list[str] = Field(default=[], max_length=1000)
    pc_character_id: str | None = Field(default=None, pattern=schemas.UUID_MUSTER)
    origin: _Herkunft | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    public_changed_at: datetime | None = None
    mentions: list[_Erwaehnung] = Field(default=[], max_length=10000)


class _Unterlage(schemas.ApiModel):
    key: str = Field(min_length=1, max_length=40)
    title: str = Field(max_length=300)
    file_name: str = Field(max_length=300)
    kind: Literal["handout", "gm", "mixed", "character_sheet"]
    file: str
    uploaded_by_seat: str | None = None
    created_at: datetime | None = None


class _Szene(schemas.ApiModel):
    id: str = Field(pattern=schemas.UUID_MUSTER)
    title: str = Field(min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=4000)
    state: Literal["open", "played", "skipped"] = "open"
    entry_keys: list[str] = Field(default=[], max_length=30)


class _Plan(schemas.ApiModel):
    key: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=120)
    session_number: int | None = Field(default=None, ge=1)
    state: Literal["draft", "ready", "played"] = "draft"
    notes: str | None = Field(default=None, max_length=20000)
    scenes: list[_Szene] = Field(default=[], max_length=50)
    names: list[Annotated[str, Field(max_length=40)]] = Field(default=[], max_length=100)
    document_keys: list[str] = Field(default=[], max_length=20)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class _Kampagne(schemas.ApiModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    world_info: str | None = Field(default=None, max_length=200_000)
    language: schemas.ContentLanguage = "de"
    system: schemas.GameSystem | None = None
    system_name: str | None = Field(default=None, max_length=100)
    cover_preset: schemas.CoverPreset | None = None
    cover_image: str | None = None
    hotwords: dict | None = None
    next_session_at: datetime | None = None


class _Datei(schemas.ApiModel):
    format: str
    api_version: str
    campaign: _Kampagne
    seats: list[_Platz] = Field(default=[], max_length=1000)
    sessions: list[_Kapitel] = Field(default=[], max_length=10000)
    entries: list[_Eintrag] = Field(default=[], max_length=100000)
    documents: list[_Unterlage] = Field(default=[], max_length=10000)
    plans: list[_Plan] = Field(default=[], max_length=1000)  # 0.4.12; ältere Dateien haben keine


def _fassung(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(t) for t in text.strip().split("."))
    except ValueError:
        return (0,)


def _sicherer_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False
    teile = PurePosixPath(name).parts
    return ".." not in teile and (name == "kampagne.json" or teile[0] in ("bilder", "unterlagen"))


def zip_pruefen(pfad: Path) -> tuple[zipfile.ZipFile, _Datei]:
    """Pfade, entpackte Gesamtgröße und Format prüfen. Wirft ImportFehler mit festem Schlüssel."""
    try:
        z = zipfile.ZipFile(pfad)
    except (zipfile.BadZipFile, OSError):
        raise ImportFehler("import_format") from None
    infos = z.infolist()
    if len(infos) > MAX_EINTRAEGE_ZIP:
        raise ImportFehler("import_unsafe")
    grenze = get_settings().import_max_bytes * 3
    summe = 0
    for info in infos:
        if info.is_dir():
            continue
        if not _sicherer_name(info.filename) or info.flag_bits & 0x1:  # verschlüsselt
            raise ImportFehler("import_unsafe")
        summe += info.file_size
        if summe > grenze or (info.compress_size and info.file_size > 50 * 1024 * 1024
                              and info.file_size / info.compress_size > 200):
            raise ImportFehler("import_unsafe")
    try:
        info = z.getinfo("kampagne.json")
    except KeyError:
        raise ImportFehler("import_format") from None
    if info.file_size > MAX_JSON_BYTES:
        raise ImportFehler("import_unsafe")
    try:
        roh = json.loads(z.read(info).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, zipfile.BadZipFile, OSError):
        raise ImportFehler("import_format") from None
    if not isinstance(roh, dict) or not isinstance(roh.get("format"), str):
        raise ImportFehler("import_format")
    fmt = roh["format"]
    if fmt != FORMAT:
        if fmt.startswith("taleward-kampagne/"):
            raise ImportFehler("import_version")
        raise ImportFehler("import_format")
    from app.routers.auth import API_VERSION

    if _fassung(str(roh.get("apiVersion") or "0")) > _fassung(API_VERSION):
        raise ImportFehler("import_version")
    try:
        datei = _Datei.model_validate(roh)
    except ValidationError:
        raise ImportFehler("import_format") from None
    _verweise_pruefen(datei)
    return z, datei


def _verweise_pruefen(d: _Datei) -> None:
    """Schlüssel eindeutig, Verweise zeigen auf Vorhandenes, Kapitelnummern eindeutig."""
    for liste in (d.seats, d.sessions, d.entries, d.documents, d.plans):
        schluessel = [x.key for x in liste]
        if len(schluessel) != len(set(schluessel)):
            raise ImportFehler("import_format")
    nummern = [s.number for s in d.sessions]
    if len(nummern) != len(set(nummern)):
        raise ImportFehler("import_format")
    plaetze = {p.key for p in d.seats}
    kapitel = {s.key for s in d.sessions}
    for s in d.sessions:
        for a in s.attendees:
            if bool(a.seat) == bool((a.guest or "").strip()) or (a.seat and a.seat not in plaetze):
                raise ImportFehler("import_format")
        for k in s.comments:
            if k.author not in plaetze or (k.recipient and k.recipient not in plaetze):
                raise ImportFehler("import_format")
    for e in d.entries:
        if (e.holder_seat and e.holder_seat not in plaetze) or any(p not in plaetze for p in e.hidden_from_seats):
            raise ImportFehler("import_format")
        if any(m.session not in kapitel for m in e.mentions):
            raise ImportFehler("import_format")


# ---------------------------------------------------------------- Import: anlegen
def _teile_zusammenfuegen(imp: CampaignImport) -> Path:
    ziel = import_ordner(imp.id) / "datei.zip"
    if ziel.exists() and ziel.stat().st_size == imp.size_bytes:
        return ziel
    tmp = ziel.with_suffix(".tmp")
    with open(tmp, "wb") as aus:
        for i in range(imp.chunk_count):
            with open(teil_pfad(imp.id, i), "rb") as ein:
                shutil.copyfileobj(ein, aus)
    tmp.replace(ziel)
    shutil.rmtree(import_ordner(imp.id) / "teile", ignore_errors=True)
    return ziel


def import_ausfuehren(db: Session, import_id: str) -> None:
    imp = db.get(CampaignImport, import_id)
    if imp is None or imp.state != "processing":
        return
    angelegt: list[Path] = []
    try:
        pfad = _teile_zusammenfuegen(imp)
        imp.progress = 0.1
        db.commit()
        z, datei = zip_pruefen(pfad)
        with z:
            user = db.get(User, imp.user_id)
            if user is None:
                raise ImportFehler("import_format")
            c, offen = _anlegen(db, user, z, datei, angelegt, imp)
        imp.state, imp.progress, imp.campaign_id, imp.open_seats = "done", 1.0, c.id, offen
        imp.message, imp.finished_at = None, utcnow()
        db.commit()
        shutil.rmtree(import_ordner(imp.id), ignore_errors=True)
    except Exception as e:
        db.rollback()
        for p in angelegt:
            shutil.rmtree(p, ignore_errors=True)
        if isinstance(e, ImportFehler):
            schluessel = e.schluessel
        else:
            log.exception("Import %s fehlgeschlagen", import_id)
            schluessel = errors.ApiError(0, "internal_error").message("de")
        imp = db.get(CampaignImport, import_id)
        if imp is not None:
            imp.state, imp.progress, imp.message, imp.finished_at = "failed", None, schluessel, utcnow()
            db.commit()
        shutil.rmtree(import_ordner(import_id), ignore_errors=True)


def _lesen(z: zipfile.ZipFile, name: str | None) -> bytes | None:
    if not name or not _sicherer_name(name):
        return None
    try:
        return z.read(name)
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def _anlegen(db: Session, user: User, z: zipfile.ZipFile, d: _Datei, angelegt: list[Path],
             imp: CampaignImport) -> tuple[Campaign, int]:
    from app import namenshilfe
    from app.bilder import cover_ordner, cover_speichern, portrait_ordner, portrait_uebernehmen, typ_aus_endung
    from app.einrichtung import betriebsart
    from app.services import choose_organization, ensure_org_member, user_org_ids

    jetzt = utcnow()
    k = d.campaign
    try:
        org_id = choose_organization(db, user, None)
    except errors.ApiError:
        org_id = user_org_ids(db, user)[0]
    c = Campaign(title=k.title.strip()[:200] or "?", description=(k.description or "").strip(),
                 organization_id=org_id, language=k.language, system=k.system,
                 system_name=(k.system_name or "").strip() or None, world_info=(k.world_info or "").strip() or None,
                 cover_preset=k.cover_preset, next_session_at=k.next_session_at, imported_by_user_id=user.id)
    c.allow_external_transcription = c.allow_cloud_summary = betriebsart(db) == "cloud"
    if isinstance(k.hotwords, dict):
        gespeichert = {s: namenshilfe._eindeutig([x for x in (k.hotwords.get(s) or []) if isinstance(x, str)])
                       [:namenshilfe.MAX_ANZEIGE] for s in ("extra", "entfernt", "ignoriert")}
        namenshilfe._speichern(c, gespeichert)
    db.add(c)
    db.flush()
    ich = Member(campaign_id=c.id, user_id=user.id, role="gm", joined_at=jetzt)
    db.add(ich)
    db.flush()
    c.imported_by_member_id = ich.id
    ensure_org_member(db, org_id, user)

    # Plätze
    platz: dict[str, Member] = {}
    kennungen: set[str] = set()
    offen = 0
    for p in d.seats:
        kennung = (p.character_id or "").lower() or None
        if p.former or kennung in kennungen:
            kennung = None  # Ehemalige nehmen keinen Platz ein; doppelte Kennung nur beim ersten Platz
        if kennung:
            kennungen.add(kennung)
        m = Member(campaign_id=c.id, user_id=None, role=p.role, open_seat=not p.former,
                   character_name=(p.character_name or "").strip()[:200] or None, character_id=kennung,
                   joined_at=jetzt, left_at=(p.left_at or jetzt) if p.former else None)
        ch = p.character if p.consented and not p.former else None
        if ch is not None:
            m.character_version = ch.version if kennung else None
            m.character_status = ch.status
            m.character_nickname = (ch.nickname or "").strip() or None
            m.character_summary = (ch.summary or "").strip() or None
            m.character_backstory = (ch.backstory or "").strip() or None
            m.character_system = (ch.system or "").strip() or None
        db.add(m)
        db.flush()
        platz[p.key] = m
        offen += int(not p.former)
        if ch is not None and p.portrait:
            voll = _lesen(z, p.portrait)
            if voll is not None:
                try:
                    angelegt.append(portrait_ordner(m.id))
                    portrait_uebernehmen(m.id, voll, _lesen(z, p.portrait_thumb), p.portrait, p.portrait_thumb)
                    m.portrait_updated_at = jetzt
                except errors.ApiError:
                    pass  # kein brauchbares Bild – der Platz kommt trotzdem
    imp.progress = 0.3
    # Titelbild
    if k.cover_image:
        bild = _lesen(z, k.cover_image)
        if bild is not None:
            try:
                angelegt.append(cover_ordner(c.id))
                cover_speichern(c.id, bild, typ_aus_endung(k.cover_image))
                c.cover_image_updated_at, c.cover_preset = jetzt, None
            except errors.ApiError:
                pass
    if c.cover_preset is None and c.cover_image_updated_at is None:
        from app.services import random_cover

        c.cover_preset = random_cover()

    # Kapitel
    kapitel: dict[str, GameSession] = {}
    for s in sorted(d.sessions, key=lambda x: x.number):
        g = GameSession(campaign_id=c.id, number=s.number, title=(s.title or "").strip() or None,
                        played_at=s.played_at, state="published", published_at=s.published_at or s.played_at,
                        audio_deleted_at=None, state_updated_at=jetzt, created_at=jetzt)
        g.attendees = [Attendee(member_id=platz[a.seat].id if a.seat else None,
                                guest_name=None if a.seat else a.guest.strip()[:200], position=i,
                                consent=False, consent_source="on_site", consent_at=None)
                       for i, a in enumerate(s.attendees)]
        db.add(g)
        db.flush()
        kapitel[s.key] = g
        if s.recap is not None:
            db.add(Recap(session_id=g.id, title=s.recap.title, text=s.recap.text,
                         open_threads=json.dumps(s.recap.open_threads, ensure_ascii=False),
                         edited_at=s.recap.edited_at, model=None, review=None, created_at=jetzt))
        if (s.gm_note or "").strip():
            db.add(GmNote(session_id=g.id, text=s.gm_note, updated_at=jetzt))
        for kom in s.comments:
            db.add(Comment(session_id=g.id, author_member_id=platz[kom.author].id,
                           recipient_member_id=platz[kom.recipient].id if kom.recipient else None,
                           text=kom.text, created_at=kom.created_at, edited_at=kom.edited_at))
    imp.progress = 0.5

    # Bibel
    eintrag_neu: dict[str, str] = {}
    for e in d.entries:
        halter = platz[e.holder_seat].id if e.holder_seat else None
        x = Entry(campaign_id=c.id, type=e.type, name=e.name.strip()[:300] or "?", summary=e.summary or "",
                  gm_notes=(e.gm_notes or "").strip() or None, visibility=e.visibility,
                  status=(e.status or "active") if e.type == "quest" else None,
                  holder_member_id=halter if e.type in ("item", "pc") else None,
                  pc_character_id=((e.pc_character_id or "").lower() or None) if e.type == "pc" else None,
                  created_at=e.created_at or jetzt, updated_at=e.updated_at or jetzt,
                  public_changed_at=e.public_changed_at)
        if e.origin is not None:
            x.origin_character_id = e.origin.character_id.lower()
            x.origin_entry_id = (e.origin.entry_id or "").lower() or None
            x.origin_version = e.origin.version
            x.origin_member_id = platz[e.origin.seat].id if e.origin.seat in platz else None
        db.add(x)
        db.flush()
        eintrag_neu[e.key] = x.id
        for schluessel in sorted(set(e.hidden_from_seats)):
            db.add(EntryHidden(entry_id=x.id, member_id=platz[schluessel].id))
        for mn in e.mentions:
            db.add(EntryMention(entry_id=x.id, session_id=kapitel[mn.session].id, note=mn.note))
    imp.progress = 0.7

    # Unterlagen der SL – geprüft wie beim Hochladen, der Text wird neu ausgelesen
    from app import unterlagen

    unterlage_neu: dict[str, str] = {}
    for u in d.documents:
        daten = _lesen(z, u.file)
        if daten is None or len(daten) > unterlagen.MAX_BYTES:
            continue
        name = Path(u.file_name).name[:300] or Path(u.file).name
        try:
            art = unterlagen.art_der_datei(name, daten)
            x = unterlagen.auslesen(art, daten) if u.kind != "character_sheet" else None
        except errors.ApiError:
            continue  # unbrauchbare Unterlage – der Rest kommt trotzdem
        doc = CampaignDocument(campaign_id=c.id, title=u.title.strip()[:300] or Path(name).stem[:300],
                               file_name=name, kind=u.kind, size_bytes=len(daten),
                               page_count=x.seiten if x is not None else None,
                               uploaded_by_member_id=platz[u.uploaded_by_seat].id if u.uploaded_by_seat in platz
                               else ich.id, state="done", created_at=u.created_at or jetzt)
        db.add(doc)
        db.flush()
        unterlage_neu[u.key] = doc.id
        ordner = unterlagen.ordner(doc.id)
        angelegt.append(ordner)
        storage.write_atomic(ordner / f"datei{Path(name).suffix.lower()}", daten)
        if x is not None:
            storage.write_atomic(ordner / "text.json", json.dumps(x.abschnitte, ensure_ascii=False).encode())

    # Kapitelpläne (0.4.12): Verweise übersetzen; was nicht angelegt wurde, fällt heraus
    for pl in d.plans:
        szenen, gesehen = [], set()
        for sz in pl.scenes:
            if sz.id.lower() in gesehen:
                continue
            gesehen.add(sz.id.lower())
            szenen.append({"id": sz.id.lower(), "title": sz.title.strip() or "?", "notes": sz.notes, "state": sz.state,
                           "entryIds": list(dict.fromkeys(eintrag_neu[k] for k in sz.entry_keys if k in eintrag_neu))})
        db.add(ChapterPlan(campaign_id=c.id, title=pl.title.strip() or "?", session_number=pl.session_number,
                           state=pl.state, notes=pl.notes, scenes=json.dumps(szenen, ensure_ascii=False),
                           names=json.dumps(list(dict.fromkeys(n.strip() for n in pl.names if n.strip())),
                                            ensure_ascii=False),
                           document_ids=json.dumps(list(dict.fromkeys(unterlage_neu[k] for k in pl.document_keys
                                                                      if k in unterlage_neu))),
                           created_at=pl.created_at or jetzt, updated_at=pl.updated_at or jetzt))
    return c, offen


# ---------------------------------------------------------------- Offene Plätze
_VERWEISE = (
    (Attendee, "member_id"), (Comment, "author_member_id"), (Comment, "recipient_member_id"),
    (Entry, "holder_member_id"), (Entry, "origin_member_id"), (DateOption, "proposed_by_member_id"),
    (Proposal, "submitted_by_member_id"), (GmNotice, "member_id"), (CampaignDocument, "uploaded_by_member_id"),
)


def _zusammenlegen(db: Session, alt: Member, platz: Member) -> None:
    """Das Mitglied `alt` geht im Platz auf: Verweise umhängen, dann `alt` löschen. Die Aufnahme-Einwilligung der
    Person geht mit (es ist dieselbe Person auf demselben Server)."""
    for modell, feld in _VERWEISE:
        spalte = getattr(modell, feld)
        db.execute(update(modell).where(spalte == alt.id).values({feld: platz.id}))
    db.execute(update(Speaker).where(Speaker.assigned_member_id == alt.id).values(assigned_member_id=platz.id))
    db.execute(update(Speaker).where(Speaker.suggested_member_id == alt.id).values(suggested_member_id=platz.id))
    vorhanden = set(db.scalars(select(EntryHidden.entry_id).where(EntryHidden.member_id == platz.id)))
    for h in db.scalars(select(EntryHidden).where(EntryHidden.member_id == alt.id)):
        if h.entry_id not in vorhanden:
            db.add(EntryHidden(entry_id=h.entry_id, member_id=platz.id))
    db.execute(EntryHidden.__table__.delete().where(EntryHidden.member_id == alt.id))
    db.execute(SessionSeen.__table__.delete().where(SessionSeen.member_id == alt.id))
    db.execute(DateVote.__table__.delete().where(DateVote.member_id == alt.id))
    if alt.recording_consent_at is not None and platz.recording_consent_at is None:
        platz.recording_consent_at = alt.recording_consent_at
    for p in db.scalars(select(Proposal).where(Proposal.campaign_id == alt.campaign_id, Proposal.decision == "open")):
        ids = json.loads(p.hidden_member_ids_json or "[]")
        if alt.id in ids:
            p.hidden_member_ids_json = json.dumps(sorted((set(ids) - {alt.id}) | {platz.id}))
    db.flush()
    db.delete(alt)
    db.flush()


def platz_einnehmen(db: Session, platz: Member, user: User, rolle: str, frueher: Member | None = None) -> None:
    if frueher is not None:
        frueher.user_id = None
        db.flush()
        _zusammenlegen(db, frueher, platz)
    platz.user_id, platz.open_seat, platz.role = user.id, False, rolle
    platz.left_at, platz.deleted_at, platz.joined_at = None, None, utcnow()
    platz.chronicle_seen_at = platz.bible_seen_at = None
    db.flush()


def offener_platz_zu(db: Session, campaign_id: str, character_id: str) -> Member | None:
    return db.scalar(select(Member).where(Member.campaign_id == campaign_id, Member.open_seat.is_(True),
                                          Member.character_id == character_id.lower()).limit(1))


def offener_platz(db: Session, campaign_id: str, member_id: str) -> Member:
    m = db.get(Member, member_id)
    if m is None or m.campaign_id != campaign_id:
        raise errors.not_found("member")
    if not m.open_seat:
        raise errors.conflict("seat_not_open")
    return m


def platz_einladung(db: Session, platz: Member, user: User) -> Invite:
    from app.services import create_invite

    inv = create_invite(db, platz.campaign_id, user)
    inv.member_id = platz.id
    return inv


def nehmen(db: Session, c: Campaign, ich: Member, platz: Member) -> None:
    """„Das bin ich“: das Platzhalter-Mitglied der importierenden SL geht im alten Platz auf (Rolle gm)."""
    if c.imported_by_member_id is None or ich.id != c.imported_by_member_id:
        raise errors.ApiError(403, "not_importer")
    user = ich.user
    ich.user_id = None
    db.flush()
    _zusammenlegen(db, ich, platz)
    platz_einnehmen(db, platz, user, "gm")
    c.imported_by_member_id = None


def freigeben(db: Session, platz: Member) -> None:
    """Eingenommenen Platz wieder öffnen: Die Person verliert den Zugriff, ihre Einwilligungen enden."""
    from app.access import aktive_sl_anzahl
    from app.services import set_move_consent, set_recording_consent

    if platz.open_seat or not platz.aktiv:
        raise errors.conflict("seat_not_open")
    if platz.role == "gm" and aktive_sl_anzahl(db, platz.campaign_id) <= 1:
        raise errors.conflict("last_gm")
    set_recording_consent(db, platz, False)
    set_move_consent(db, platz, False)
    platz.user_id, platz.open_seat, platz.role = None, True, "player"
    platz.chronicle_seen_at = platz.bible_seen_at = None
    db.execute(SessionSeen.__table__.delete().where(SessionSeen.member_id == platz.id))
    db.execute(DateVote.__table__.delete().where(DateVote.member_id == platz.id))
    db.execute(GmNotice.__table__.delete().where(GmNotice.member_id == platz.id, GmNotice.code == "seat_claimed"))
    c = db.get(Campaign, platz.campaign_id)
    if c.imported_by_member_id == platz.id:
        c.imported_by_member_id = None


def eingenommen_melden(db: Session, platz: Member) -> None:
    db.add(GmNotice(campaign_id=platz.campaign_id, code="seat_claimed", member_id=platz.id, entry_ids="[]"))
