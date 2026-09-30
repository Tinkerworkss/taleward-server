"""Worker auf fremden Windows-Rechnern: Pascal-Karten, fehlende Laufzeit, voller Grafikspeicher, nvml im Treiberspeicher."""
import sys
from pathlib import Path

import pytest

from app import probelauf, transkription
from app.audio import AudioFehler
from app.transkription import WhisperXMotor, _mit_ausweichen


def test_sparsamer_stuft_zweimal_ab_und_dann_nicht_mehr():
    m = WhisperXMotor("large-v3", "int8_float16", 8, geraet="cuda")
    assert m.sparsamer() and m.geraete == "cuda/cpu/cpu" and m.batch == 8
    assert m.sparsamer() and m.batch == 4
    assert m.sparsamer() and m.batch == 2
    assert m.sparsamer() and m.batch == 1
    assert not m.sparsamer()
    assert not WhisperXMotor("large-v3", "int8", 4, geraet="cpu").sparsamer()


def test_voller_grafikspeicher_wird_einmal_sparsamer_wiederholt():
    m = WhisperXMotor("large-v3", "int8_float16", 8, geraet="cuda")
    laeufe = []

    def lauf():
        laeufe.append(m.geraete)
        if len(laeufe) == 1:
            raise RuntimeError("CUDA out of memory. Tried to allocate 512 MiB")
        return {"ok": True}

    assert _mit_ausweichen(m, lauf) == {"ok": True}
    assert laeufe == ["cuda/cuda/cuda", "cuda/cpu/cpu"]


def test_zweimal_voll_ist_ein_fehler():
    m = WhisperXMotor("large-v3", "int8_float16", 8, geraet="cuda")

    def lauf():
        raise RuntimeError("CUDA out of memory")

    with pytest.raises(AudioFehler) as e:
        _mit_ausweichen(m, lauf)
    assert e.value.code == "cuda_oom" and e.value.retryable


def test_andere_fehler_werden_nicht_wiederholt():
    laeufe = []

    def lauf():
        laeufe.append(1)
        raise ValueError("kaputt")

    with pytest.raises(AudioFehler) as e:
        _mit_ausweichen(WhisperXMotor(geraet="cuda"), lauf)
    assert e.value.code == "worker_error" and len(laeufe) == 1


def test_dll_fehler_ist_einrichtungsfehler(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    f = transkription._uebersetzen(OSError("[WinError 126] Das angegebene Modul wurde nicht gefunden. c10.dll"))
    assert f.code == "worker_setup" and not f.retryable and "Visual C++" in str(f)
    f = transkription._uebersetzen(ImportError("DLL load failed while importing _C"))
    assert f.code == "worker_setup"
    monkeypatch.setattr(sys, "platform", "linux")
    assert "Visual C++" not in transkription.laufzeit_hinweis(OSError("x"))


def test_nvml_auch_im_treiberspeicher(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    ablage = tmp_path / "System32" / "DriverStore" / "FileRepository" / "nv_dispi.inf_amd64_123"
    ablage.mkdir(parents=True)
    (ablage / "nvml.dll").write_bytes(b"")
    (tmp_path / "System32" / "DriverStore" / "FileRepository" / "intel.inf_amd64_1").mkdir()
    namen = probelauf._nvml_kandidaten()
    assert namen[0] == "nvml.dll" and str(ablage / "nvml.dll") in namen
    assert not any("intel" in n for n in namen)
    monkeypatch.setattr(sys, "platform", "linux")
    assert probelauf._nvml_kandidaten() == ["libnvidia-ml.so.1", "libnvidia-ml.so"]


def test_pascal_karte_rechnet_int8(monkeypatch):
    """Compute Capability < 7.0 → kein float16; sonst lehnt CTranslate2 den Auftrag ab."""
    import types

    torch = types.SimpleNamespace(cuda=types.SimpleNamespace(
        is_available=lambda: True, get_device_capability=lambda i: (6, 1),
        get_device_name=lambda i: "GeForce GTX 1070", get_device_properties=lambda i: None),
        set_num_threads=lambda n: None)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "whisperx", types.ModuleType("whisperx"))
    monkeypatch.setattr(transkription, "cuda_bibliotheken_vorladen", lambda: None)
    from app import modelle

    class Bereit:
        repo, fassung, pfad, kennung = "x", "abc", Path("."), "x"

    monkeypatch.setattr(modelle, "bereitstellen", lambda repo, token: Bereit())
    monkeypatch.setattr(probelauf.VramMesser, "gesamt_mb", staticmethod(lambda: 8192))
    m = WhisperXMotor("large-v3", "int8_float16", 8, hf_token="hf_x", geraet="cuda")
    info = m.pruefen()
    assert info["genauigkeit"] == "int8" and m.genauigkeit == "int8"


def test_sechs_gb_karte_bekommt_kleineren_stapel():
    from app.arbeitsweise import profil

    assert (profil(6144)["modell"], profil(6144)["batch"]) == ("large-v3", 4)
    assert (profil(8192)["modell"], profil(8192)["batch"]) == ("large-v3", 8)
