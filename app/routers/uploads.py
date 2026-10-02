"""Stückweiser Upload der Aufnahme: starten, Teile senden (idempotent), fehlende Teile abfragen, abschließen."""
import hashlib
import math

from typing import Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors, schemas, storage
from app.access import current_user, load_session_gm, membership
from app.config import get_settings
from app.db import get_db, utcnow
from app.errors import sprache
from app.models import GameSession, Upload, UploadFile, User
from app.queue import create_transcribe_job
from app.services import check_upload_consent, processing_status, set_state

router = APIRouter(tags=["Upload"])

ERLAUBTE_MIME = ("audio/", "video/webm", "video/mp4", "application/ogg")


class FileIn(schemas.ApiModel):
    file_name: str = Field(min_length=1, max_length=300)
    size_bytes: int = Field(gt=0)
    mime_type: str = Field(min_length=1, max_length=100)
    track_member_id: str | None = None


class UploadStart(schemas.ApiModel):
    source: Literal["table", "discord"]
    files: list[FileIn] = Field(max_length=500)


class UploadFileOut(schemas.ApiModel):
    file_id: str
    file_name: str
    chunk_count: int


class UploadStartOut(schemas.ApiModel):
    upload_id: str
    chunk_size_bytes: int
    files: list[UploadFileOut]


class MissingOut(schemas.ApiModel):
    file_id: str
    missing_chunks: list[int]


class UploadStatusOut(schemas.ApiModel):
    files: list[MissingOut]


def _start_out(up: Upload) -> UploadStartOut:
    return UploadStartOut(
        upload_id=up.id, chunk_size_bytes=up.chunk_size,
        files=[UploadFileOut(file_id=f.id, file_name=f.file_name, chunk_count=f.chunk_count) for f in up.files],
    )


def _gleiche_dateien(up: Upload, body: UploadStart) -> bool:
    alt = [(f.file_name, f.size_bytes, f.track_member_id) for f in up.files]
    neu = [(f.file_name, f.size_bytes, f.track_member_id) for f in body.files]
    return up.source == body.source and alt == neu


def _pruefe_dateien(s: GameSession, body: UploadStart) -> None:
    if not body.files:
        raise errors.bad_request("files_empty")
    maxb = get_settings().max_file_bytes
    for f in body.files:
        mime = f.mime_type.lower()
        if not mime.startswith(ERLAUBTE_MIME):
            raise errors.bad_request("unsupported_audio", name=f.file_name)
        if f.size_bytes > maxb:
            raise errors.ApiError(413, "file_too_large", name=f.file_name)
    if body.source == "table":
        if any(f.track_member_id for f in body.files):
            raise errors.bad_request("track_member_unexpected")
    else:
        anwesend = {a.member_id for a in s.attendees if a.member_id}
        ids = [f.track_member_id for f in body.files]
        if any(i is None or i not in anwesend for i in ids) or len(set(ids)) != len(ids):
            raise errors.bad_request("track_member_invalid")


@router.post("/sessions/{sessionId}/uploads", status_code=201, response_model=UploadStartOut)
def start_upload(sessionId: str, body: UploadStart, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_session_gm(db, sessionId, user).session
    check_upload_consent(db, s)
    offen = db.scalar(select(Upload).where(Upload.session_id == s.id, Upload.state == "open"))
    if offen is not None:
        if _gleiche_dateien(offen, body):
            return _start_out(offen)  # App setzt einen abgebrochenen Upload fort
        raise errors.conflict("upload_in_progress")
    if s.state not in ("created", "failed"):
        raise errors.conflict("invalid_state", "invalid_state.upload")
    _pruefe_dateien(s, body)
    # Neuanfang nach Fehlschlag: altes Audio verwerfen
    for alt in db.scalars(select(Upload).where(Upload.session_id == s.id, Upload.state == "completed")):
        storage.delete_upload_files(alt.id)
        alt.state = "aborted"
    chunk = get_settings().chunk_size_bytes
    up = Upload(session_id=s.id, source=body.source, chunk_size=chunk)
    up.files = [
        UploadFile(position=i, file_name=f.file_name, size_bytes=f.size_bytes, mime_type=f.mime_type,
                   track_member_id=f.track_member_id, chunk_count=math.ceil(f.size_bytes / chunk))
        for i, f in enumerate(body.files)
    ]
    db.add(up)
    s.source = body.source
    s.audio_deleted_at = None
    s.duration_seconds = None
    set_state(s, "uploading")
    db.commit()
    db.refresh(up)
    return _start_out(up)


def _load_upload(db: Session, upload_id: str, user: User) -> tuple[Upload, GameSession]:
    up = db.get(Upload, upload_id)
    if up is None:
        raise errors.not_found()
    s = db.get(GameSession, up.session_id)
    m = membership(db, s.campaign_id, user)
    if m is None or m.role != "gm":  # Uploads gehören der SL; für alle anderen existieren sie nicht
        raise errors.not_found()
    return up, s


def _expected_size(up: Upload, f: UploadFile, index: int) -> int:
    if index < f.chunk_count - 1:
        return up.chunk_size
    return f.size_bytes - up.chunk_size * (f.chunk_count - 1)


@router.put("/uploads/{uploadId}/files/{fileId}/chunks/{index}", status_code=204)
async def put_chunk(
    uploadId: str, fileId: str, index: int, request: Request,
    x_chunk_sha256: str | None = Header(default=None),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    up, _ = _load_upload(db, uploadId, user)
    f = next((x for x in up.files if x.id == fileId), None)
    if f is None:
        raise errors.not_found()
    if up.state != "open":
        raise errors.conflict("upload_closed")
    if index < 0 or index >= f.chunk_count:
        raise errors.bad_request("chunk_index_invalid", index=index)
    erwartet = _expected_size(up, f, index)
    from app.koerper import lesen

    data = await lesen(request, up.chunk_size)
    if len(data) != erwartet:
        raise errors.bad_request("chunk_size_mismatch", index=index, got=len(data), expected=erwartet)
    if x_chunk_sha256 and hashlib.sha256(data).hexdigest() != x_chunk_sha256.strip().lower():
        raise errors.bad_request("chunk_checksum_mismatch", index=index)
    storage.write_atomic(storage.chunk_path(up.id, f.id, index), data)
    return Response(status_code=204)


def _missing(up: Upload) -> dict[str, list[int]]:
    return {
        f.id: [i for i in range(f.chunk_count) if not storage.chunk_path(up.id, f.id, i).exists()]
        for f in up.files
    }


@router.get("/uploads/{uploadId}", response_model=UploadStatusOut)
def upload_status(uploadId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    up, _ = _load_upload(db, uploadId, user)
    fehlend = _missing(up) if up.state == "open" else {f.id: [] for f in up.files}
    return UploadStatusOut(files=[MissingOut(file_id=k, missing_chunks=v) for k, v in fehlend.items()])


@router.post("/uploads/{uploadId}/complete", status_code=202, response_model=schemas.ProcessingStatusOut)
def complete(uploadId: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    up, s = _load_upload(db, uploadId, user)
    if up.state == "completed":
        return processing_status(db, s, sprache(request))  # doppelt abgeschickt – harmlos
    if up.state != "open":
        raise errors.conflict("upload_closed")
    anzahl = sum(len(v) for v in _missing(up).values())
    if anzahl:
        raise errors.conflict("chunks_missing", count=anzahl)
    check_upload_consent(db, s)  # Widerruf während des Hochladens
    up.state = "completed"
    up.completed_at = utcnow()
    create_transcribe_job(db, s, up)
    db.commit()
    return processing_status(db, s, sprache(request))
