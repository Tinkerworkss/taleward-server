"""Pause in beide Richtungen: in der Worker-App pausiert → sieht der Server; in der Verwaltung pausiert → sieht die App."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.test_betrieb import einrichten, ntfy  # noqa: F401 (Fixture)
from tests.test_step2a import audio_abschnitte, hochladen, neue_session, status, worker_token
from tests.test_verwaltung import admin  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


@pytest.fixture()
def _kleine_teile(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(64 * 1024))


def _worker(client, token, tmp_path, ereignisse):
    from app.worker_prozess import WorkerProzess, verarbeite_attrappe

    return WorkerProzess("http://testserver", token, tmp_path / "knecht", verarbeite_attrappe,
                         client=TestClient(client.app), claim_wait=0, info={"gpu": "Testkarte"},
                         melden=lambda art, **d: ereignisse.append(art))


def test_in_der_app_pausiert(client, world, dbs, tmp_path, admin, ntfy):  # noqa: F811
    from app import benachrichtigung, queue
    from app.db import utcnow
    from app.models import Job, Worker

    w = world
    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (6,)))
    ereignisse = []
    knecht = _worker(client, worker_token(dbs), tmp_path, ereignisse)
    knecht.pausieren(True)
    knecht.pause_melden()  # das macht die Schleife alle 30 s
    dbs.expire_all()
    wk = dbs.query(Worker).one()
    assert wk.app_paused_since is not None and "pausiert" not in (wk.info or "") and "Testkarte" in wk.info
    assert dbs.query(Job).one().state == "queued"  # nichts abgeholt
    assert not queue.worker_online(dbs) and queue.pausierte_worker(dbs) == [wk.name]
    assert "pausiert" in status(client, w["gm"], s["id"])["message"]
    seite = client.get("/verwaltung/transkription").text
    assert "in der App pausiert" in seite

    # Benachrichtigung: „pausiert“ statt „kein Worker erreichbar“
    einrichten(dbs, stunden="1")
    dbs.query(Job).one().created_at = utcnow() - timedelta(hours=2)
    dbs.commit()
    assert benachrichtigung.pruefen(dbs) == ["worker_pausiert"]
    assert wk.name in ntfy[-1]["message"] and "Fortsetzen" in ntfy[-1]["message"]

    # Externe Transkription: ein schon länger pausierter Worker zählt nicht als erreichbar
    from app.extern import worker_seit

    wk.app_paused_since = utcnow() - timedelta(hours=30)
    dbs.commit()
    assert not worker_seit(dbs, utcnow() - timedelta(hours=24))

    # Fortsetzen: der nächste normale Abruf hebt die Pause auf und holt den Auftrag
    knecht.pausieren(False)
    assert knecht.einen_auftrag()
    dbs.expire_all()
    assert dbs.query(Worker).one().app_paused_since is None
    assert ereignisse[:2] == ["pausiert", "fortgesetzt"]


def test_in_der_verwaltung_pausiert(client, world, dbs, tmp_path, admin):  # noqa: F811
    from app.models import Worker

    ereignisse = []
    knecht = _worker(client, worker_token(dbs), tmp_path, ereignisse)
    wid = dbs.query(Worker).one().id
    client.post(f"/verwaltung/worker/{wid}/pause", data={"csrf": admin})
    assert knecht.einen_auftrag() is False
    assert knecht.in_verwaltung_pausiert and ereignisse == ["server_pausiert"]
    knecht.einen_auftrag()
    assert ereignisse == ["server_pausiert"]  # nur Änderungen
    client.post(f"/verwaltung/worker/{wid}/fortsetzen", data={"csrf": admin})
    knecht.einen_auftrag()
    assert not knecht.in_verwaltung_pausiert and ereignisse == ["server_pausiert", "server_fortgesetzt"]
    # Auch ein in der App pausierter Worker erfährt es
    client.post(f"/verwaltung/worker/{wid}/pause", data={"csrf": admin})
    knecht.pause_melden()
    assert ereignisse[-1] == "server_pausiert"
