"""ffmpeg darf nie auf Eingaben warten: Unter der Worker-App läuft deren Steuerung über stdin."""
import subprocess
import sys

import pytest

from app import audio


def test_ffmpeg_ohne_eingabekanal_und_mit_zeitgrenze(monkeypatch, tmp_path):
    aufrufe = []

    def run(cmd, **kw):
        aufrufe.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, b"\0\0" * 1600, b"")

    monkeypatch.setattr(audio, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(audio.subprocess, "run", run)
    datei = tmp_path / "a.m4a"
    datei.write_bytes(b"x" * 1000)
    assert audio.dekodieren(datei)
    cmd, kw = aufrufe[0]
    assert cmd[:2] == ["ffmpeg", "-nostdin"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] >= 600
    if sys.platform == "win32":
        assert kw["creationflags"] == 0x08000000


def test_haengendes_ffmpeg_bricht_mit_meldung_ab(monkeypatch, tmp_path):
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(audio, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(audio.subprocess, "run", run)
    with pytest.raises(audio.AudioFehler) as e:
        audio.dekodieren(tmp_path / "fehlt.m4a")
    assert e.value.code == "audio_timeout" and e.value.retryable
