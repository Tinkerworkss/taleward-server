"""Externe Transkription (Mistral Voxtral) als Ersatz – mit nachgebautem Anbieter."""
from datetime import timedelta

import httpx
import pytest

from tests.test_step2a import API, audio_abschnitte, hochladen, neue_session, status, worker_token


@pytest.fixture(autouse=True)
def _einstellungen(client, monkeypatch):
    from app import config

    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(64 * 1024))
    monkeypatch.setenv("EXTERNAL_TRANSCRIPTION", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-schluessel")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


class Anbieter:
    """Antwortet wie POST /v1/audio/transcriptions (Format aus dem Mistral-SDK)."""

    def __init__(self, status: int = 200):
        self.status, self.anfragen = status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "nope"})
        body = request.read()
        diarize = b'name="diarize"\r\n\r\ntrue' in body
        segs = [{"text": " Ich bin Anna, ich leite heute.", "start": 1.0, "end": 6.0, "speaker_id": "speaker_0"},
                {"text": " Und ich spiele Mira.", "start": 7.0, "end": 12.0, "speaker_id": "speaker_1"},
                {"text": " Musik Musik", "start": 13.0, "end": 14.0, "speaker_id": None},
                {"text": " Wir gehen in die Taverne.", "start": 15.0, "end": 19.0, "speaker_id": "speaker_0"}]
        if not diarize:
            for s in segs:
                s.pop("speaker_id")
        return httpx.Response(200, json={"model": "voxtral-mini-2602", "text": "…", "language": "de",
                                         "segments": segs, "usage": {"prompt_audio_seconds": 20}})


def klient(anbieter, dbs=None):
    from app.einstellungen import ExternKonfig
    from app.extern import MistralKlient

    k = ExternKonfig("mistral", "mistral", "test-schluessel", 24, "voxtral-mini-latest",
                     "https://api.mistral.ai/v1/audio/transcriptions", 9000, 0.3, "env")
    return MistralKlient(k, httpx.Client(transport=httpx.MockTransport(anbieter)))


def wartende_session(client, w, dbs, tmp_path, erlauben=True, stunden=25, source="table"):
    from app.models import Job

    client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={"allowExternalTranscription": erlauben})
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"],
                 json={"characterName": "Mira"})
    s = neue_session(client, w)
    if source == "table":
        hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (25,)))
    else:
        hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (20, 22)), "discord",
                  [w["gm_member"], w["pl_member"]])
    job = dbs.query(Job).filter_by(session_id=s["id"]).one()
    job.created_at = job.created_at - timedelta(hours=stunden)
    dbs.commit()
    return s


def test_info_und_statushinweis(client, world, dbs, tmp_path):
    info = client.get(f"{API}/info").json()
    assert info["externalTranscription"] == "mistral"
    assert info["externalTranscriptionInfo"] == {"id": "mistral", "name": "Mistral AI", "region": "eu", "country": "FR"}
    s = wartende_session(client, world, dbs, tmp_path, stunden=1)
    msg = status(client, world["gm"], s["id"])["message"]
    assert "Mistral" in msg and "Uhr" in msg


def test_tischaufnahme_extern(client, world, dbs, tmp_path):
    from app.extern import einen_auftrag
    from app.models import UsageLog

    w = world
    s = wartende_session(client, w, dbs, tmp_path)
    anbieter = Anbieter()
    assert einen_auftrag(dbs, klient(anbieter))
    anfrage = anbieter.anfragen[0]
    assert anfrage.headers["authorization"] == "Bearer test-schluessel"
    roh = anfrage.read()
    for teil in (b'name="model"\r\n\r\nvoxtral-mini-latest', b'name="diarize"\r\n\r\ntrue',
                 b'name="timestamp_granularities"\r\n\r\nsegment', b'name="context_bias"\r\n\r\nMira'):
        assert teil in roh
    st = status(client, w["gm"], s["id"])
    assert st["state"] == "awaiting_speakers"
    sess = client.get(f"{API}/sessions/{s['id']}", headers=w["gm"]).json()
    assert sess["transcriptionEngine"] == "external" and sess["audioDeletedAt"] is None  # bis zur Freigabe
    zeilen = client.get(f"{API}/sessions/{s['id']}/transcript", headers=w["gm"]).json()
    assert [z["text"] for z in zeilen] == ["Ich bin Anna, ich leite heute.", "Und ich spiele Mira.",
                                           "Wir gehen in die Taverne."]  # „Musik Musik“ entfernt
    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    assert len(sprecher) == 2  # speaker_1 mit 5 s bleibt knapp als Stimme
    assert {sp["suggestedMemberId"] for sp in sprecher} == {w["gm_member"], w["pl_member"]}  # Vorstellungsrunde
    assert all(sp["source"] == "intro_round" for sp in sprecher)
    log = dbs.query(UsageLog).filter_by(session_id=s["id"]).one()
    assert log.engine == "external" and log.cost_cents == 1 and log.model == "mistral/voxtral-mini-latest"


def test_discord_extern_ohne_sprechertrennung(client, world, dbs, tmp_path):
    from app.extern import einen_auftrag

    w = world
    s = wartende_session(client, w, dbs, tmp_path, source="discord")
    anbieter = Anbieter()
    assert einen_auftrag(dbs, klient(anbieter))
    assert len(anbieter.anfragen) == 2 and all(b'name="diarize"\r\n\r\nfalse' in a.read() for a in anbieter.anfragen)
    assert status(client, w["gm"], s["id"])["state"] == "summarizing"
    zeilen = client.get(f"{API}/sessions/{s['id']}/transcript", headers=w["gm"]).json()
    assert {z["memberId"] for z in zeilen} == {w["gm_member"], w["pl_member"]}


def test_lange_aufnahmen_werden_geteilt(client, world, dbs, tmp_path, monkeypatch):
    from app import config
    from app.extern import einen_auftrag

    monkeypatch.setenv("EXTERNAL_MAX_SECONDS", "10")
    config.get_settings.cache_clear()
    s = wartende_session(client, world, dbs, tmp_path)  # 25 s → 3 Teile
    anbieter = Anbieter()
    assert einen_auftrag(dbs, klient(anbieter))
    assert len(anbieter.anfragen) == 3
    starts = [z["start"] for z in client.get(f"{API}/sessions/{s['id']}/transcript", headers=world["gm"]).json()]
    assert max(starts) >= 20  # Versatz der Teile eingerechnet


@pytest.mark.parametrize("fall", ["nicht_erlaubt", "zu_frueh", "worker_da", "nicht_freigegeben"])
def test_greift_nur_wenn_alles_passt(client, world, dbs, tmp_path, monkeypatch, fall):
    from app import config
    from app.extern import einen_auftrag

    s = wartende_session(client, world, dbs, tmp_path, erlauben=(fall != "nicht_erlaubt"),
                         stunden=(1 if fall == "zu_frueh" else 25))
    if fall == "worker_da":
        from fastapi.testclient import TestClient
        from app.models import Job, Worker
        t = worker_token(dbs)
        # Worker war nach dem Upload da (hat den Auftrag aber nicht geschafft, z. B. lange Schlange)
        TestClient(client.app).post("/worker/v1/jobs/claim", json={"capabilities": [], "waitSeconds": 0},
                                    headers={"Authorization": f"Bearer {t}"})
        dbs.expire_all()
        assert dbs.get(Worker, t.split(".")[1]).last_seen_at > dbs.query(Job).one().created_at
    if fall == "nicht_freigegeben":
        monkeypatch.delenv("MISTRAL_API_KEY")
        config.get_settings.cache_clear()
    anbieter = Anbieter()
    assert not einen_auftrag(dbs, klient(anbieter)) and not anbieter.anfragen
    assert status(client, world["gm"], s["id"])["state"] == "queued"


def test_fehler_des_anbieters(client, world, dbs, tmp_path):
    from app.extern import einen_auftrag

    w = world
    s = wartende_session(client, w, dbs, tmp_path)
    assert einen_auftrag(dbs, klient(Anbieter(503)))
    assert status(client, w["gm"], s["id"])["state"] == "queued"  # vorübergehend → neu eingereiht
    assert einen_auftrag(dbs, klient(Anbieter(401)))
    st = status(client, w["gm"], s["id"])
    assert st["state"] == "failed" and "API-Schlüssel" in st["message"]
