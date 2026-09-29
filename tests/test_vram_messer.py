"""Speichermessung ohne Programmstarts im Sekundentakt (Windows: Lader und Thread-Start blockieren sich sonst)."""
import subprocess

from app import probelauf


def test_nvml_wird_bevorzugt(monkeypatch):
    monkeypatch.setattr(probelauf._Nvml, "speicher_mb", classmethod(lambda cls: (3000, 8192)))
    monkeypatch.setattr(probelauf.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("kein Start")))
    assert probelauf.VramMesser._lesen() == 3000 and probelauf.VramMesser.gesamt_mb() == 8192


def test_ausweichweg_ohne_lese_threads(monkeypatch):
    monkeypatch.setattr(probelauf._Nvml, "speicher_mb", classmethod(lambda cls: None))
    aufrufe = []

    class P:
        def __init__(self, cmd, **kw):
            aufrufe.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def communicate(self, timeout=None):
            return "2048\n", None

    monkeypatch.setattr(probelauf.subprocess, "Popen", P)
    assert probelauf.VramMesser._lesen() == 2048
    kw = aufrufe[0]
    assert kw["stdin"] is subprocess.DEVNULL and kw["stderr"] is subprocess.DEVNULL and kw["stdout"] is subprocess.PIPE
