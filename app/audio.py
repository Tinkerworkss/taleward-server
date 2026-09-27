"""Audio-Hilfen für den Worker: Abschnitte dekodieren und lückenlos zusammenfügen, Hörproben schneiden.

Jeder Abschnitt wird einzeln zu 16 kHz mono PCM dekodiert und angehängt (kein „concat -c copy“):
robust bei gemischten Formaten, und die Zeitstempel stimmen exakt. Der Startversatz jedes Abschnitts
wird zurückgegeben – ist ein Abschnitt defekt, nennt die Meldung seine Nummer.
"""
import shutil
import subprocess
import wave
from pathlib import Path

SAMPLE_RATE = 16000


class AudioFehler(Exception):
    """Fehler mit verständlicher Meldung; `retryable` = lohnt ein neuer Versuch?"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def _ffmpeg() -> str:
    pfad = shutil.which("ffmpeg")
    if not pfad:
        raise AudioFehler("ffmpeg_missing", "ffmpeg ist auf dem Worker nicht installiert.", retryable=True)
    return pfad


def dekodieren(quelle: Path) -> bytes:
    """Eine Datei zu 16 kHz mono, 16 bit PCM (roh)."""
    res = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(quelle),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        capture_output=True,
    )
    if res.returncode != 0 or not res.stdout:
        raise AudioFehler("audio_unreadable", res.stderr.decode(errors="replace").strip()[-300:] or "keine Audiodaten")
    return res.stdout


def zusammenfuegen(abschnitte: list[Path], ziel: Path) -> list[float]:
    """Abschnitte in Reihenfolge zu einer WAV-Datei. Gibt die Startzeit (s) jedes Abschnitts zurück."""
    starts, pos = [], 0
    with wave.open(str(ziel), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        for nr, datei in enumerate(abschnitte, start=1):
            try:
                pcm = dekodieren(datei)
            except AudioFehler as e:
                raise AudioFehler("audio_unreadable", f"Abschnitt {nr} ({datei.name}) ist nicht lesbar: {e}") from e
            starts.append(pos / SAMPLE_RATE)
            w.writeframes(pcm)
            pos += len(pcm) // 2
    return starts


def dauer(wav: Path) -> float:
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def hoerprobe(wav: Path, start: float, laenge: float, ziel: Path) -> bytes:
    """Kurze Hörprobe als Ogg/Opus (wenige Sekunden, klein)."""
    res = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, start):.2f}",
         "-t", f"{laenge:.2f}", "-i", str(wav), "-c:a", "libopus", "-b:a", "24k", str(ziel)],
        capture_output=True,
    )
    if res.returncode != 0:
        raise AudioFehler("sample_failed", res.stderr.decode(errors="replace").strip()[-300:])
    return ziel.read_bytes()
