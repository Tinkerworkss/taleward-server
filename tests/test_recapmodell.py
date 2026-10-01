"""Recap-Modell nach Grafikkarte, Kontext nach Länge der Session, harte Regeln für Vorschläge (Server 0.4.36)."""
import httpx
import pytest

from app import recapmodell as rm
from tests.test_step2a import status
from tests.test_step2b import _kleine_teile, zusammenfassen  # noqa: F401 (Fixture)
from tests.test_step5 import Modell, einstellen, mit_geheimnissen

pytestmark = pytest.mark.usefixtures("_kleine_teile")


def test_wahl_nach_grafikspeicher():
    assert rm.wahl(None) == ("gemma4:e4b", 20480)       # ohne messbare Karte
    assert rm.wahl(4096) == ("gemma4:e4b", 12288)
    assert rm.wahl(8192) == ("gemma4:e4b", 20480)       # RTX 3060 Ti
    assert rm.wahl(12288) == ("gemma4:12b", 24576)
    assert rm.wahl(16380) == ("gemma4:12b", 32768)      # RTX 4060 Ti 16 GB
    assert rm.wahl(16380, 8000) == ("gemma4:e4b", 20480)  # Grenze aus der Worker-App gilt


def test_kontext_nach_laenge():
    assert rm.kontext_fuer(3000, 20480) == 12288     # kurze Session: nie unter 12288
    assert rm.kontext_fuer(14000, 32768) == 20480    # 47 min Spiel passt ganz hinein
    assert rm.kontext_fuer(60000, 20480) == 20480    # 4 h: höchstens die Stufe, darüber Szenennotizen
    assert rm.kontext_fuer(3000, 8192) == 8192       # von Hand kleiner eingestellt


def test_ollama_mindestfassung():
    assert rm.ollama_zu_alt("gemma4:e4b", "0.34.4") == "0.35.0"
    assert rm.ollama_zu_alt("gemma4:12b", "0.35.0") is None
    assert rm.ollama_zu_alt("gemma4:12b", "0.36.1-rc2") is None
    assert rm.ollama_zu_alt("ministral-3:8b", "0.12.0") is None
    assert rm.ollama_zu_alt("gemma4:e4b", None) is None


class NeuesOllama(Modell):
    def __init__(self, fassung="0.35.0", **kw):
        super().__init__(ollama=True, **kw)
        self.fassung = fassung

    def __call__(self, request):
        pfad = request.url.path
        if pfad.endswith("/api/version"):
            return httpx.Response(200, json={"version": self.fassung})
        if pfad.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": n, "digest": "0123456789abcdef"}
                                                        for n in ("gemma4:e4b", "gemma4:12b")]})
        return super().__call__(request)


def test_auto_nach_grafikkarte(client, world, dbs, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import arbeitsweise
    from app.models import UsageLog, Worker
    from app.worker_prozess import WorkerProzess, lokales_sprachmodell, verarbeite_attrappe
    from tests.test_step2a import worker_token

    w = world
    s, _oeff, _geheim, _ = mit_geheimnissen(client, w, dbs, tmp_path)
    einstellen(dbs, art="lokal", lokal_modell="auto")
    monkeypatch.setattr(arbeitsweise, "grafikspeicher_mb", lambda: 16380)
    modell = NeuesOllama()
    token = worker_token(dbs)
    dbs.get(Worker, token.split(".")[1]).capabilities = "asr,llm"
    dbs.commit()
    fn, llm = lokales_sprachmodell("http://ollama", httpx.Client(transport=httpx.MockTransport(modell)))
    knecht = WorkerProzess("http://testserver", token, tmp_path / "ollama", verarbeite_attrappe,
                           client=TestClient(client.app), claim_wait=0, info={"llm": llm}, zusammenfassen=fn)
    assert not zusammenfassen(dbs)
    assert knecht.einen_auftrag()
    assert status(client, w["gm"], s["id"])["state"] == "awaiting_review"
    recap = modell.recap_aufruf()
    assert recap["body"]["model"] == "gemma4:12b" and recap["body"]["options"]["num_ctx"] == 12288  # kurze Session
    log = dbs.query(UsageLog).filter_by(session_id=s["id"], kind="summary").one()
    assert log.model == "ollama/gemma4:12b@0123456789ab/ctx12288"


def test_auftrag_nennt_auto_und_ersatzmodell(dbs):
    from app.einstellungen import llm_konfig
    from app.routers.worker import _modell_angabe

    einstellen(dbs, art="lokal", lokal_modell="auto")
    assert _modell_angabe(llm_konfig(dbs)) == {"model": "gemma4:e4b", "context": 32768, "auto": True}
    einstellen(dbs, lokal_modell="qwen3:8b", lokal_kontext="16384")
    assert _modell_angabe(llm_konfig(dbs)) == {"model": "qwen3:8b", "context": 16384}


def test_zu_altes_ollama_meldet_sich(monkeypatch):
    from app import arbeitsweise
    from app.sprachmodell import SprachmodellFehler
    from app.worker_prozess import lokales_sprachmodell

    monkeypatch.setattr(arbeitsweise, "grafikspeicher_mb", lambda: 8192)
    fn, _ = lokales_sprachmodell("http://ollama", httpx.Client(transport=httpx.MockTransport(NeuesOllama("0.34.4"))))
    auftrag = {"type": "summarize", "summarize": {"model": "gemma4:e4b", "context": 32768, "auto": True,
                                                  "recap": {"transkript": [], "bibel": []}, "proposals": {}}}
    with pytest.raises(SprachmodellFehler, match="mindestens 0.35.0"):
        fn(auftrag, lambda _p: None)


def test_vorschlaege_ohne_meta_und_ohne_spielercharaktere():
    from app.sprachmodell import ohne_meta, pruefen

    assert ohne_meta("Dieser Ort wurde in der Session etabliert. Eine Tonne dient als Deckung.") == \
        "Eine Tonne dient als Deckung."
    assert ohne_meta("Die SL hat angedeutet, dass Oren Iria kennt.\nOren scheint Iria zu kennen.") == \
        "Oren scheint Iria zu kennen."
    roh = [
        {"entryType": "npc", "action": "update", "targetEntryId": "e1", "title": "Litha Flamel (Deckname: Rita)",
         "detail": "Sucht Tubo."},
        {"entryType": "npc", "action": "update", "targetEntryId": "e1", "title": "Die Fremde", "detail": "Sucht Tubo."},
        {"entryType": "quest", "action": "create", "title": "Flucht aus der Stadt",
         "detail": "Die Gruppe muss die Stadt verlassen.",
         "gmNotes": "Dies ist die unmittelbare Nebenmission in dieser Session. Die Janisaris warten am Tor."},
        {"entryType": "other", "action": "create", "title": "Nur Meta", "detail": "Wurde in der Session erwähnt."},
    ]
    aus = pruefen(roh, {"e1"}, set(), charaktere=["Litha Flamel", "Tubo"], namen={"e1": "Litha Flamel"})
    assert [v["title"] for v in aus] == ["Flucht aus der Stadt"]
    assert aus[0]["gmNotes"] == "Die Janisaris warten am Tor."
