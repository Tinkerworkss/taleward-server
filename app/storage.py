"""Dateiablage unter DATA_DIR: Upload-Teile und Hörproben. Audio liegt nur hier, nie in der Datenbank."""
import shutil
from pathlib import Path

from app.config import get_settings


def uploads_root() -> Path:
    return get_settings().data_dir / "uploads"


def upload_dir(upload_id: str) -> Path:
    return uploads_root() / upload_id


def chunk_path(upload_id: str, file_id: str, index: int) -> Path:
    return upload_dir(upload_id) / file_id / f"{index:05d}.part"


def samples_dir(session_id: str) -> Path:
    return get_settings().data_dir / "samples" / session_id


def sample_path(session_id: str, speaker_id: str) -> Path:
    return samples_dir(session_id) / f"{speaker_id}.ogg"


def delete_upload_files(upload_id: str) -> None:
    shutil.rmtree(upload_dir(upload_id), ignore_errors=True)


def delete_samples(session_id: str) -> None:
    shutil.rmtree(samples_dir(session_id), ignore_errors=True)


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
