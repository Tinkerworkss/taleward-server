"""Schritt 6: Stimmprofile – anlegen, wiedererkennen (voice_match), lernen, löschen."""
import json
import math

import pytest
from fastapi.testclient import TestClient

from tests.test_step2a import API, audio_abschnitte, hochladen, neue_session, worker_token


@pytest.fixture(autouse=True)
def _kleine_teile(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(64 * 1024))


def knecht(client, dbs, tmp_path, verarbeite=None):
    from app.worker_prozess import WorkerProzess, verarbeite_attrappe

    return WorkerProzess("http://testserver", worker_token(dbs), tmp_path / "k", verarbeite or verarbeite_attrappe,
                        client=TestClient(client.app), claim_wait=0)


def aufnahme(tmp_path, sekunden=25):
    return audio_abschnitte(tmp_path, (sekunden,))[0].read_bytes()


def anlegen(client, h, daten, **form):
    return client.post(f"{API}/me/voice-profile", headers=h,
                       files={"audio": ("stimme.m4a", daten, "audio/mp4")},
                       data={"consent": "true", **form})


def vektor(richtung: int, dim: int = 32, rauschen: float = 0.0) -> list[float]:
    v = [0.0] * dim
    v[richtung] = 1.0
    v[(richtung + 1) % dim] = rauschen
    return v


def test_anlegen_und_pruefungen(client, world, dbs, tmp_path, monkeypatch):
    from app import stimmprofile
    from app.models import VoiceProfile

    w = world
    daten = aufnahme(tmp_path)
    r = client.post(f"{API}/me/voice-profile", headers=w["pl"], files={"audio": ("s.m4a", daten, "audio/mp4")})
    assert r.status_code == 400 and r.json()["code"] == "voice_consent_missing"
    r = client.post(f"{API}/me/voice-profile", headers=w["pl"], files={"audio": ("s.pdf", b"%PDF", "application/pdf")},
                    data={"consent": "true"})
    assert r.json()["code"] == "voice_unsupported"
    (tmp_path / "kurz").mkdir()
    kurz = audio_abschnitte(tmp_path / "kurz", (5,))[0].read_bytes()
    r = anlegen(client, w["pl"], kurz)
    assert r.status_code == 400 and "5 Sekunden" in r.json()["message"]
    monkeypatch.setattr(stimmprofile, "MAX_BYTES", 1000)
    assert anlegen(client, w["pl"], daten).status_code == 413
    monkeypatch.undo()
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(64 * 1024))

    r = anlegen(client, w["pl"], daten, learnFromSessions="true")
    assert r.status_code == 202 and r.json()["status"] == "processing" and r.json()["learnFromSessions"] is True
    assert "keine Transkription verfügbar" in client.get(f"{API}/me/voice-profile", headers=w["pl"]).json()["message"]
    assert anlegen(client, w["pl"], daten).json()["code"] == "voice_processing"
    audio_dateien = list(stimmprofile.audio_pfad("x").parent.glob("*.audio"))
    assert len(audio_dateien) == 1

    assert knecht(client, dbs, tmp_path).einen_auftrag()
    vp = client.get(f"{API}/me/voice-profile", headers=w["pl"]).json()
    assert vp["status"] == "ready" and 24 <= vp["sampleSeconds"] <= 26 and vp["createdAt"] and vp["message"] is None
    assert not any(stimmprofile.audio_pfad("x").parent.glob("*.audio"))  # Audio ist weg
    dbs.expire_all()
    gespeichert = dbs.get(VoiceProfile, dbs.query(VoiceProfile).one().user_id)
    assert len(json.loads(gespeichert.base_embedding)) == 32 and gespeichert.consent_at


def test_zu_wenig_sprache_und_fehlschlag(client, world, dbs, tmp_path):
    from app.transkription import verarbeiter

    class StilleMotor:
        modell = "test"

        def audio_laden(self, wav):
            return wav

        def stimmabdruck(self, daten, fortschritt):
            return vektor(0), 4.0  # nur 4 s Sprache erkannt

    w = world
    anlegen(client, w["pl"], aufnahme(tmp_path))
    knecht(client, dbs, tmp_path, verarbeiter(StilleMotor())).einen_auftrag()
    vp = client.get(f"{API}/me/voice-profile", headers=w["pl"]).json()
    assert vp["status"] == "failed" and "4 Sekunden Sprache" in vp["message"]
    # Endgültiger Fehlschlag des Workers
    (tmp_path / "b").mkdir()
    anlegen(client, w["gm"], aufnahme(tmp_path / "b"))
    wc = TestClient(client.app)
    t = worker_token(dbs, "pc-x")
    a = wc.post("/worker/v1/jobs/claim", json={"waitSeconds": 0}, headers={"Authorization": f"Bearer {t}"}).json()
    assert a["type"] == "voice_enroll" and "session" not in a  # keine Angaben zur Person
    wc.post(f"/worker/v1/jobs/{a['jobId']}/fail", headers={"Authorization": f"Bearer {t}"},
            json={"code": "audio_unreadable", "message": "Datei defekt", "retryable": False})
    vp = client.get(f"{API}/me/voice-profile", headers=w["gm"]).json()
    assert vp["status"] == "failed" and "Datei defekt" in vp["message"]


# ---------------------------------------------------------------- Wiedererkennen und Lernen
def profil_setzen(dbs, member_id, v, lernen=True):
    from app.db import utcnow
    from app.models import Member, VoiceProfile

    m = dbs.get(Member, member_id)
    dbs.add(VoiceProfile(user_id=m.user_id, status="ready", base_embedding=json.dumps(v), learn_from_sessions=lernen,
                         learned_session_count=0, created_at=utcnow(), sample_seconds=25))
    dbs.commit()


class StimmMotor:
    """Zwei Stimmen ohne Vorstellungsrunde; die Abdrücke liegen nahe an den Profilen."""

    modell = "test"

    def audio_laden(self, wav):
        from app import audio
        return audio.dauer(wav)

    def transkribieren(self, dauer, sprache, hotwords, fortschritt):
        return [{"start": t, "end": t + 8, "text": f" Satz {i}."} for i, t in enumerate(range(0, int(dauer) - 8, 10))]

    def ausrichten(self, segmente, dauer, sprache, fortschritt):
        return segmente

    def sprecher_trennen(self, dauer, segmente, min_n, max_n, fortschritt):
        for i, s in enumerate(segmente):
            s["speaker"] = "SPEAKER_00" if i % 3 else "SPEAKER_01"  # 00 redet mehr
        return segmente, {"SPEAKER_00": vektor(3, rauschen=0.3), "SPEAKER_01": vektor(7, rauschen=0.2)}


def transkribierte_session(client, w, dbs, tmp_path, sekunden=140):
    from app.transkription import verarbeiter

    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (sekunden,)))
    assert knecht(client, dbs, tmp_path, verarbeiter(StimmMotor())).einen_auftrag()
    return s


def test_wiedererkennen(client, world, dbs, tmp_path):
    w = world
    profil_setzen(dbs, w["pl_member"], vektor(3))
    profil_setzen(dbs, w["gm_member"], vektor(7))
    s = transkribierte_session(client, w, dbs, tmp_path)
    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    vorschlag = {sp["label"]: (sp["suggestedMemberId"], sp["source"], sp["confidence"]) for sp in sprecher}
    assert vorschlag["Stimme 1"][:2] == (w["pl_member"], "voice_match")
    assert vorschlag["Stimme 2"][:2] == (w["gm_member"], "voice_match")
    assert all(c >= 0.8 for _, _, c in vorschlag.values())


def test_nur_profile_von_anwesenden(client, world, dbs, tmp_path, make_user, login):
    w = world
    # cleo ist in keiner Session – ihr Profil darf nie verglichen werden
    code = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    client.post(f"{API}/campaigns/join", json={"code": code}, headers=w["out"])
    from app.models import Member, User
    cleo = dbs.query(Member).join(User, Member.user_id == User.id).filter(User.username == "cleo").one()
    profil_setzen(dbs, cleo.id, vektor(3))
    s = transkribierte_session(client, w, dbs, tmp_path)
    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    assert all(sp["suggestedMemberId"] != cleo.id for sp in sprecher)
    assert all(sp["source"] == "none" for sp in sprecher)


def test_lernen_und_loeschen(client, world, dbs, tmp_path):
    from app import stimmprofile
    from app.models import VoiceProfile

    w = world
    profil_setzen(dbs, w["pl_member"], vektor(3), lernen=True)
    profil_setzen(dbs, w["gm_member"], vektor(7), lernen=False)
    s = transkribierte_session(client, w, dbs, tmp_path)
    assert client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[]).status_code == 202
    ben = client.get(f"{API}/me/voice-profile", headers=w["pl"]).json()
    anna = client.get(f"{API}/me/voice-profile", headers=w["gm"]).json()
    assert ben["learnedSessionCount"] == 1  # 60 s Redezeit erreicht, Profil stimmt überein
    assert anna["learnedSessionCount"] == 0  # „aus Sessions lernen“ aus (und zu wenig Redezeit)
    dbs.expire_all()
    vp = dbs.query(VoiceProfile).filter(VoiceProfile.learn_from_sessions.is_(True)).one()
    ben_user = vp.user_id
    neu = stimmprofile.effektiv(vp)
    assert neu != vektor(3) and stimmprofile.kosinus(neu, vektor(3)) > 0.95  # leicht angepasst
    # Löschen entfernt alles
    assert client.delete(f"{API}/me/voice-profile", headers=w["pl"]).status_code == 204
    assert client.get(f"{API}/me/voice-profile", headers=w["pl"]).json() == {
        "status": "none", "createdAt": None, "sampleSeconds": None, "learnFromSessions": False,
        "learnedSessionCount": 0, "message": None}
    dbs.expire_all()
    assert dbs.get(VoiceProfile, ben_user) is None


def test_nicht_aus_falsch_bestaetigter_stimme_lernen(client, world, dbs, tmp_path):
    w = world
    profil_setzen(dbs, w["pl_member"], vektor(3), lernen=True)
    s = transkribierte_session(client, w, dbs, tmp_path)
    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    # SL ordnet die längere Stimme (Abdruck ≈ Profil) der SL zu und die andere Ben – passt nicht zum Profil
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[
        {"speakerId": sprecher[0]["id"], "memberId": w["gm_member"]},
        {"speakerId": sprecher[1]["id"], "memberId": w["pl_member"]}])
    assert client.get(f"{API}/me/voice-profile", headers=w["pl"]).json()["learnedSessionCount"] == 0


def test_loeschen_waehrend_der_verarbeitung(client, world, dbs, tmp_path):
    from app import stimmprofile

    w = world
    anlegen(client, w["pl"], aufnahme(tmp_path))
    wc = TestClient(client.app)
    t = worker_token(dbs)
    a = wc.post("/worker/v1/jobs/claim", json={"waitSeconds": 0}, headers={"Authorization": f"Bearer {t}"}).json()
    client.delete(f"{API}/me/voice-profile", headers=w["pl"])
    assert not any(stimmprofile.audio_pfad("x").parent.glob("*.audio"))
    r = wc.post(f"/worker/v1/jobs/{a['jobId']}/voice-result", headers={"Authorization": f"Bearer {t}"},
                json={"embedding": vektor(1), "speechSeconds": 20, "audioSeconds": 25})
    assert r.status_code == 409  # Auftrag abgebrochen – Abdruck wird verworfen
    assert client.get(f"{API}/me/voice-profile", headers=w["pl"]).json()["status"] == "none"


def test_vektoren():
    from app.stimmprofile import kosinus, sicherheit

    assert math.isclose(kosinus([1, 0], [2, 0]), 1.0) and kosinus([1, 0], [0, 1]) == 0.0
    assert kosinus([1, 0], [1, 0, 0]) == 0.0  # andere Modelle/Größen werden nie verglichen
    assert sicherheit(0.3) == 0.0 and sicherheit(0.9) == 0.95 and 0.4 < sicherheit(0.56) < 0.6
