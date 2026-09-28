"""Feste Modellfassungen (app/modelle.py) und Stimmprofile bei einer neuen Fassung des Sprechermodells."""
import json

import pytest

from tests.test_step2a import API
from tests.test_step6 import StimmMotor, _kleine_teile, profil_setzen, vektor  # noqa: F401 (Fixture)

REPO = "pyannote/speaker-diarization-community-1"


# ---------------------------------------------------------------- Bereitstellen
@pytest.fixture
def hub(tmp_path, monkeypatch):
    """Nachgebauter Hugging-Face-Zwischenspeicher: Ordner snapshots/<commit> je Fassung."""
    from app import modelle
    from app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    cache = tmp_path / "hf"
    stand = {"main": "aaaa1111", "lokal": set(), "aufrufe": []}

    def anlegen(fassung, vollstaendig=True):
        d = cache / "snapshots" / fassung
        d.mkdir(parents=True, exist_ok=True)
        if vollstaendig:
            (d / "config.yaml").write_text("x")
        stand["lokal"].add(fassung)

    def laden(repo, fassung, token, nur_lokal):
        stand["aufrufe"].append((fassung, nur_lokal))
        ziel = fassung or stand["main"]
        if nur_lokal:
            if ziel not in stand["lokal"]:
                raise FileNotFoundError("nicht im Zwischenspeicher")
            return cache / "snapshots" / ziel
        if stand.get("fehler"):
            raise RuntimeError(stand["fehler"])
        anlegen(ziel)
        return cache / "snapshots" / ziel

    monkeypatch.setattr(modelle, "_laden", laden)
    monkeypatch.setattr(modelle, "FASSUNGEN", {})
    stand["anlegen"] = anlegen
    yield stand
    get_settings.cache_clear()


def test_vorhandenes_modell_wird_gemerkt_und_nie_nachgefragt(hub):
    from app import modelle

    hub["anlegen"]("aaaa1111")  # z. B. Benjamins PC: schon geladen
    b = modelle.bereitstellen(REPO, "hf_x")
    assert (b.fassung, b.quelle) == ("aaaa1111", "neu") and hub["aufrufe"] == [(None, True)]
    assert modelle.gemerkt() == {REPO: "aaaa1111"}
    hub["main"] = "bbbb2222"  # bei Hugging Face erscheint eine neue Fassung
    b = modelle.bereitstellen(REPO, "hf_x")
    assert (b.fassung, b.quelle) == ("aaaa1111", "gemerkt")
    assert hub["aufrufe"][-1] == ("aaaa1111", True)  # nur lokal, ohne Netz
    assert b.kennung == f"{REPO}@aaaa1111"


def test_erstes_laden_und_unvollstaendiger_ordner(hub):
    from app import modelle

    hub["anlegen"]("aaaa1111", vollstaendig=False)
    b = modelle.bereitstellen(REPO, "hf_x")
    assert b.fassung == "aaaa1111" and hub["aufrufe"] == [(None, True), (None, False)]


def test_festgelegte_fassung_hat_vorrang(hub, monkeypatch):
    from app import modelle

    hub["anlegen"]("aaaa1111")
    modelle.bereitstellen(REPO)  # gemerkt: aaaa
    monkeypatch.setitem(modelle.FASSUNGEN, REPO, "cccc3333")  # Server-Update legt neue Fassung fest
    b = modelle.bereitstellen(REPO)
    assert (b.fassung, b.quelle) == ("cccc3333", "festgelegt")
    assert hub["aufrufe"][-2:] == [("cccc3333", True), ("cccc3333", False)]
    assert modelle.gemerkt()[REPO] == "cccc3333"


@pytest.mark.parametrize("fehler,erwartet", [("401 Client Error: Unauthorized", "HF_TOKEN"),
                                             ("ConnectionError: offline", "nicht erreichbar")])
def test_verstaendliche_fehler(hub, fehler, erwartet):
    from app import modelle

    hub["fehler"] = fehler
    with pytest.raises(modelle.ModellFehler, match=erwartet):
        modelle.bereitstellen(REPO)


def test_eigener_ordner(tmp_path):
    from app import modelle

    b = modelle.bereitstellen(str(tmp_path))
    assert (b.fassung, b.quelle, b.kennung) == (None, "ordner", str(tmp_path))


def test_passt():
    from app.modelle import passt

    assert passt(f"{REPO}@aaaa", f"{REPO}@aaaa")
    assert not passt(f"{REPO}@aaaa", f"{REPO}@bbbb")
    assert passt(REPO, f"{REPO}@bbbb")  # Profil von vor der Festlegung
    assert passt(None, f"{REPO}@bbbb")
    assert not passt("anderes/modell@aaaa", f"{REPO}@aaaa")


# ---------------------------------------------------------------- Stimmprofile bei neuer Fassung
class NeueFassung(StimmMotor):
    abdruck_modell = f"{REPO}@bbbb2222"


def _transkribieren(client, w, dbs, tmp_path, motor):
    from tests.test_step2a import audio_abschnitte, hochladen, neue_session
    from tests.test_step6 import knecht
    from app.transkription import verarbeiter

    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (140,)))
    assert knecht(client, dbs, tmp_path, verarbeiter(motor)).einen_auftrag()
    return s


def _fassung_setzen(dbs, member_id, modell):
    from app.models import Member, VoiceProfile

    vp = dbs.get(VoiceProfile, dbs.get(Member, member_id).user_id)
    vp.embedding_model = modell
    dbs.commit()


def test_fassung_wird_mit_den_stimmen_gespeichert(client, world, dbs, tmp_path):
    from app.models import Speaker

    s = _transkribieren(client, world, dbs, tmp_path, NeueFassung())
    assert {sp.embedding_model for sp in dbs.query(Speaker).filter_by(session_id=s["id"])} == {f"{REPO}@bbbb2222"}


def test_andere_fassung_wird_nicht_verglichen_sondern_neu_angelernt(client, world, dbs, tmp_path):
    from app import stimmprofile
    from app.models import Member, VoiceProfile

    w = world
    profil_setzen(dbs, w["pl_member"], vektor(3), lernen=True)
    profil_setzen(dbs, w["gm_member"], vektor(7), lernen=False)
    for m in ("pl_member", "gm_member"):
        _fassung_setzen(dbs, w[m], f"{REPO}@aaaa1111")
    s = _transkribieren(client, w, dbs, tmp_path, NeueFassung())
    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    assert all(sp["source"] != "voice_match" for sp in sprecher)  # alte Abdrücke nicht mit neuen vergleichen
    # SL ordnet selbst zu und bestätigt
    zuordnung = [{"speakerId": sp["id"], "memberId": w["pl_member"] if sp["label"] == "Stimme 1" else w["gm_member"]}
                 for sp in sprecher]
    assert client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=zuordnung).status_code == 202
    ben = client.get(f"{API}/me/voice-profile", headers=w["pl"]).json()
    anna = client.get(f"{API}/me/voice-profile", headers=w["gm"]).json()
    assert ben["status"] == "ready" and ben["learnedSessionCount"] == 1
    assert anna["status"] == "failed" and "neu aufnehmen" in anna["message"]
    dbs.expire_all()
    vp = dbs.get(VoiceProfile, dbs.get(Member, w["pl_member"]).user_id)
    assert vp.embedding_model == f"{REPO}@bbbb2222" and vp.learned_sum is None
    assert stimmprofile.kosinus(json.loads(vp.base_embedding), vektor(3)) > 0.9
    alt = dbs.get(VoiceProfile, dbs.get(Member, w["gm_member"]).user_id)
    assert alt.base_embedding is None and alt.embedding_model is None  # verworfen, nichts bleibt
    # nächste Session mit derselben Fassung: wird wieder erkannt
    s2 = _transkribieren(client, w, dbs, tmp_path, NeueFassung())
    sprecher = client.get(f"{API}/sessions/{s2['id']}/speakers", headers=w["gm"]).json()
    assert any(sp["suggestedMemberId"] == w["pl_member"] and sp["source"] == "voice_match" for sp in sprecher)


def test_gleiche_fassung_unveraendert(client, world, dbs, tmp_path):
    w = world
    profil_setzen(dbs, w["pl_member"], vektor(3))
    _fassung_setzen(dbs, w["pl_member"], f"{REPO}@bbbb2222")
    s = _transkribieren(client, w, dbs, tmp_path, NeueFassung())
    sprecher = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    assert any(sp["suggestedMemberId"] == w["pl_member"] and sp["source"] == "voice_match" for sp in sprecher)


def test_motor_nutzt_feste_fassungen(monkeypatch, tmp_path):
    import sys
    import types

    from app import modelle, transkription

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: True, get_device_name=lambda i: "Testkarte")
    geladen = {}
    wx = types.ModuleType("whisperx")
    wx.load_model = lambda pfad, *a, **kw: geladen.setdefault("whisper", pfad) and None
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "whisperx", wx)
    monkeypatch.setattr(transkription, "cuda_bibliotheken_vorladen", lambda: None)
    monkeypatch.setattr(transkription.VramMesser, "gesamt_mb", staticmethod(lambda: 8192))
    monkeypatch.setattr(modelle, "bereitstellen",
                        lambda repo, token=None: modelle.Bereit(repo, "f00d" * 10, tmp_path / repo, "gemerkt"))
    m = transkription.WhisperXMotor(hf_token="hf_x")
    assert m.abdruck_modell == REPO  # vor dem Prüfen: ohne Fassung
    info = m.pruefen()
    assert info["modelle"] == {"Systran/faster-whisper-large-v3": "f00d" * 3, REPO: "f00d" * 3}
    assert m.abdruck_modell == f"{REPO}@{'f00d' * 3}"
    assert m._pfad("whisper", "large-v3") == str(tmp_path / "Systran/faster-whisper-large-v3")
    import os
    assert os.environ["PYANNOTE_METRICS_ENABLED"] == "false"


def _schein_ki(monkeypatch, tmp_path, cuda: bool):
    import sys
    import types

    from app import modelle, transkription

    aufrufe = {}
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: cuda, get_device_name=lambda i: "Testkarte",
        get_device_properties=lambda i: types.SimpleNamespace(total_memory=8 * 2 ** 30),
        set_per_process_memory_fraction=lambda f, i: aufrufe.setdefault("anteil", f))
    torch.set_num_threads = lambda n: aufrufe.setdefault("threads", n)
    wx = types.ModuleType("whisperx")

    class Modell:
        def transcribe(self, *a, **kw):
            return {"segments": []}

    def laden(pfad, geraet, **kw):
        aufrufe["laden"] = (geraet, kw)
        return Modell()

    wx.load_model = laden
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "whisperx", wx)
    monkeypatch.setattr(transkription, "cuda_bibliotheken_vorladen", lambda: None)
    monkeypatch.setattr(transkription, "gpu_freigeben", lambda: None)
    monkeypatch.setattr(transkription.VramMesser, "gesamt_mb", staticmethod(lambda: 8192 if cuda else None))
    monkeypatch.setattr(modelle, "bereitstellen",
                        lambda repo, token=None: modelle.Bereit(repo, "f00d" * 10, tmp_path / repo, "gemerkt"))
    return aufrufe


def test_motor_auf_dem_prozessor(monkeypatch, tmp_path):
    """Ohne Grafikkarte: alles auf der CPU, int8 statt float16, eigene Zahl an Threads."""
    from app import transkription

    aufrufe = _schein_ki(monkeypatch, tmp_path, cuda=False)
    m = transkription.WhisperXMotor(modell="large-v3-turbo", hf_token="hf_x", geraet="cpu", threads=6)
    info = m.pruefen()
    assert info["gpu"] == "CPU" and info["geraete"] == "cpu/cpu/cpu" and m.genauigkeit == "int8"
    assert aufrufe["threads"] == 6
    m.transkribieren([], "de", [], lambda p: None)
    assert aufrufe["laden"][0] == "cpu" and aufrufe["laden"][1]["threads"] == 6
    # Grafikkarte verlangt, aber keine da → verständlicher Fehler
    with pytest.raises(transkription.EinrichtungsFehler, match="Prozessor"):
        transkription.WhisperXMotor(hf_token="hf_x").pruefen()


def test_motor_mit_speichergrenze(monkeypatch, tmp_path):
    from app import transkription

    aufrufe = _schein_ki(monkeypatch, tmp_path, cuda=True)
    m = transkription.WhisperXMotor(hf_token="hf_x", geraet="cuda", geraet_sprecher="cpu", grenze_mb=4096)
    info = m.pruefen()
    assert info["geraete"] == "cuda/cuda/cpu" and info["grenzeMb"] == 4096
    assert aufrufe["anteil"] == 0.5 and m.genauigkeit == "int8_float16"
