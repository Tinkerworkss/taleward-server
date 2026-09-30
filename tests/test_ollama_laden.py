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
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        koerper.append(json.loads(req.content))
        assert koerper[-1]["stream"] is True
        status, inhalt = antworten.pop(0)
        if status == 200:  # Ollama streamt Zeile für Zeile
            text = inhalt["message"]["content"]
            zeilen = [{"message": {"content": text[:3]}, "done": False},
                      {"message": {"content": text[3:]}, "done": True,
                       "prompt_eval_count": inhalt.get("prompt_eval_count", 0), "eval_count": inhalt.get("eval_count", 0),
                       "eval_duration": inhalt.get("eval_duration", 0)}]
            return httpx.Response(200, content="\n".join(json.dumps(z) for z in zeilen).encode())
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


def test_stillstand_ist_ein_klarer_fehler():
    def antwort(req: httpx.Request):
        raise httpx.ReadTimeout("nichts kommt")

    k = OllamaKlient("http://ollama:11434", "m", client=httpx.Client(transport=httpx.MockTransport(antwort)))
    try:
        k.chat("s", "n")
    except SprachmodellFehler as e:
        assert "antwortet seit" in str(e)
    else:
        raise AssertionError("kein Fehler")


def test_langsamer_rechner_wird_gemessen(caplog):
    k, _ = _chat_klient([(200, {"message": {"content": '{"a": 1}'}, "eval_count": 100, "eval_duration": int(100e9)})])
    with caplog.at_level("WARNING", logger="worker"):
        k.chat("s", "n")
    assert k.token_s == 1.0 and any("sehr langsam" in r.message for r in caplog.records)


def test_entladen_wartet_bis_das_modell_weg_ist(monkeypatch):
    from app import sprachmodell

    abfragen = {"n": 0}

    def antwort(req: httpx.Request):
        if req.url.path == "/api/generate":
            return httpx.Response(200, json={})
        abfragen["n"] += 1
        return httpx.Response(200, json={"models": [] if abfragen["n"] >= 3 else [{"name": "m"}]})

    monkeypatch.setattr(sprachmodell.time, "sleep", lambda s: None)
    k = OllamaKlient("http://ollama:11434", "m", client=httpx.Client(transport=httpx.MockTransport(antwort)))
    k.entladen()
    assert abfragen["n"] == 3


def test_taetigkeit_meldet_token_pro_sekunde(monkeypatch):
    """Die Worker-App zeigt, was der Worker gerade tut – beim Sprachmodell die Geschwindigkeit."""
    from app import worker_prozess
    from app.sprachmodell import Zaehler

    gemeldet = []
    monkeypatch.setattr(worker_prozess, "_taetigkeit", lambda text, **d: gemeldet.append((text, d)))
    k, _ = _chat_klient([(200, {"message": {"content": '{"a": 1}'}, "prompt_eval_count": 3800, "eval_count": 900,
                                "eval_duration": int(60e9)})])
    z = Zaehler()
    assert z.aufruf(k, "s", "n") == {"a": 1}
    text, daten = gemeldet[-1]
    assert "15,0 Token/s" in text and "3 800 Token gelesen" in text
    assert daten == {"schritt": "sprachmodell", "tokenS": 15.0, "aufruf": 1}
    assert z.token_s_mittel == 15.0


def test_recap_text_aus_abweichenden_antworten():
    from app.sprachmodell import recap_text

    assert recap_text({"title": "K", "text": "Es war einmal."}) == "Es war einmal."
    assert recap_text({"title": "K", "recap": "Anders benannt."}) == "Anders benannt."
    assert recap_text({"recap": {"title": "K", "text": "Verschachtelt."}}) == "Verschachtelt."
    assert recap_text({"title": "K", "text": ["Absatz eins.", "Absatz zwei."]}) == "Absatz eins.\n\nAbsatz zwei."
    lang = "x" * 250
    assert recap_text({"title": "K", "irgendwas": lang}) == lang
    assert recap_text({"title": "K", "a": lang, "b": lang}) == ""  # zweideutig: lieber Fehler
    assert recap_text({"title": "K"}) == ""


def test_fehlender_recap_nennt_nur_die_form():
    from app.sprachmodell import Ablauf, Antwort, SprachmodellFehler

    class K:
        modell = "m"

        def chat(self, system, nutzer):
            return Antwort('{"title": "Kapitel 3", "summary_de": {"a": 1}, "openThreads": []}', 10, 5)

        def kosten_cent(self, a, b):
            return 0

    ein = {"bibel": [], "session_nummer": 3, "sprache": "de", "kampagne": "K", "system": None, "system_name": None,
           "welt": None, "personen": [], "transkript": []}
    try:
        Ablauf(K()).recap(ein, "Transkript", "…")
    except SprachmodellFehler as e:
        assert "title:str[9]" in str(e) and "summary_de:{a}" in str(e) and "Kapitel" not in str(e)
    else:
        raise AssertionError("kein Fehler")
