"""Schritt 3: echte Verarbeitung im Worker – geprüft mit einem Test-Motor statt WhisperX (keine Grafikkarte nötig)."""
import json

import pytest
from fastapi.testclient import TestClient

from tests.test_step2a import API, audio_abschnitte, hochladen, neue_session, status, worker_token


@pytest.fixture(autouse=True)
def _kleine_teile(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(64 * 1024))


class ProbeMotor:
    """Verhält sich wie WhisperX: Segmente mit Zeiten, später Sprecher und Abdrücke."""

    modell = "test"

    def __init__(self, fehler: Exception | None = None):
        self.fehler = fehler
        self.aufrufe: dict = {}

    def audio_laden(self, wav):
        from app import audio

        return audio.dauer(wav)

    def transkribieren(self, dauer, sprache, hotwords, fortschritt):
        self.aufrufe["sprache"], self.aufrufe["hotwords"] = sprache, hotwords
        if self.fehler:
            raise self.fehler
        fortschritt(50)
        segs, t, k = [], 0.0, 0
        while t + 8 <= dauer:
            segs.append({"start": t, "end": t + 8, "text": f" Satz {k} über den Grauen Fürsten."})
            t, k = t + 10, k + 1
        segs.append({"start": dauer - 4.0, "end": dauer - 2.5, "text": " Ähm."})  # kurzer Einwurf
        # Leerer Text wird bereinigt; produktiv kommen seit Resolver 3 keine Hotwords mehr an Whisper.
        segs.append({"start": dauer - 1.5, "end": dauer - 0.2, "text": ", ".join(hotwords[:4])})
        fortschritt(100)
        return segs

    def ausrichten(self, segmente, dauer, sprache, fortschritt):
        fortschritt(100)
        return segmente

    def sprecher_trennen(self, dauer, segmente, min_n, max_n, fortschritt):
        self.aufrufe["sprecher"] = (min_n, max_n)
        for i, s in enumerate(segmente):
            s["speaker"] = f"SPEAKER_0{i % 2}"
        segmente[-1]["speaker"] = "SPEAKER_02"  # winziger Cluster (Fehlzuordnung): der kurze Einwurf
        fortschritt(100)
        return segmente, {"SPEAKER_00": [0.1] * 4, "SPEAKER_01": [0.2] * 4, "SPEAKER_02": [0.3] * 4}


def knecht(client, dbs, tmp_path, motor):
    from app.worker_prozess import WorkerProzess
    from app.transkription import verarbeiter

    return WorkerProzess("http://testserver", worker_token(dbs), tmp_path / "knecht", verarbeiter(motor),
                        client=TestClient(client.app), claim_wait=0)


def test_tischaufnahme_echt(client, world, dbs, tmp_path):
    from app.models import Speaker, UsageLog

    w = world
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"],
                 json={"characterName": "Jemma Reed"})
    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (30, 25)))
    motor = ProbeMotor()
    assert knecht(client, dbs, tmp_path, motor).einen_auftrag()

    assert status(client, w["gm"], s["id"])["state"] == "awaiting_speakers"
    assert motor.aufrufe["sprache"] == "de" and motor.aufrufe["hotwords"] == []
    assert motor.aufrufe["sprecher"] == (1, 3)  # 2 Anwesende ± 1
    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    assert len(sprecher) == 2  # der winzige Cluster wird keine eigene Stimme
    assert all(sp["sampleText"].startswith("Satz") for sp in sprecher)
    probe = client.get(f"{API}/sessions/{s['id']}/speakers/{sprecher[0]['id']}/sample", headers=w["gm"])
    assert probe.content[:4] == b"OggS"
    dbs.expire_all()
    assert all(json.loads(sp.embedding) for sp in dbs.query(Speaker).filter_by(session_id=s["id"]))
    zeilen = client.get(f"{API}/sessions/{s['id']}/transcript", headers=w["gm"]).json()
    assert not any("Jemma Reed" in z["text"] for z in zeilen)  # kein Prompt-Echo möglich
    assert zeilen[-1]["text"] == "Ähm." and zeilen[-1]["speakerId"] == ""  # ohne Stimme
    assert all(z["speakerId"] for z in zeilen[:-1])
    log = dbs.query(UsageLog).filter_by(session_id=s["id"], kind="transcription").one()
    assert log.model == "whisperx/test" and 54 <= log.audio_seconds <= 56
    # Terminologiestand bleibt reproduzierbar, aber ASR-Hotwords sind bewusst leer.
    snap = json.loads(dbs.get(__import__("app.models", fromlist=["GameSession"]).GameSession, s["id"]).hotword_snapshot)
    assert snap["resolverVersion"] == "3" and snap["mode"] == "dynamic"
    assert "Jemma Reed" in snap["terms"] and len(snap["fingerprint"]) == 64
    assert snap["asrHotwords"] == [] and snap["terminologyVersion"] == "1"
    assert isinstance(snap["terminologyRules"], list) and len(snap["terminologyRuleFingerprint"]) == 64


def test_kleiner_cluster_bleibt_ohne_stimme(tmp_path):
    from app.transkription import sprecher_auswerten

    wav = tmp_path / "a.wav"
    from app import audio
    audio.zusammenfuegen(audio_abschnitte(tmp_path, (20,)), wav)
    segs = [{"start": 0, "end": 8, "text": "Hallo", "speaker": "A"},
            {"start": 9, "end": 11, "text": "Ähm", "speaker": "B"},
            {"start": 12, "end": 13, "text": "ja", "speaker": None}]
    aus, stimmen = sprecher_auswerten(segs, wav, tmp_path, {"A": [1.0], "B": [2.0]})
    assert [s["label"] for s in stimmen] == ["A"] and stimmen[0]["embedding"] == [1.0]
    assert [s["speaker"] for s in aus] == ["A", None, None]


def test_discord_echt(client, world, dbs, tmp_path):
    w = world
    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (20, 30)), "discord",
              [w["gm_member"], w["pl_member"]])
    motor = ProbeMotor()
    assert knecht(client, dbs, tmp_path, motor).einen_auftrag()
    assert "sprecher" not in motor.aufrufe  # keine Sprechertrennung bei Discord
    assert status(client, w["gm"], s["id"])["state"] == "summarizing"
    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    assert {sp["suggestedMemberId"] for sp in sprecher} == {w["gm_member"], w["pl_member"]}
    zeilen = client.get(f"{API}/sessions/{s['id']}/transcript", headers=w["gm"]).json()
    assert [z["start"] for z in zeilen] == sorted(z["start"] for z in zeilen)  # nach Zeit zusammengeführt
    assert {z["memberId"] for z in zeilen} == {w["gm_member"], w["pl_member"]}
    sess = client.get(f"{API}/sessions/{s['id']}", headers=w["gm"]).json()
    assert 29 <= sess["durationSeconds"] <= 31  # längste Spur


@pytest.mark.parametrize("fehler, zustand, text", [
    (RuntimeError("CUDA out of memory. Tried to allocate 2 GiB"), "queued", None),
    (OSError("401 Client Error: Unauthorized – gated repo"), "failed", "HF_TOKEN"),
])
def test_fehler_werden_verstaendlich(client, world, dbs, tmp_path, fehler, zustand, text):
    from app.models import Job

    w = world
    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (12,)))
    assert knecht(client, dbs, tmp_path, ProbeMotor(fehler)).einen_auftrag()
    st = status(client, w["gm"], s["id"])
    assert st["state"] == zustand
    if text:
        assert text in st["message"]
    dbs.expire_all()
    job = dbs.query(Job).filter_by(session_id=s["id"]).one()
    assert job.error_code == ("cuda_oom" if zustand == "queued" else "worker_setup")


def test_worker_startet_nicht_ohne_einrichtung(client, monkeypatch):
    """Die Start-Prüfung stoppt mit klarer Meldung. Startet nie einen echten Worker – auch nicht auf einem
    PC, auf dem KI-Pakete und Grafikkarte vorhanden sind."""
    from typer.testing import CliRunner

    from app import config, worker_prozess, transkription
    from app.cli import app

    def nie_starten(*_a, **_k):
        raise AssertionError("Test darf keinen echten Worker starten")

    monkeypatch.setattr(worker_prozess.WorkerProzess, "laufen", nie_starten)
    monkeypatch.setattr(worker_prozess.WorkerProzess, "einen_auftrag", nie_starten)
    monkeypatch.setenv("WORKER_TOKEN", "wk.x.y")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    config.get_settings.cache_clear()
    r = CliRunner().invoke(app, ["worker"])
    # Ohne Hugging-Face-Zugang startet er trotzdem, wenn der Server das Sprechermodell liefert – hier scheitert es
    # vorher an den KI-Paketen bzw. am nicht erreichbaren Server; in jedem Fall mit klarer Meldung
    assert r.exit_code == 1 and "Worker kann nicht starten" in r.output

    def keine_ki(self):
        raise transkription.EinrichtungsFehler("Die KI-Pakete fehlen. Installieren mit: uv sync --extra ki")

    monkeypatch.setattr(transkription.WhisperXMotor, "pruefen", keine_ki)
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    config.get_settings.cache_clear()
    r = CliRunner().invoke(app, ["worker"])
    assert r.exit_code == 1 and "KI-Pakete fehlen" in r.output


def test_halluzinationen():
    from app.transkription import halluzinationen_entfernen, ist_halluzination

    assert ist_halluzination("Musik Musik Musik Musik") and ist_halluzination(" Untertitel im Auftrag des ZDF, 2021")
    assert not ist_halluzination("Die Musik im Wirtshaus ist laut.")
    segs = [{"text": "Musik Musik", "speaker": None}, {"text": "Musik", "speaker": "SPEAKER_00"},
            {"text": "Hallo", "speaker": None}]
    assert [s["text"] for s in halluzinationen_entfernen(segs)] == ["Musik", "Hallo"]
    assert [s["text"] for s in halluzinationen_entfernen(segs, nur_ohne_sprecher=False)] == ["Hallo"]
