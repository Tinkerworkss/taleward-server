"""Anbindung der Worker-App: Ereignisse als JSON-Zeilen, Pause und Stopp über stdin."""
import io
import json
import threading
import time

from starlette.testclient import TestClient

from tests.test_step2a import _kleine_teile, audio_abschnitte, hochladen, neue_session, worker_token  # noqa: F401


def _ereignisse(text: str) -> list[dict]:
    from app.worker_app import VORSILBE

    return [json.loads(z[len(VORSILBE):]) for z in text.splitlines() if z.startswith(VORSILBE)]


def test_ereignisse_eines_auftrags(client, world, dbs, tmp_path):
    from app.worker_app import Anbindung
    from app.worker_prozess import WorkerProzess, verarbeite_attrappe

    aus = io.StringIO()
    anb = Anbindung(aus=aus, ein=io.StringIO(""))
    s = neue_session(client, world)
    hochladen(client, world["gm"], s["id"], audio_abschnitte(tmp_path))
    k = WorkerProzess("http://testserver", worker_token(dbs), tmp_path / "k", verarbeite_attrappe,
                      client=TestClient(client.app), claim_wait=0, melden=anb.melden)
    assert k.einen_auftrag()
    e = _ereignisse(aus.getvalue())
    arten = [x["ereignis"] for x in e]
    assert arten[0] == "auftrag" and arten[-1] == "fertig" and "fortschritt" in arten
    assert e[0]["typ"] == "transcribe" and e[0]["dateien"] == 3
    p = [x["p"] for x in e if x["ereignis"] == "fortschritt"]
    assert p == sorted(p) and p[-1] <= 1.0
    # Keine Inhalte: nur bekannte Felder
    erlaubt = {"ereignis", "zeit", "jobId", "typ", "dateien", "p", "sekunden", "audioSekunden", "peakVramMb", "tokenS",
               "text", "schritt"}
    assert all(set(x) <= erlaubt for x in e)
    # Was der Worker gerade tut, kommt als Ereignis – nur mit dem Text der Tätigkeit, nie mit Inhalten
    from app import worker_prozess

    assert worker_prozess._taetigkeit is None  # nach dem Auftrag wieder abgemeldet


def test_fehlschlag_wird_gemeldet(client, world, dbs, tmp_path):
    from app.audio import AudioFehler
    from app.worker_app import Anbindung
    from app.worker_prozess import WorkerProzess

    def kaputt(*a):
        raise AudioFehler("audio_unreadable", "Datei nicht lesbar", False)

    aus = io.StringIO()
    s = neue_session(client, world)
    hochladen(client, world["gm"], s["id"], audio_abschnitte(tmp_path))
    k = WorkerProzess("http://testserver", worker_token(dbs), tmp_path / "k", kaputt,
                      client=TestClient(client.app), claim_wait=0, melden=Anbindung(aus=aus).melden)
    k.einen_auftrag()
    e = _ereignisse(aus.getvalue())
    assert e[-1]["ereignis"] == "fehlgeschlagen" and e[-1]["code"] == "audio_unreadable"


def test_pause_weiter_stopp(client, dbs, tmp_path):
    from app.worker_app import Anbindung
    from app.worker_prozess import WorkerProzess, verarbeite_attrappe

    aus = io.StringIO()

    class Eingabe:
        def __init__(self):
            self.zeilen = []
            self.neu = threading.Event()

        def __iter__(self):
            while True:
                self.neu.wait()
                self.neu.clear()
                while self.zeilen:
                    z = self.zeilen.pop(0)
                    if z is None:
                        return
                    yield z

        def senden(self, z):
            self.zeilen.append(z)
            self.neu.set()

    ein = Eingabe()
    anb = Anbindung(aus=aus, ein=ein)
    k = WorkerProzess("http://testserver", worker_token(dbs), tmp_path / "k", verarbeite_attrappe,
                      client=TestClient(client.app), claim_wait=0, melden=anb.melden)
    beendet = threading.Event()
    anb.steuern(k, beenden=beendet.set)
    lauf = threading.Thread(target=k.laufen, daemon=True)
    lauf.start()
    ein.senden("pause\n")
    time.sleep(0.3)
    ein.senden("weiter\n")
    time.sleep(0.3)
    ein.senden("stopp\n")
    assert beendet.wait(10)
    lauf.join(5)
    arten = [x["ereignis"] for x in _ereignisse(aus.getvalue())]
    assert "warte" in arten
    assert arten.index("pausiert") < arten.index("fortgesetzt") < arten.index("beendet")
    assert not lauf.is_alive()


def test_serverfassung_beim_koppeln(client, dbs):
    from app.koppeln import code_erzeugen

    code, _ = code_erzeugen(dbs)
    dbs.commit()
    r = client.post("/worker/v1/pair", json={"code": code, "name": "spiele-pc"})
    assert r.status_code == 201 and r.json()["serverVersion"] == "0.4.60"


def test_engine_requirements_passen_zu_uv_lock(tmp_path):
    """Die Worker-App installiert das KI-Paket aus engine-requirements.txt – sie muss zu uv.lock passen."""
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    if shutil.which("uv") is None:
        pytest.skip("uv fehlt")
    wurzel = Path(__file__).resolve().parent.parent
    ziel = tmp_path / "req.txt"
    subprocess.run(["uv", "export", "--format", "requirements-txt", "--extra", "ki", "--no-dev", "--no-hashes",
                    "--no-emit-project", "--no-header", "--frozen", "-q", "-o", str(ziel)], cwd=wurzel, check=True)
    assert ziel.read_text() == (wurzel / "engine-requirements.txt").read_text(), \
        "engine-requirements.txt neu erzeugen (Befehl in docs/ENTWICKLUNG.md)"
