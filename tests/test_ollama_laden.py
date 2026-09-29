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


def _chat_klient(antworten):
    koerper = []

    def antwort(req: httpx.Request):
        koerper.append(json.loads(req.content))
        status, inhalt = antworten.pop(0)
        return httpx.Response(status, json=inhalt)

    k = OllamaKlient("http://ollama:11434", "ministral-3:8b", client=httpx.Client(transport=httpx.MockTransport(antwort)))
    return k, koerper


def test_wiederholungsschleife_wird_einmal_neu_versucht():
    k, koerper = _chat_klient([
        (500, {"error": "prediction aborted, token repeat limit reached"}),
        (200, {"message": {"content": '{"ok": true}'}, "prompt_eval_count": 10, "eval_count": 5}),
    ])
    a = k.chat("sys", "nutzer")
    assert a.text == '{"ok": true}' and a.tokens_out == 5
    erste, zweite = (b["options"] for b in koerper)
    assert erste["repeat_penalty"] > 1 and erste["num_predict"] > 0
    assert zweite["temperature"] > erste["temperature"] and zweite["repeat_penalty"] > erste["repeat_penalty"]


def test_wiederholungsschleife_zweimal_ist_ein_fehler():
    k, koerper = _chat_klient([(500, {"error": "token repeat limit reached"})] * 2)
    try:
        k.chat("sys", "nutzer")
    except SprachmodellFehler as e:
        assert "repeat" in str(e) and len(koerper) == 2
    else:
        raise AssertionError("kein Fehler")


def test_anderer_fehler_wird_nicht_wiederholt():
    k, koerper = _chat_klient([(500, {"error": "out of memory"})])
    try:
        k.chat("sys", "nutzer")
    except SprachmodellFehler:
        assert len(koerper) == 1
    else:
        raise AssertionError("kein Fehler")
