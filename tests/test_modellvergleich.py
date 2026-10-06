"""Modellvergleich (chronik modellvergleich): Eingabe über die Schnittstelle, Ablauf je Modell, Richter, Bericht."""
import json

import httpx
import pytest

from tests.test_pruefung_046 import _zur_pruefung
from tests.test_step2b import _kleine_teile, zusammenfassen  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


def _ollama(aufrufe: list, kaputt: set[str] = frozenset(), pruefung_kaputt: bool = False):
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
        if pruefung_kaputt and system.startswith("Du prüfst den Recap"):
            zeile = {"message": {"content": "Hier ist meine Einschätzung: alles prima ]}<tool_call|>"}, "done": True,
                     "eval_count": 5, "eval_duration": 1_000_000_000, "prompt_eval_count": 300}
            return httpx.Response(200, content=(json.dumps(zeile) + "\n").encode())
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
    assert erg["ollama"] == "http://ollama (Ollama 0.34.4)" and erg["warnungen"] == []
    assert "Ollama: http://ollama (Ollama 0.34.4)." in bericht
    assert "Titel a:1" in (ordner / "bericht.html").read_text(encoding="utf-8")
    transkript = (ordner / "transkript.txt").read_text(encoding="utf-8")
    assert "] Spielleitung:" in transkript and "[0:00:00]" in transkript
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


def test_pruefliste_lesen_und_anwenden():
    from app import modellvergleich as mv

    text = """# Kommentar
[Kapitel]
Zehn Tage Kerker :: Kerker; zehn Tage/10 Tage
Pipo gibt sein Schwert :: Pipo; Schwert/Lostriana
[Vorschläge]
Pipo :: Pipo
Grünkappen
"""
    punkte = mv.pruefliste_lesen(text)
    assert [p["bereich"] for p in punkte] == ["Kapitel", "Kapitel", "Vorschläge", "Vorschläge"]
    assert punkte[0]["woerter"] == [["kerker"], ["zehn tage", "10 tage"]] and punkte[3]["woerter"] == [["grünkappen"]]

    e = mv.Ergebnis(modell="m", kontext=1, ok=True, notizen="[0:01] Zehn Tage im Kerker.\n[1:00] Pipo gibt Lostriana.",
                    verlauf="", titel="Kapitel 1", text="Nach 10 Tagen Kerker floh die Gruppe. Pipo half.",
                    vorschlaege=[{"title": "Pipo", "detail": "Wache", "gmNotes": None}])
    ein = {"transkript": [{"start": 0.0, "sprecher": "Spielleitung", "member_id": None, "text": "Zehn Tage Kerker. Pipo gibt euch sein Schwert."}]}
    aus = mv.pruefliste_anwenden(punkte, ein, e)
    assert aus[0]["vorkommen"] == {"Transkript": True, "Notizen": True, "Teile": None, "Kapitel 1": None, "Kapitel 2": None,
                                   "Kapitel": True, "Vorschläge": False}
    assert aus[1]["vorkommen"]["Kapitel"] is False and aus[1]["vorkommen"]["Notizen"] is True  # im Kapitel verloren
    assert aus[2]["vorkommen"] == {"Transkript": True, "Notizen": True, "Teile": None, "Kapitel 1": None, "Kapitel 2": None,
                                   "Kapitel": None, "Vorschläge": True}
    e.pruefliste = aus
    md = mv.pruefliste_md(e)
    assert "| 2 | Pipo gibt sein Schwert | ✓ | ✓ | · | · | · | – | – |" in md and "*Kapitel: 1 von 2*" in md
    e.kapitel1 = "Nach 10 Tagen Kerker floh die Gruppe."  # erster Entwurf ohne Pipo → Ergänzung hat ihn gerettet
    aus = mv.pruefliste_anwenden(punkte, ein, e)
    assert aus[0]["vorkommen"]["Kapitel 1"] is True and aus[1]["vorkommen"]["Kapitel 1"] is False


def test_modellvergleich_ohne_nachbesserung_und_warnungen(client, world, dbs, tmp_path):
    """--ohne-nachbesserung lässt das Kapitel stehen; eine ausgefallene Gegenprüfung landet mit Antwortanfang im Ergebnis."""
    from app import modellvergleich as mv

    s = _zur_pruefung(client, world, dbs, tmp_path)
    server = mv.Server("http://testserver", client=client)
    server.anmelden("anna", "geheim123")
    einst = mv.Einstellungen(nachbesserung=False)
    meldungen = []
    ordner = mv.ausfuehren(server, s["id"], mv.modelle_lesen("a:1", 12288), "", 12288, "http://ollama",
                           tmp_path / "aus", meldungen.append, client=_ollama([]), einst=einst)
    erg = json.loads((ordner / "a_1-ctx12288" / "ergebnis.json").read_text(encoding="utf-8"))
    assert erg["nachgebessert"] is False and erg["text"].endswith("Ein Drache erschien.")
    assert erg["selbst"]["unsupported"] == 1  # geprüft wurde trotzdem
    # Gegenprüfung liefert Fließtext statt JSON → übersprungen, Grund und Antwortanfang bleiben erhalten
    meldungen = []
    ordner = mv.ausfuehren(server, s["id"], mv.modelle_lesen("a:1", 12288), "", 12288, "http://ollama",
                           tmp_path / "aus2", meldungen.append, client=_ollama([], pruefung_kaputt=True))
    erg = json.loads((ordner / "a_1-ctx12288" / "ergebnis.json").read_text(encoding="utf-8"))
    assert erg["selbst"]["unchecked"] == 2 and len(erg["warnungen"]) == 1
    assert erg["warnungen"][0]["schritt"] == "review" and "JSON" in erg["warnungen"][0]["fehler"]
    assert erg["warnungen"][0]["antwort"].startswith("Hier ist meine Einschätzung")
    assert any("Warnung (review)" in m for m in meldungen)
    assert "Warnung (review)" in (ordner / "bericht.md").read_text(encoding="utf-8")
