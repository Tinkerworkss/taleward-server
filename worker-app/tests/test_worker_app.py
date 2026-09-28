import json
import sys
import time
from pathlib import Path

import pytest

from taleward_worker import autostart, hardware, motor, pfade, server
from taleward_worker.dienst import Dienst
from taleward_worker.einstellungen import Einstellungen


def test_einstellungen_speichern_ohne_token_in_oberflaeche():
    e = Einstellungen(server="https://t.example.org", token="wk.geheim")
    e.speichern()
    neu = Einstellungen.laden()
    assert neu.token == "wk.geheim" and neu.gekoppelt
    assert "token" not in neu.fuer_oberflaeche() and neu.fuer_oberflaeche()["gekoppelt"]
    if sys.platform != "win32":
        assert oct(pfade.einstellungen().stat().st_mode)[-3:] == "600"


def test_adressen():
    assert server.adresse_normalisieren(" taleward.example.org/ ") == "https://taleward.example.org"
    assert server.unverschluesselt("http://192.168.1.20:8000")
    assert not server.unverschluesselt("http://localhost:8000")
    assert not server.unverschluesselt("https://x.org")
    with pytest.raises(server.ServerFehler):
        server.adresse_normalisieren("  ")


def test_modellwahl_nach_grafikspeicher():
    assert hardware.modell_fuer(8192) == {"modell": "large-v3", "batch": 8}   # RTX 3060 Ti
    assert hardware.modell_fuer(24576)["batch"] == 16
    assert hardware.modell_fuer(4096)["modell"] == "large-v3-turbo"


def test_profile_mit_speichergrenze():
    p = hardware.profil(8192, grenze_mb=4096)          # Schieberegler auf 4 GB
    assert (p["modell"], p["batch"], p["geraet"], p["grenzeMb"]) == ("large-v3-turbo", 4, "cuda", 4096)
    p = hardware.profil(8192, grenze_mb=2048)          # sehr knapp: Ausrichtung/Sprecher auf dem Prozessor
    assert (p["geraet"], p["ausrichten"], p["sprecher"], p["batch"]) == ("cuda", "cpu", "cpu", 2)
    assert hardware.profil(8192, grenze_mb=1024)["geraet"] == "cpu"
    assert hardware.profil(None)["stufe"] == "cpu" and hardware.profil(None)["genauigkeit"] == "int8"
    assert hardware.profil(8192, prozessor=True)["geraet"] == "cpu"
    assert hardware.profil(8192)["grenzeMb"] == 0
    # Wunsch „genau“ trotz knapper Grenze: large-v3 mit kleinerem Stapel, Rest auf dem Prozessor
    p = hardware.profil(8192, grenze_mb=4096, modell_wunsch="large-v3")
    assert (p["modell"], p["batch"], p["ausrichten"]) == ("large-v3", 2, "cpu")
    # Dauer-Schätzung wird mit weniger Speicher nicht kürzer
    assert hardware.profil(8192)["dauerMin"][1] <= hardware.profil(8192, grenze_mb=2048)["dauerMin"][1]


def test_motor_umgebung_folgt_profil(monkeypatch):
    from taleward_worker import dienst as modul

    monkeypatch.setattr(hardware, "grafikkarten", lambda frisch=False: [hardware.Grafikkarte("RTX", 8192, "576.1")])
    e = Einstellungen(server="https://x", token="t", vram_grenze_mb=4096)
    env = modul.Dienst(e)._motor_umgebung()
    assert env["WHISPER_MODEL"] == "large-v3-turbo" and env["GPU_MEMORY_LIMIT_MB"] == "4096"
    assert env["WHISPER_DEVICE"] == "cuda" and env["CPU_THREADS"] == "0"
    e.geraet = "cpu"
    env = modul.Dienst(e)._motor_umgebung()
    assert env["WHISPER_DEVICE"] == env["ALIGN_DEVICE"] == env["DIARIZE_DEVICE"] == "cpu"
    assert env["WHISPER_COMPUTE_TYPE"] == "int8" and int(env["CPU_THREADS"]) >= 1
    assert hardware.Grafikkarte("x", 8192, "576.88").treiber_ok
    assert not hardware.Grafikkarte("x", 8192, "470.10").treiber_ok


def test_quelle_lokal_und_main(monkeypatch):
    monkeypatch.setenv("TALEWARD_MOTOR_QUELLE", "/pfad/server")
    q = motor.quelle("0.4.0")
    assert q["paket"] == "/pfad/server" and q["anforderungen"].endswith("engine-requirements.txt")
    monkeypatch.delenv("TALEWARD_MOTOR_QUELLE")
    assert motor._adressen("main", False)["paket"].endswith("/refs/heads/main.zip")
    assert motor._adressen("v0.4.0", True)["paket"].endswith("/refs/tags/v0.4.0.zip")


@pytest.mark.skipif(sys.platform == "win32", reason="Linux-Autostart")
def test_autostart_linux(eigener_ordner):
    assert not autostart.aktiv()
    autostart.setzen(True)
    datei = eigener_ordner / "config" / "autostart" / "taleward-worker.desktop"
    assert autostart.aktiv() and "--hintergrund" in datei.read_text()
    autostart.setzen(False)
    assert not autostart.aktiv()


# ---------------------------------------------------------------- Dienst mit einem Schein-Motor
SCHEIN = r'''
import json, sys, time
def m(e, **d): print("@@taleward " + json.dumps({"ereignis": e, **d}), flush=True)
print("12:00:00 Worker bereit", flush=True)
m("pruefe"); m("bereit", gpu="Test-GPU", modell="large-v3"); m("warte")
m("auftrag", jobId="j1", typ="transcribe", dateien=2)
for p in (0.2, 0.6, 1.0): m("fortschritt", jobId="j1", p=p)
m("fertig", jobId="j1", sekunden=90); m("warte")
for zeile in sys.stdin:
    b = zeile.strip()
    if b == "pause": m("pausiert")
    elif b == "weiter": m("fortgesetzt")
    elif b == "stopp": break
m("beendet")
'''


def _warten(bedingung, sekunden=10):
    ende = time.time() + sekunden
    while time.time() < ende:
        if bedingung():
            return True
        time.sleep(0.05)
    return False


def test_dienst_verarbeitet_ereignisse(tmp_path):
    skript = tmp_path / "schein.py"
    skript.write_text(SCHEIN)
    e = Einstellungen(server="http://localhost:1", token="t")
    d = Dienst(e, befehl=[sys.executable, str(skript)])
    d.starten()
    assert _warten(lambda: d.statistik.fuer_oberflaeche()["heute"] == 1 and d.zustand["art"] == "warte")
    assert d.info["gpu"] == "Test-GPU"
    assert any("Auftrag fertig" in z for z in d.protokoll.zeilen)
    assert any(z.endswith("Worker bereit") and "12:00:00" not in z for z in d.protokoll.zeilen)
    d.pausieren(True)
    assert _warten(lambda: d.zustand["art"] == "pausiert")
    d.pausieren(False)
    assert _warten(lambda: d.zustand["art"] == "warte")
    d.stoppen()
    assert d.zustand["art"] == "gestoppt" and not d.laeuft()


def test_dienst_startet_nach_absturz_neu(tmp_path, monkeypatch):
    from taleward_worker import dienst as modul

    monkeypatch.setattr(modul, "WARTEZEITEN", [1])
    skript = tmp_path / "absturz.py"
    zaehler = tmp_path / "n"
    skript.write_text(f"import pathlib,sys; p=pathlib.Path(r'{zaehler}'); p.write_text(str(int(p.read_text() or 0)+1) "
                      "if p.exists() else '1'); sys.exit(3)")
    d = Dienst(Einstellungen(server="x", token="t"), befehl=[sys.executable, str(skript)])
    d.starten()
    assert _warten(lambda: zaehler.exists() and int(zaehler.read_text()) >= 2, 8)
    d.stoppen()


def test_dienst_ohne_motor():
    d = Dienst(Einstellungen(server="x", token="t"))
    d.starten()
    assert d.zustand == {**d.zustand, "art": "fehler", "code": "motor_fehlt"}


def test_installiert_liest_kennzeichnung():
    assert motor.installiert() is None
    py = pfade.motor_python()
    py.parent.mkdir(parents=True)
    py.write_text("")
    (pfade.motor() / "taleward-motor.json").write_text(json.dumps({"fassung": "0.4.0", "testmodus": False}))
    assert motor.installiert()["fassung"] == "0.4.0"
    motor.entfernen()
    assert not Path(pfade.motor()).exists()


# ---------------------------------------------------------------- Selbst-Update über den Server
def test_selbstupdate_fassungen():
    from taleward_worker import selbstupdate

    assert selbstupdate.neuer("0.2.0", "0.1.0") and not selbstupdate.neuer("0.1.0", "0.1.0")
    assert selbstupdate.neuer("0.10.0", "0.9.9") and not selbstupdate.neuer("kaputt", "0.1.0")


def test_selbstupdate_abfragen_und_laden(monkeypatch):
    import hashlib

    import httpx

    from taleward_worker import VERSION, selbstupdate

    inhalt = b"MZ neue Fassung"

    def antwort(req):
        if req.url.path == "/worker/v1/app-update":
            assert req.headers["Authorization"] == "Bearer t" and req.url.params["system"] in ("windows", "linux")
            return httpx.Response(200, json={"version": "9.9.9", "url": "https://srv/downloads/x.exe",
                                             "sha256": hashlib.sha256(inhalt).hexdigest(), "notes": "Neu"})
        return httpx.Response(200, content=inhalt)

    transport = httpx.MockTransport(antwort)
    echt_get, echt_stream = httpx.get, httpx.stream
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Client(transport=transport).get(*a, **{
        x: y for x, y in k.items() if x != "timeout"}))

    def stream(methode, url, **k):
        return httpx.Client(transport=transport).stream(methode, url, headers=k.get("headers"))

    monkeypatch.setattr(httpx, "stream", stream)
    angebot = selbstupdate.abfragen("https://srv", "t")
    assert angebot["version"] == "9.9.9" and selbstupdate.neuer(angebot["version"], VERSION)
    u = selbstupdate.Aktualisierung(angebot, "https://srv", "t")
    datei = u._laden()
    assert datei.read_bytes() == inhalt and u.anteil > 0
    u2 = selbstupdate.Aktualisierung({**angebot, "sha256": "0" * 64}, "https://srv", "t")
    with pytest.raises(RuntimeError, match="Prüfsumme"):
        u2._laden()
    assert not list((pfade.basis() / "updates").glob("*.exe"))
    assert echt_get and echt_stream
