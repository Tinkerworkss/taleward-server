"""Sprachmodell laden: Fortschritt im Protokoll statt 20 Minuten Stille."""
import json

import httpx

from app.sprachmodell import OllamaKlient, SprachmodellFehler


def _klient(pull_zeilen, tags_danach=True):
    zustand = {"geladen": False}

    def antwort(req: httpx.Request):
        if req.url.path == "/api/tags":
            modelle = [{"name": "ministral-3:8b", "digest": "abcdef1234567890"}] if zustand["geladen"] else []
            return httpx.Response(200, json={"models": modelle})
        if req.url.path == "/api/pull":
            assert json.loads(req.content)["stream"] is True
            zustand["geladen"] = tags_danach
            return httpx.Response(200, content="\n".join(json.dumps(z) for z in pull_zeilen).encode())
        return httpx.Response(404)

    return OllamaKlient("http://ollama:11434", "ministral-3:8b", client=httpx.Client(transport=httpx.MockTransport(antwort)))


def test_fortschritt_alle_zehn_prozent():
    gesamt = 6 * 2 ** 30
    zeilen = [{"status": "pulling manifest"}, {"status": "pulling", "total": 1000, "completed": 1000}]
    zeilen += [{"status": "pulling", "total": gesamt, "completed": gesamt * p // 100} for p in [*range(0, 100, 3), 100]]
    zeilen += [{"status": "success"}]
    gemeldet = []
    assert _klient(zeilen).bereitstellen(gemeldet.append) == "abcdef123456"
    prozente = [z for z in gemeldet if "%" in z]
    assert prozente[0].endswith("0 % von 6.0 GB") and prozente[-1].startswith("Sprachmodell ministral-3:8b: 100 %")
    assert len(prozente) == 11


def test_fehler_von_ollama():
    try:
        _klient([{"error": "pull model manifest: file does not exist"}], tags_danach=False).bereitstellen()
    except SprachmodellFehler as e:
        assert "does not exist" in str(e)
    else:
        raise AssertionError("kein Fehler")
