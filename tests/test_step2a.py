"""Schritt 2a: Upload, Warteschlange, Worker-Protokoll, Worker (Attrappe), Wartung."""
import hashlib
import shutil
import subprocess
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

API = "/api/v1"
CHUNK = 64 * 1024
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg fehlt")


@pytest.fixture(autouse=True)
def _kleine_teile(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(CHUNK))


# ---------- Hilfen ----------
def ordner(basis, name):
    (basis / name).mkdir(exist_ok=True)
    return basis / name


def audio_abschnitte(tmp_path, sekunden=(25, 25, 20), freq=440) -> list:
    """Echte AAC/M4A-Abschnitte wie von der Android-App."""
    dateien = []
    for i, dauer in enumerate(sekunden):
        pfad = tmp_path / f"teil-{i + 1:03d}.m4a"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency={freq + 110 * i}:duration={dauer}", "-ac", "1", "-ar", "32000",
                        "-c:a", "aac", "-b:a", "48k", str(pfad)], check=True)
        dateien.append(pfad)
    return dateien


def neue_session(client, w, mit_ben=True):
    att = [{"memberId": w["gm_member"], "consent": True, "consentSource": "on_site"}]
    if mit_ben:
        att.append({"memberId": w["pl_member"], "consent": True, "consentSource": "on_site"})
    r = client.post(f"{API}/campaigns/{w['cid']}/sessions", json={"playedAt": "2026-09-20T18:00:00Z",
                                                                   "attendees": att}, headers=w["gm"])
    assert r.status_code == 201, r.text
    return r.json()


def hochladen(client, h, session_id, dateien, source="table", tracks=None, abschliessen=True):
    files = [{"fileName": d.name, "sizeBytes": d.stat().st_size, "mimeType": "audio/mp4",
              **({"trackMemberId": tracks[i]} if tracks else {})} for i, d in enumerate(dateien)]
    r = client.post(f"{API}/sessions/{session_id}/uploads", json={"source": source, "files": files}, headers=h)
    assert r.status_code == 201, r.text
    up = r.json()
    for f, d in zip(up["files"], dateien):
        daten = d.read_bytes()
        for i in range(f["chunkCount"]):
            teil = daten[i * CHUNK:(i + 1) * CHUNK]
            r = client.put(f"{API}/uploads/{up['uploadId']}/files/{f['fileId']}/chunks/{i}", content=teil,
                           headers={**h, "X-Chunk-SHA256": hashlib.sha256(teil).hexdigest()})
            assert r.status_code == 204, r.text
    if abschliessen:
        r = client.post(f"{API}/uploads/{up['uploadId']}/complete", headers=h)
        assert r.status_code == 202, r.text
    return up


def worker_token(dbs, name="heim-pc"):
    import secrets

    from app.models import Worker
    from app.routers.worker import token_hash

    geheim = secrets.token_urlsafe(16)
    w = Worker(name=name, token_hash=token_hash(geheim), capabilities="asr")
    dbs.add(w)
    dbs.commit()
    return f"wk.{w.id}.{geheim}"


def worker_starten(client, token, tmp_path, name="knecht"):
    from app.worker_prozess import WorkerProzess, verarbeite_attrappe

    return WorkerProzess("http://testserver", token, tmp_path / name, verarbeite_attrappe,
                        client=TestClient(client.app), claim_wait=0)


def status(client, h, sid, lang="de"):
    return client.get(f"{API}/sessions/{sid}/status", headers={**h, "Accept-Language": lang}).json()


# ---------- Ende-zu-Ende ----------
def test_tischaufnahme_von_upload_bis_stimmen(client, world, dbs, tmp_path):
    from app import storage
    from app.models import Upload

    w = world
    s = neue_session(client, w)
    dateien = audio_abschnitte(tmp_path)
    up = hochladen(client, w["gm"], s["id"], dateien, abschliessen=False)
    # Fortsetzen nach Abbruch: gleicher Start liefert denselben Upload
    files = [{"fileName": d.name, "sizeBytes": d.stat().st_size, "mimeType": "audio/mp4"} for d in dateien]
    r = client.post(f"{API}/sessions/{s['id']}/uploads", json={"source": "table", "files": files}, headers=w["gm"])
    assert r.json()["uploadId"] == up["uploadId"]
    fehlend = client.get(f"{API}/uploads/{up['uploadId']}", headers=w["gm"]).json()
    assert all(f["missingChunks"] == [] for f in fehlend["files"])
    r = client.post(f"{API}/uploads/{up['uploadId']}/complete", headers=w["gm"])
    assert r.json()["state"] == "queued" and r.json()["queuePosition"] == 1
    # Noch kein Worker verbunden → verständlicher Hinweis
    assert "keine Transkription verfügbar" in status(client, w["gm"], s["id"])["message"]
    assert "Transcription is not available" in status(client, w["gm"], s["id"], "en")["message"]

    knecht = worker_starten(client, worker_token(dbs), tmp_path)
    assert knecht.einen_auftrag() is True
    st = status(client, w["gm"], s["id"])
    assert st["state"] == "awaiting_speakers" and st["message"] is None

    sess = client.get(f"{API}/sessions/{s['id']}", headers=w["gm"]).json()
    assert 69 <= sess["durationSeconds"] <= 71 and sess["transcriptionEngine"] == "local"
    # Neuer Server: Audio bleibt bis zur Freigabe des Recaps (höchstens 7 Tage) – siehe test_aufbewahrung.py
    assert sess["audioDeletedAt"] is None and storage.upload_dir(up["uploadId"]).exists()
    assert dbs.get(Upload, up["uploadId"]).state == "completed"

    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    assert [x["label"] for x in sprecher] == ["Stimme 1", "Stimme 2"] and sprecher[0]["source"] == "none"
    probe = client.get(f"{API}/sessions/{s['id']}/speakers/{sprecher[0]['id']}/sample", headers=w["gm"])
    assert probe.status_code == 200 and probe.content[:4] == b"OggS"
    assert client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["pl"]).status_code == 404
    zeilen = client.get(f"{API}/sessions/{s['id']}/transcript", headers=w["gm"]).json()
    assert len(zeilen) >= 3 and zeilen[0]["text"].startswith("Platzhalter")

    usage = client.get(f"{API}/campaigns/{w['cid']}/usage", headers=w["gm"]).json()
    assert usage["sessions"] == 1 and 69 <= usage["audioSeconds"] <= 71
    assert not any((tmp_path / "knecht").iterdir())  # Worker hat aufgeräumt


def test_discord_spuren(client, world, dbs, tmp_path):
    w = world
    s = neue_session(client, w)
    dateien = audio_abschnitte(tmp_path, (30, 28))
    # Spur ohne anwesendes Mitglied wird abgelehnt
    bad = [{"fileName": "x.ogg", "sizeBytes": 10, "mimeType": "audio/ogg", "trackMemberId": "fremd"}]
    r = client.post(f"{API}/sessions/{s['id']}/uploads", json={"source": "discord", "files": bad}, headers=w["gm"])
    assert r.status_code == 400 and r.json()["code"] == "track_member_invalid"
    hochladen(client, w["gm"], s["id"], dateien, "discord", [w["gm_member"], w["pl_member"]])
    assert worker_starten(client, worker_token(dbs), tmp_path).einen_auftrag()
    assert status(client, w["gm"], s["id"])["state"] == "summarizing"
    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    assert {x["suggestedMemberId"] for x in sprecher} == {w["gm_member"], w["pl_member"]}
    assert all(x["source"] == "discord_track" and x["confidence"] == 1 for x in sprecher)
    zeilen = client.get(f"{API}/sessions/{s['id']}/transcript", headers=w["gm"]).json()
    assert {z["memberId"] for z in zeilen} == {w["gm_member"], w["pl_member"]}


# ---------- Upload-Prüfungen ----------
def test_upload_pruefungen(client, world, tmp_path):
    w = world
    s = neue_session(client, w)
    dateien = audio_abschnitte(tmp_path, (10,))
    size = dateien[0].stat().st_size
    base = f"{API}/sessions/{s['id']}/uploads"
    r = client.post(base, json={"source": "table", "files": [{"fileName": "x.pdf", "sizeBytes": 5,
                                                                "mimeType": "application/pdf"}]}, headers=w["gm"])
    assert r.status_code == 400 and r.json()["code"] == "unsupported_audio"
    r = client.post(base, json={"source": "table", "files": [{"fileName": "a.m4a", "sizeBytes": 10**12,
                                                                "mimeType": "audio/mp4"}]}, headers=w["gm"])
    assert r.status_code == 413
    up = hochladen(client, w["gm"], s["id"], dateien, abschliessen=False)
    f = up["files"][0]
    url = f"{API}/uploads/{up['uploadId']}/files/{f['fileId']}/chunks"
    assert client.put(f"{url}/0", content=b"zu kurz", headers=w["gm"]).json()["code"] == "chunk_size_mismatch"
    teil = dateien[0].read_bytes()[:CHUNK]
    r = client.put(f"{url}/0", content=teil, headers={**w["gm"], "X-Chunk-SHA256": "0" * 64})
    assert r.json()["code"] == "chunk_checksum_mismatch"
    assert client.put(f"{url}/{f['chunkCount']}", content=teil, headers=w["gm"]).json()["code"] == "chunk_index_invalid"
    # anderer Upload während einer offen ist → 409
    r = client.post(base, json={"source": "table", "files": [{"fileName": "b.m4a", "sizeBytes": size,
                                                                "mimeType": "audio/mp4"}]}, headers=w["gm"])
    assert r.json()["code"] == "upload_in_progress"
    # Teil löschen → Abschluss meldet fehlende Teile
    from app import storage
    storage.chunk_path(up["uploadId"], f["fileId"], 0).unlink()
    r = client.post(f"{API}/uploads/{up['uploadId']}/complete", headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "chunks_missing"
    # Spieler sehen fremde Uploads nicht
    assert client.get(f"{API}/uploads/{up['uploadId']}", headers=w["pl"]).status_code == 404


# ---------- Leases, mehrere Worker, Fehlschläge ----------
def _claim(wc, token):
    return wc.post("/worker/v1/jobs/claim", json={"capabilities": ["asr"], "waitSeconds": 0},
                   headers={"Authorization": f"Bearer {token}"})


def test_lease_ablauf_und_zweiter_worker_starten(client, world, dbs, tmp_path):
    from app.db import utcnow
    from app.models import Job
    from app.queue import sweep

    w = world
    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (12,)))
    wc = TestClient(client.app)
    t1, t2 = worker_token(dbs, "pc-1"), worker_token(dbs, "pc-2")
    auftrag = _claim(wc, t1).json()
    assert status(client, w["gm"], s["id"])["state"] == "transcribing"
    assert _claim(wc, t2).status_code == 204  # nichts mehr frei
    # pc-1 meldet sich nicht mehr → Lease läuft ab → Auftrag zurück in die Schlange
    job = dbs.get(Job, auftrag["jobId"])
    job.lease_expires_at = utcnow() - timedelta(seconds=1)
    dbs.commit()
    assert sweep(dbs)["leases"] == 1
    dbs.expire_all()
    assert dbs.get(Job, auftrag["jobId"]).state == "queued"
    assert status(client, w["gm"], s["id"])["state"] == "queued"
    # pc-2 übernimmt und liefert
    k2 = worker_starten(client, t2, tmp_path, "pc2")
    assert k2.einen_auftrag()
    assert status(client, w["gm"], s["id"])["state"] == "awaiting_speakers"
    # verspätetes Ergebnis von pc-1 wird abgelehnt
    r = wc.post(f"/worker/v1/jobs/{auftrag['jobId']}/result", headers={"Authorization": f"Bearer {t1}"},
                json={"audioSeconds": 1, "computeSeconds": 1, "segments": [], "speakers": []})
    assert r.status_code == 409


def test_zwei_worker_arbeiten_parallel(client, world, dbs, tmp_path):
    w = world
    s1, s2 = neue_session(client, w), neue_session(client, w)
    hochladen(client, w["gm"], s1["id"], audio_abschnitte(ordner(tmp_path, "a"), (8,)))
    hochladen(client, w["gm"], s2["id"], audio_abschnitte(ordner(tmp_path, "b"), (8,)))
    assert status(client, w["gm"], s2["id"])["queuePosition"] == 2
    wc = TestClient(client.app)
    a = _claim(wc, worker_token(dbs, "pc-1")).json()
    b = _claim(wc, worker_token(dbs, "pc-2")).json()
    assert a["jobId"] != b["jobId"]
    assert {status(client, w["gm"], x["id"])["state"] for x in (s1, s2)} == {"transcribing"}


def test_fehlschlag_und_neustart(client, world, dbs, tmp_path):
    from app import storage
    from app.models import Job

    w = world
    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (8,)))
    wc = TestClient(client.app)
    t = worker_token(dbs)
    auftrag = _claim(wc, t).json()
    r = wc.post(f"/worker/v1/jobs/{auftrag['jobId']}/fail", headers={"Authorization": f"Bearer {t}"},
                json={"code": "audio_unreadable", "message": "Abschnitt 1 ist nicht lesbar", "retryable": False})
    assert r.status_code == 204
    st = status(client, w["gm"], s["id"])
    assert st["state"] == "failed" and "Abschnitt 1 ist nicht lesbar" in st["message"]
    assert status(client, w["gm"], s["id"], "en")["message"].startswith("Transcription failed")
    # Neustart: Audio ist noch da → wieder in der Schlange
    r = client.post(f"{API}/sessions/{s['id']}/retry", headers=w["gm"])
    assert r.status_code == 202 and r.json()["state"] == "queued"
    assert worker_starten(client, t, tmp_path).einen_auftrag()
    assert status(client, w["gm"], s["id"])["state"] == "awaiting_speakers"
    # Vorübergehende Fehler: neu einreihen, nach 3 Versuchen aufgeben
    s2 = neue_session(client, w)
    hochladen(client, w["gm"], s2["id"], audio_abschnitte(ordner(tmp_path, "c"), (8,)))
    for versuch in range(3):
        a = _claim(wc, t).json()
        wc.post(f"/worker/v1/jobs/{a['jobId']}/fail", headers={"Authorization": f"Bearer {t}"},
                json={"code": "cuda_oom", "message": "Grafikspeicher voll", "retryable": True})
    assert status(client, w["gm"], s2["id"])["state"] == "failed"
    assert dbs.get(Job, a["jobId"]).attempts == 3
    # Audio weg → Neustart nicht möglich
    for u in storage.uploads_root().iterdir():
        shutil.rmtree(u)
    r = client.post(f"{API}/sessions/{s2['id']}/retry", headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "audio_gone"


def test_worker_token_pruefung(client, dbs):
    from app.db import utcnow
    from app.models import Worker

    wc = TestClient(client.app)
    assert _claim(wc, "wk.x.y").status_code == 401
    t = worker_token(dbs)
    assert _claim(wc, t).status_code == 204
    w = dbs.get(Worker, t.split(".")[1])
    w.revoked_at = utcnow()
    dbs.commit()
    assert _claim(wc, t).status_code == 401
    # Nutzer-Token öffnet das Worker-Protokoll nicht und umgekehrt
    assert wc.get(f"{API}/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_worker_bricht_bei_verlorener_lease_ab(client, world, dbs, tmp_path):
    from app.models import Job
    from app.worker_prozess import Abgebrochen, WorkerProzess

    w = world
    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (8,)))

    def verarbeite(auftrag, dateien, arbeit, fortschritt):
        job = dbs.get(Job, auftrag["jobId"])
        job.lease_worker_id = "jemand-anderes"  # simuliert: Auftrag wurde inzwischen neu vergeben
        dbs.commit()
        raise Abgebrochen()

    knecht = WorkerProzess("http://testserver", worker_token(dbs), tmp_path / "k", verarbeite,
                          client=TestClient(client.app), claim_wait=0)
    assert knecht.einen_auftrag()
    assert not any((tmp_path / "k").iterdir())


# ---------- Wartung ----------
def test_wartung_loescht_audio_nach_frist_und_verwaiste_uploads(client, world, dbs, tmp_path):
    from app import storage
    from app.db import utcnow
    from app.models import GameSession, Upload
    from app.queue import sweep

    w = world
    s1 = neue_session(client, w)
    up1 = hochladen(client, w["gm"], s1["id"], audio_abschnitte(ordner(tmp_path, "a"), (6,)))
    s2 = neue_session(client, w)
    up2 = hochladen(client, w["gm"], s2["id"], audio_abschnitte(ordner(tmp_path, "b"), (6,)),
                    abschliessen=False)
    alt = utcnow() - timedelta(days=8)
    dbs.get(Upload, up1["uploadId"]).completed_at = alt
    dbs.get(Upload, up2["uploadId"]).created_at = alt
    dbs.commit()
    ergebnis = sweep(dbs)
    assert ergebnis["audio"] == 1 and ergebnis["uploads"] == 1
    dbs.expire_all()
    assert not storage.upload_dir(up1["uploadId"]).exists() and dbs.get(GameSession, s1["id"]).audio_deleted_at
    assert dbs.get(GameSession, s2["id"]).state == "created"


# ---------- Namenshilfe ----------
def test_namenshilfe(client, world, dbs):
    from app.models import Campaign
    from app.namenshilfe import fuer_kampagne

    w = world
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"],
                 json={"characterName": "Jemma Reed", "characterBackstory": "Geheime Tochter des Königs"})
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"],
                json={"type": "npc", "name": "Der Graue Fürst", "gmNotes": "Verräter"})
    client.patch(f"{API}/campaigns/{w['cid']}", json={"system": "dsa"}, headers=w["gm"])
    dbs.expire_all()
    namen = fuer_kampagne(dbs, dbs.get(Campaign, w["cid"]))
    assert {"Anna", "Jemma Reed"} <= set(namen) and "Der Graue Fürst" in namen and "Aventurien" in namen
    assert not any("Tochter" in n or "Verräter" in n for n in namen)
    assert sum(len(n) + 2 for n in namen) <= 700
    client.patch(f"{API}/campaigns/{w['cid']}", json={"system": "other", "systemName": "Household"}, headers=w["gm"])
    dbs.expire_all()
    namen = fuer_kampagne(dbs, dbs.get(Campaign, w["cid"]))
    assert "Household" in namen and "Aventurien" not in namen
