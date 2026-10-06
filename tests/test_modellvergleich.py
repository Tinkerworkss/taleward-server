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
        if system.startswith("Du extrahierst") or system.startswith("Du suchst im ORIGINALTRANSKRIPT"):
            inhalt = {"events": []}
        elif system.startswith("Du prüfst Ledger-Kandidaten"):
            inhalt = {"reviews": [], "coverage": [], "anchors": [], "encounters": []}
        elif system.startswith("Du ordnest aktuelle"):
            inhalt = {"links": []}
        elif system.startswith("Du prüfst"):
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
    assert erg["model_digest"] == "abc" and erg["ollama_version"] == "0.34.4"
    assert erg["selbst"]["supported"] == 1 and erg["richter"]["total"] == 2  # der Richter hat bewertet
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



def test_modellvergleich_nur_ledger_ueberspringt_recap_pipeline(client, world, dbs, tmp_path):
    """0.4.52: Ledger-Diagnose darf keine Notizen, Recap-, Review-, Proposal- oder Richter-Aufrufe auslösen."""
    from app import modellvergleich as mv

    s = _zur_pruefung(client, world, dbs, tmp_path)
    server = mv.Server("http://testserver", client=client)
    server.anmelden("anna", "geheim123")
    aufrufe = []
    ollama = _ollama(aufrufe)
    meldungen = []
    einst = mv.Einstellungen(nur_ledger=True, laeufe=1)
    ordner = mv.ausfuehren(server, s["id"], [("a:1", 12288)], "richter:1", 12288,
                           "http://ollama", tmp_path / "ledger", meldungen.append, client=ollama, einst=einst)

    systeme = [b["messages"][0]["content"] for b in aufrufe]
    assert systeme
    assert all(s.startswith(("Du extrahierst", "Du prüfst Ledger-Kandidaten", "Du ordnest aktuelle")) for s in systeme)
    assert not any(s.startswith("Du extrahierst aus EINEM Abschnitt") for s in systeme)
    assert not any(s.startswith("Du suchst im ORIGINALTRANSKRIPT") for s in systeme)
    assert all(b["model"] != "richter:1" for b in aufrufe)

    d = ordner / "a_1-ctx12288"
    ledger = json.loads((d / "ledger.json").read_text(encoding="utf-8"))
    erg = json.loads((d / "ergebnis.json").read_text(encoding="utf-8"))
    assert ledger["version"] == 4 and ledger["state"] == "ok"
    assert erg["nur_ledger"] is True and erg["grundlage"] == "Originaltranskript"
    assert not (d / "recap.txt").exists()
    assert not (d / "vorschlaege.json").exists()
    assert not (d / "plan.json").exists()
    assert any("Ledger (Schatten v4)" in m for m in meldungen)



def test_modellvergleich_nur_ledger_mit_atomic_gold_exportiert_harness(client, world, dbs, tmp_path):
    from app import modellvergleich as mv

    s = _zur_pruefung(client, world, dbs, tmp_path)
    server = mv.Server("http://testserver", client=client)
    server.anmelden("anna", "geheim123")
    gold = {
        "version": 1, "name": "unit", "scope": "selective",
        "facts": [{"id": "x", "match": {"assertions": [{
            "subject": ["Alrik"], "property": "life_status", "value": ["alive"]
        }]}}],
        "expectedNonClaims": [],
    }
    einst = mv.Einstellungen(nur_ledger=True, laeufe=1, ledger_gold=gold,
                            hardware_label="RTX Test / CUDA")
    ordner = mv.ausfuehren(server, s["id"], [("a:1", 12288)], "", 12288,
                           "http://ollama", tmp_path / "gold", lambda _x: None,
                           client=_ollama([]), einst=einst)
    d = ordner / "a_1-ctx12288"
    harness = json.loads((d / "ledger-harness.json").read_text(encoding="utf-8"))
    erg = json.loads((d / "ergebnis.json").read_text(encoding="utf-8"))
    assert harness["goldName"] == "unit"
    assert harness["metrics"]["factsTotal"] == 1
    assert (d / "ledger-harness.md").is_file()
    assert erg["ledgerHarnessMetrics"]["factsTotal"] == 1
    assert erg["hardware_label"] == "RTX Test / CUDA"
    assert erg["model_digest"] == "abc" and erg["ollama_version"] == "0.34.4"
    assert erg["ledger_harness"] is None


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

    e = mv.Ergebnis(modell="m", kontext=1, ok=True,
                    notizen="[0:01] Zehn Tage im Kerker.\n[1:00] Pipo gibt Lostriana.",
                    plan=[{"id": "N002", "teil": 1, "zeit": 60.0, "notiz": "[1:00] Pipo gibt Lostriana."}],
                    verlauf="", titel="Kapitel 1", text="Nach 10 Tagen Kerker floh die Gruppe. Pipo half.",
                    vorschlaege=[{"title": "Pipo", "detail": "Wache", "gmNotes": None}])
    ein = {"transkript": [{"start": 0.0, "sprecher": "Spielleitung", "member_id": None,
                           "text": "Zehn Tage Kerker. Pipo gibt euch sein Schwert."}]}
    aus = mv.pruefliste_anwenden(punkte, ein, e)
    assert aus[0]["vorkommen"] == {"Transkript": True, "Notizen": True, "Plan": False, "Ledger": None, "Ledger Teile (diagn.)": None, "Teile": None,
                                   "Kapitel 1": None, "Kapitel 2": None, "Kapitel": True, "Vorschläge": False}
    assert aus[1]["vorkommen"]["Kapitel"] is False and aus[1]["vorkommen"]["Notizen"] is True
    assert aus[1]["vorkommen"]["Plan"] is True
    assert aus[2]["vorkommen"] == {"Transkript": True, "Notizen": True, "Plan": True, "Ledger": None, "Ledger Teile (diagn.)": None, "Teile": None,
                                   "Kapitel 1": None, "Kapitel 2": None, "Kapitel": None, "Vorschläge": True}
    e.pruefliste = aus
    md = mv.pruefliste_md(e)
    assert "| 2 | Pipo gibt sein Schwert | ✓ | ✓ | ✓ | · | · | · | · | · | – | – |" in md
    assert "*Kapitel: 1 von 2*" in md
    e.kapitel1 = "Nach 10 Tagen Kerker floh die Gruppe."
    aus = mv.pruefliste_anwenden(punkte, ein, e)
    assert aus[0]["vorkommen"]["Kapitel 1"] is True and aus[1]["vorkommen"]["Kapitel 1"] is False


def test_pruefliste_ledger_verlangt_zusammengehoerige_fakten_im_selben_event():
    """0.4.51: Pipo irgendwo + Kindheit bei Lysander darf im Ledger nicht als Pipo-Kindheitsbeziehung zählen."""
    from app import modellvergleich as mv

    punkte = mv.pruefliste_lesen("[Kapitel]\nPipo seit Kindheit :: Pipo; Kindheit/klein")
    e = mv.Ergebnis(modell="m", kontext=1, ok=True, ledger={"events": [
        {"summary": "Pipo kennt Orasilas schon lange.", "actors": ["Pipo", "Orasilas"], "targets": [],
         "objects": [], "locations": [], "factions": [], "assertions": []},
        {"summary": "Lysander kennt Orasilas seit dessen Kindheit.", "actors": ["Lysander", "Orasilas"], "targets": [],
         "objects": [], "locations": [], "factions": [], "assertions": []},
    ]})
    ein = {"transkript": [{"start": 0.0, "sprecher": "Mira", "member_id": None,
                           "text": "Pipo und Lysander werden erwähnt."}]}
    aus = mv.pruefliste_anwenden(punkte, ein, e)
    assert aus[0]["vorkommen"]["Ledger"] is False

    e.ledger["events"][0]["assertions"] = [{
        "subject": "Pipo", "property": "relationship", "value": "kennt Orasilas seit dessen Kindheit"
    }]
    aus = mv.pruefliste_anwenden(punkte, ein, e)
    assert aus[0]["vorkommen"]["Ledger"] is True


def test_pruefliste_ledger_fakten_zeigt_atomisierung_als_obere_grenze():
    """0.4.52: Strict Ledger bleibt relationstreu; Ledger Teile (diagn.) zeigt getrennt, ob Teilfakten nur atomisiert wurden."""
    from app import modellvergleich as mv

    punkte = mv.pruefliste_lesen("[Kapitel]\nZusammengesetzter Punkt :: Galgen; Prinzessinnenmörder")
    e = mv.Ergebnis(modell="m", kontext=1, ok=True, ledger={"events": [
        {"summary": "Vier Stricke hängen am Galgen.", "actors": [], "targets": [], "objects": ["Galgen"],
         "locations": [], "factions": [], "assertions": []},
        {"summary": "Die Menge ruft Prinzessinnenmörder.", "actors": ["Menge"], "targets": [],
         "objects": [], "locations": [], "factions": [], "assertions": []},
    ]})
    ein = {"transkript": [{"start": 0.0, "sprecher": "Spielleitung", "member_id": None,
                           "text": "Vier Stricke am Galgen. Die Menge ruft Prinzessinnenmörder."}]}
    aus = mv.pruefliste_anwenden(punkte, ein, e)
    assert aus[0]["vorkommen"]["Ledger"] is False
    assert aus[0]["vorkommen"]["Ledger Teile (diagn.)"] is True
