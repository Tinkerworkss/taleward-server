"""Modellvergleich (chronik modellvergleich): Eingabe über die Schnittstelle, Ablauf je Modell, Richter, Bericht."""
import json

import httpx
import pytest

from tests.test_pruefung_046 import _zur_pruefung
from tests.test_step2b import _kleine_teile, zusammenfassen  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


def _ollama(aufrufe: list, kaputt: set[str] = frozenset()):
    """Nachgebautes Ollama: antwortet je nach Systemanweisung mit passendem JSON."""
    def antwort(request: httpx.Request) -> httpx.Response:
        pfad = request.url.path
        if pfad == "/api/version":
            return httpx.Response(200, json={"version": "0.34.4"})
        if pfad == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "a:1", "digest": "abc"}, {"name": "b:2", "digest": "def"},
                                                        {"name": "richter:1", "digest": "fff"}]})
        if pfad == "/api/show":
            name = json.loads(request.content)["model"]
            return httpx.Response(200, json={"capabilities": ["completion"] + (["thinking"] if name == "b:2" else [])})
        if pfad == "/api/generate" or pfad == "/api/ps":
            return httpx.Response(200, json={"models": []})
        body = json.loads(request.content)
        aufrufe.append(body)
        if body["model"] in kaputt:
            return httpx.Response(500, json={"error": "kaputt"})
        system = body["messages"][0]["content"]
        if system.startswith("Du prüfst"):
            inhalt = {"absaetze": [{"nr": 1, "urteil": "belegt", "stellen": [{"zeit": "00:00", "zitat": ""}]},
                                   {"nr": 2, "urteil": "unbelegt", "begruendung": "erfunden"}]}
        elif system.startswith("Du überarbeitest"):
            inhalt = {"absaetze": [{"nr": 2, "text": "Danach rasteten sie."}]}
        elif system.startswith("Du schreibst den Recap"):
            inhalt = {"title": f"Titel {body['model']}", "text": "Die Gruppe zog los.\n\nEin Drache erschien.",
                      "openThreads": ["Wer war der Fremde?"]}
        elif system.startswith("Du hilfst"):
            inhalt = {"notizen": ["Die Gruppe zog los."]}
        else:
            inhalt = {"proposals": []}
        zeile = {"message": {"content": json.dumps(inhalt)}, "done": True, "eval_count": 120,
                 "eval_duration": 2_000_000_000, "prompt_eval_count": 300}
        return httpx.Response(200, content=(json.dumps(zeile) + "\n").encode())
    return httpx.Client(transport=httpx.MockTransport(antwort))


def test_modellvergleich(client, world, dbs, tmp_path):
    from app import modellvergleich as mv

    w = world
    s = _zur_pruefung(client, w, dbs, tmp_path)
    server = mv.Server("http://testserver", client=client)
    with pytest.raises(mv.VergleichFehler):
        server.anmelden("anna", "falsch")
    server.anmelden("anna", "geheim123")
    assert [x["id"] for x in server.sessions_mit_transkript()] == [s["id"]]
    recap_ein, vorschlag_ein, info = mv.eingabe_aus_schnittstelle(server, s["id"])
    assert recap_ein["transkript"] and recap_ein["geheim"] == [] and info["kapitel"] == 1
    assert any("Spielleitung" in z["sprecher"] for z in recap_ein["transkript"])

    aufrufe = []
    ollama = _ollama(aufrufe, kaputt={"b:2"})
    meldungen = []
    ordner = mv.ausfuehren(server, s["id"], mv.modelle_lesen("a:1, b:2@8192", 12288), "richter:1", 12288,
                           "http://ollama", tmp_path / "aus", meldungen.append, client=ollama)
    bericht = (ordner / "bericht.md").read_text(encoding="utf-8")
    assert "| a:1 (ctx 12288) | ok |" in bericht and "| b:2 (ctx 8192) | Fehler |" in bericht
    erg = json.loads((ordner / "a_1-ctx12288" / "ergebnis.json").read_text(encoding="utf-8"))
    assert erg["nachgebessert"] is True and erg["text"].endswith("Danach rasteten sie.")
    assert erg["selbst"]["supported"] == 1 and erg["richter"]["total"] == 2  # der Richter hat bewertet
    assert "Titel a:1" in (ordner / "bericht.html").read_text(encoding="utf-8")
    transkript = (ordner / "transkript.txt").read_text(encoding="utf-8")
    assert "(Spielleitung)" in transkript and "[0:00:00]" in transkript
    # Denkmodus: nur beim Modell, das ihn kann, wird er abgeschaltet
    assert all("think" not in b for b in aufrufe if b["model"] == "a:1")
    assert all(b.get("think") is False for b in aufrufe if b["model"] == "b:2")
    # Spieler können nicht vergleichen (keine SL)
    spieler = mv.Server("http://testserver", client=client)
    spieler.anmelden("ben", "geheim123")
    with pytest.raises(mv.VergleichFehler):
        mv.eingabe_aus_schnittstelle(spieler, s["id"])


def test_modelle_lesen():
    from app.modellvergleich import modelle_lesen

    assert modelle_lesen("x, y@8192 ,", 12288) == [("x", 12288), ("y", 8192)]
