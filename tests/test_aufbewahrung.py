"""Aufbewahrung der Aufnahmen (Schnittstelle 0.4.4): bis zur Freigabe, höchstens 7 Tage – oder sofort."""
from datetime import timedelta

import pytest

from tests.test_step2a import API, audio_abschnitte, hochladen, neue_session, ordner, worker_starten, worker_token
from tests.test_step2b import transkribiert, zusammenfassen
from tests.test_verwaltung import admin  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")


@pytest.fixture()
def _kleine_teile(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(64 * 1024))


def _uploads(dbs, sid):
    from app.models import Upload

    dbs.expire_all()
    return dbs.query(Upload).filter_by(session_id=sid).all()


def test_neuer_server_behaelt_audio_bis_zur_freigabe(client, world, dbs, tmp_path):
    from app import storage

    w = world
    info = client.get(f"{API}/info").json()
    assert info["audioRetention"] == {"mode": "until_release", "maxDays": 7}

    s = transkribiert(client, w, dbs, tmp_path)
    sid = s["id"]
    (up,) = _uploads(dbs, sid)
    assert storage.upload_dir(up.id).exists()
    assert client.get(f"{API}/sessions/{sid}", headers=w["gm"]).json()["audioDeletedAt"] is None

    sprecher = client.get(f"{API}/sessions/{sid}/speakers", headers=w["gm"]).json()
    client.put(f"{API}/sessions/{sid}/speakers", headers=w["gm"],
               json=[{"speakerId": sprecher[0]["id"], "memberId": w["gm_member"]},
                     {"speakerId": sprecher[1]["id"], "memberId": w["pl_member"]}])
    assert zusammenfassen(dbs)
    assert storage.upload_dir(up.id).exists()  # während der Prüfung noch da
    r = client.post(f"{API}/sessions/{sid}/publish", headers=w["gm"])
    assert r.status_code == 200 and r.json()["audioDeletedAt"]
    assert not storage.upload_dir(up.id).exists()


def test_frist_greift_auch_ohne_freigabe(client, world, dbs, tmp_path):
    from app import aufbewahrung, storage
    from app.db import utcnow
    from app.models import GameSession
    from app.queue import sweep

    w = world
    aufbewahrung.speichern(dbs, "until_release", 3, None)  # kürzer: keine neue Zustimmung nötig
    dbs.commit()
    s = transkribiert(client, w, dbs, tmp_path)
    (up,) = _uploads(dbs, s["id"])
    up.completed_at = utcnow() - timedelta(days=2)
    dbs.commit()
    assert sweep(dbs)["audio"] == 0 and storage.upload_dir(up.id).exists()
    up.completed_at = utcnow() - timedelta(days=4)
    dbs.commit()
    assert sweep(dbs)["audio"] == 1 and not storage.upload_dir(up.id).exists()
    dbs.expire_all()
    assert dbs.get(GameSession, s["id"]).audio_deleted_at


def test_sofort_loescht_nach_transkription_und_beim_umstellen(client, world, dbs, tmp_path):
    from app import aufbewahrung, storage

    w = world
    s1 = transkribiert(client, w, dbs, tmp_path)  # noch „bis zur Freigabe“
    (up1,) = _uploads(dbs, s1["id"])
    assert storage.upload_dir(up1.id).exists()
    assert aufbewahrung.speichern(dbs, "immediate", 7, None) == 0  # Umstellen: vorhandenes Audio sofort weg
    dbs.commit()
    assert not storage.upload_dir(up1.id).exists()
    assert client.get(f"{API}/info").json()["audioRetention"] == {"mode": "immediate", "maxDays": None}

    s2 = neue_session(client, w)
    hochladen(client, w["gm"], s2["id"], audio_abschnitte(ordner(tmp_path, "zwei"), (6,)))
    assert worker_starten(client, worker_token(dbs, "zwei"), tmp_path).einen_auftrag()
    (up2,) = _uploads(dbs, s2["id"])
    assert not storage.upload_dir(up2.id).exists()
    assert client.get(f"{API}/sessions/{s2['id']}", headers=w["gm"]).json()["audioDeletedAt"]


def test_bestehender_server_startet_mit_sofort(client, world, dbs):
    from app import aufbewahrung
    from app.models import ServerMeta

    neue_session(client, world)
    dbs.delete(dbs.get(ServerMeta, aufbewahrung.K_MODUS))
    dbs.commit()
    aufbewahrung.festlegen(dbs)
    assert aufbewahrung.lesen(dbs).modus == "immediate"


def test_verlaengern_setzt_zustimmungen_zurueck(client, world, dbs, admin):  # noqa: F811
    from app import aufbewahrung
    from app.datenschutz import vorlage
    from app.db import utcnow
    from app.models import ConsentLog, Member

    aufbewahrung.speichern(dbs, "immediate", 7, None)
    for m in dbs.query(Member).all():
        m.recording_consent_at = utcnow()
    dbs.commit()
    assert "gelöscht, sobald sie in Text umgewandelt sind" in vorlage(dbs)
    seite = client.get("/verwaltung/einstellungen").text
    assert 'id="aufnahmen"' in seite and "zurzeit 2 Zustimmungen" in seite

    form = {"csrf": admin, "server_name": "Drachenhort", "server_operator": "Verein", "min_age": "16",
            "audio_modus": "until_release", "audio_tage": "7"}
    r = client.post("/verwaltung/einstellungen", data=form)
    assert r.status_code == 400 and "neu zustimmen" in r.text
    assert aufbewahrung.lesen(dbs).modus == "immediate"
    r = client.post("/verwaltung/einstellungen", data={**form, "audio_tage": "9", "audio_bestaetigt": "ja"})
    assert r.status_code == 400  # mehr als 7 Tage geht nicht

    r = client.post("/verwaltung/einstellungen", data={**form, "audio_bestaetigt": "ja"}, follow_redirects=False)
    assert r.status_code == 303 and "aufbewahrung_zustimmung" in r.headers["location"]
    dbs.expire_all()
    assert all(m.recording_consent_at is None for m in dbs.query(Member).all())
    assert dbs.query(ConsentLog).filter_by(action="reset").count() == 2
    assert "bis die Spielleitung den Recap freigibt, höchstens 7 Tage" in vorlage(dbs)
    konto = client.get(f"{API}/campaigns/{world['cid']}", headers=world["pl"]).json()
    assert all(m["recordingConsentAt"] is None for m in konto["members"])

    # Kürzer stellen: keine neue Zustimmung, kein Kästchen nötig
    m = dbs.query(Member).first()
    m.recording_consent_at = utcnow()
    dbs.commit()
    r = client.post("/verwaltung/einstellungen", data={**form, "audio_tage": "3"}, follow_redirects=False)
    assert r.status_code == 303 and "ok=aufbewahrung" in r.headers["location"]
    dbs.expire_all()
    assert dbs.get(Member, m.id).recording_consent_at is not None
    assert aufbewahrung.lesen(dbs).tage == 3
