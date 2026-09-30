"""Worker im wackligen WLAN: Teile werden wiederholt geladen, Herzschläge nachgeschlagen."""
from pathlib import Path

import httpx
import pytest

from app.worker_prozess import Abgebrochen, WorkerProzess, verarbeite_attrappe


def _knecht(antwort, tmp_path):
    client = httpx.Client(base_url="http://server", transport=httpx.MockTransport(antwort))
    k = WorkerProzess("http://server", "t", tmp_path / "arbeit", verarbeite_attrappe, client=client, claim_wait=0)
    k.arbeit.mkdir(parents=True, exist_ok=True)
    return k


def test_teil_wird_nach_netzfehler_erneut_geladen(tmp_path, monkeypatch):
    versuche = {"n": 0}

    def antwort(req: httpx.Request):
        versuche["n"] += 1
        if versuche["n"] == 1:
            raise httpx.ConnectError("WLAN weg")
        if versuche["n"] == 2:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, content=b"abcd")

    k = _knecht(antwort, tmp_path)
    monkeypatch.setattr(k._stop, "wait", lambda s: False)
    dateien = k.herunterladen({"files": [{"position": 1, "fileId": "f1",
                                          "chunks": [{"index": 0, "url": "http://server/c/0", "sizeBytes": 4}]}]})
    assert versuche["n"] == 3 and Path(dateien[0]).read_bytes() == b"abcd"


def test_teil_gibt_nach_vier_versuchen_auf(tmp_path, monkeypatch):
    def antwort(req: httpx.Request):
        raise httpx.ReadTimeout("nichts")

    k = _knecht(antwort, tmp_path)
    monkeypatch.setattr(k._stop, "wait", lambda s: False)
    with pytest.raises(httpx.ReadTimeout):
        k._teil_laden({"index": 0, "url": "http://server/c/0", "sizeBytes": 4})


def test_409_und_404_werden_nicht_wiederholt(tmp_path, monkeypatch):
    zaehler = {"n": 0}

    def antwort(req: httpx.Request):
        zaehler["n"] += 1
        return httpx.Response(409 if "a" in req.url.path else 404)

    k = _knecht(antwort, tmp_path)
    with pytest.raises(Abgebrochen):
        k._teil_laden({"index": 0, "url": "http://server/a", "sizeBytes": 4})
    with pytest.raises(httpx.HTTPStatusError):
        k._teil_laden({"index": 0, "url": "http://server/b", "sizeBytes": 4})
    assert zaehler["n"] == 2

