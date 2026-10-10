"""Server 0.4.69: Korrektur per Hinweis der Spielleitung, zum Messen im Probelauf. Nur Absätze mit Hinweis ändern
sich (bei „Was fehlt“ höchstens einer mehr); alle anderen bleiben zeichengleich."""
import json
import time

import httpx
import pytest

from app import sprachmodell as sm
from tests.test_step2b import _kleine_teile, transkribiert  # noqa: F401
from tests.test_verwaltung import admin  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")

TEILE = ["Mara floh aus der Stadt und nahm das Schwert Eisenwind mit.",
         "Am Hafen wartete Hanna Kessler mit einem Boot und brachte die Gruppe über den Fluss.",
         "Im Morgengrauen erreichten sie das Kloster am Berg."]
TEXT = "\n\n".join(TEILE)


def test_nur_absaetze_mit_hinweis_aendern_sich():
    hinweise = {0: "Mara ließ das Schwert zurück."}
    d = {"absaetze": [{"nr": 1, "text": "Mara floh aus der Stadt und ließ das Schwert Eisenwind zurück."},
                      {"nr": 3, "text": "Im Morgengrauen erreichten sie das Kloster."}]}
    neu, verworfen = sm.korrektur_anwenden(d, TEILE, hinweise, "")
    assert list(neu) == [0] and verworfen == [{"absatz": 3, "grund": "ohne Hinweis geändert"}]


def test_freier_hinweis_aendert_hoechstens_so_viele_absaetze_wie_er_saetze_hat():
    d = {"absaetze": [{"nr": 2, "text": TEILE[1] + " Unterwegs erzählte sie vom Leuchtturm."},
                      {"nr": 3, "text": TEILE[2] + " Dort schliefen sie."}]}
    neu, verworfen = sm.korrektur_anwenden(d, TEILE, {}, "Hanna erzählt vom Leuchtturm.")
    assert list(neu) == [1] and verworfen == [{"absatz": 3, "grund": "ohne Hinweis geändert"}]
    neu, verworfen = sm.korrektur_anwenden(d, TEILE, {}, "Hanna erzählt vom Leuchtturm. Im Kloster schlafen sie.")
    assert list(neu) == [1, 2] and not verworfen
    assert sm.hinweis_saetze("Das Boot war ein Floß. Mara überlebt.\nEs fehlt die Ankunft") == 3


def test_aushoehlen_und_abschreiben_wird_verworfen():
    d = {"absaetze": [{"nr": 2, "text": "Ein Boot."}]}
    assert sm.korrektur_anwenden(d, TEILE, {1: "Das Boot war ein Floß."}, "")[1][0]["grund"] == "zu stark gekürzt"
    d = {"absaetze": [{"nr": 2, "text": "Laut Hinweis der Spielleitung wartete Hanna Kessler mit einem Floß am Hafen."}]}
    assert sm.korrektur_anwenden(d, TEILE, {1: "Floß statt Boot."}, "")[1][0]["grund"] == "Hinweis abgeschrieben"


class Anbieter:
    def __init__(self, antwort: dict):
        self.antwort, self.nutzer = antwort, ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][0]["content"].startswith("Du überarbeitest das Kapitel")
        self.nutzer = body["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.antwort)}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})


def test_ablauf_korrigieren_laesst_den_rest_zeichengleich():
    anbieter = Anbieter({"absaetze": [{"nr": 2, "text": "Am Hafen wartete Hanna Kessler mit einem Floß und "
                                                         "brachte die Gruppe über den Fluss."}]})
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", httpx.Client(transport=httpx.MockTransport(anbieter)))
    ein = {"kampagne": "K", "session_number": 1, "session_nummer": 1, "personen": [], "bibel": []}
    text, bericht = sm.Ablauf(klient).korrigieren(ein, TEXT, {1: "Es war ein Floß, kein Boot."}, "",
                                                   "[1:00] Hanna bringt die Gruppe mit dem Boot über den Fluss.")
    teile = text.split("\n\n")
    assert teile[0] == TEILE[0] and teile[2] == TEILE[2] and "Floß" in teile[1]
    assert {k: bericht[k] for k in ("anzahl", "geaendert", "verworfen", "ohne_aenderung")} == {
        "anzahl": 1, "geaendert": [2], "verworfen": [], "ohne_aenderung": []}
    assert bericht["pruefliste"][0]["umgesetzt"] and bericht["pruefliste"][0]["absaetze"][0]["absatz"] == 2
    assert "Absatz 2: Es war ein Floß" in anbieter.nutzer and "bei Widerspruch gilt der Hinweis" in anbieter.nutzer


def test_korrektur_im_probelauf(client, world, dbs, tmp_path, admin):  # noqa: F811
    from app import kapitelprobe as probelauf
    from app.models import Campaign, Member, User
    from tests.test_step5 import einstellen

    s = transkribiert(client, world, dbs, tmp_path)
    einstellen(dbs, art="attrappe")
    chef = dbs.query(User).filter_by(username="chef").one()
    dbs.add(Member(campaign_id=world["cid"], user_id=chef.id, role="gm"))
    dbs.get(Campaign, world["cid"]).allow_cloud_summary = True
    dbs.commit()
    r = client.post("/verwaltung/probelauf", data={"csrf": admin, "session": s["id"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[1]
    for _ in range(100):
        if probelauf.lesen(pid).zustand != "läuft":
            break
        time.sleep(0.05)
    seite = client.get(f"/verwaltung/probelauf/{pid}").text
    assert 'name="h0"' in seite and 'name="fehlt"' in seite and "Stimmt etwas nicht oder fehlt etwas?" in seite
    leer = client.post(f"/verwaltung/probelauf/{pid}/korrektur", data={"csrf": admin, "h0": "  "})
    assert "Bitte mindestens einen Hinweis eintragen." in leer.text
    vorher = probelauf.lesen(pid).text.split("\n\n")
    client.post(f"/verwaltung/probelauf/{pid}/korrektur", data={"csrf": admin, "h0": "Das stimmt so nicht."})
    for _ in range(100):
        if not probelauf.lesen(pid).korrektur_laeuft:
            break
        time.sleep(0.05)
    p = probelauf.lesen(pid)
    assert len(p.korrekturen) == 1 and p.korrekturen[0]["geaendert"] == [1]
    nachher = probelauf.aktueller_text(p).split("\n\n")
    assert nachher[0].endswith("(Testmodus: korrigiert)") and nachher[1:] == vorher[1:]
    seite = client.get(f"/verwaltung/probelauf/{pid}").text
    assert "Durchgang 1" in seite and "Stand nach 1 Korrekturdurchgängen" in seite
    assert "Zum Abhaken" in seite and "Das stimmt so nicht." in seite
    assert {"korrektur.json", "recap-korrigiert.txt"} <= {n for n in probelauf.DATEIEN if probelauf.datei(pid, n)}
    # fremdes Konto sieht nichts und kann nichts auslösen
    assert client.post("/verwaltung/probelauf/00000000-0000-0000-0000-000000000000/korrektur",
                       data={"csrf": admin, "h0": "x"}).url.path.endswith("/zusammenfassung")


def test_kuerzen_nennt_untergrenze():
    # 0.4.80: statt einer Zielspanne „streiche etwa N Wörter“; die Untergrenze prüft der Server (KUERZEN_UNTEN)
    assert "Streiche etwa {weg} Wörter – nicht mehr" in sm.SYSTEM_KUERZEN and sm.KUERZEN_UNTEN == 0.95
    assert sm.untergrenze({"transkript": [{"start": 9000}]}, True) == 900


def test_bericht_ueberschreibt_die_hinweise_nicht(client, world, dbs, tmp_path, admin, monkeypatch):  # noqa: F811
    """Mit Cloud-API: Der Durchgang speichert die Hinweise als Liste und die Anzahl getrennt (die Seite zeigt beides)."""
    from app import kapitelprobe as probelauf
    from app.kapitelprobe import Probe

    class Klient:
        modell = "m"

        def kosten_cent(self, *_a):
            return 0

    def korrigieren(self, ein, text, hinweise, frei="", notizen=""):
        teile = text.split("\n\n")
        teile[0] += " Richtig."
        return "\n\n".join(teile), {"anzahl": 2, "geaendert": [1], "verworfen": [], "ohne_aenderung": []}

    monkeypatch.setattr(sm.Ablauf, "korrigieren", korrigieren)
    monkeypatch.setattr("app.zusammenfassung.api_klient", lambda _k: Klient())
    p = Probe(id="11111111-1111-1111-1111-111111111111", session_id="s", kampagne="K", kapitel=1, quelle="Runde",
              zeilen=1, modell="m", gestartet="2026-10-09T00:00:00Z", zustand="fertig", text=TEXT, campaign_id="c",
              besitzer="b")
    probelauf._speichern(p)
    k = type("K", (), {"art": "api"})()
    probelauf._korrigieren(p, k, {}, {0: "Stimmt nicht."}, "Es fehlt etwas.")
    runde = probelauf.lesen(p.id).korrekturen[0]
    assert runde["hinweise"] == [{"absatz": 1, "text": "Stimmt nicht."}] and runde["anzahl"] == 2
    assert probelauf.aktueller_text(probelauf.lesen(p.id)).startswith(TEILE[0] + " Richtig.")


def test_freie_hinweise_einzeln_mit_geaenderten_saetzen():
    """0.4.71: Jeder Satz des freien Felds ist ein eigener Hinweis. Die Prüfliste zeigt je Hinweis die neuen und
    gestrichenen Sätze; ein Hinweis, den das Modell keinem geänderten Absatz zuordnet, gilt als nicht umgesetzt."""
    frei = "Hanna kam mit einem Floß. Mr. Kessler war nicht dabei.\nSie schliefen im Kloster."
    assert sm.hinweis_liste(frei) == ["Hanna kam mit einem Floß.", "Mr. Kessler war nicht dabei.",
                                      "Sie schliefen im Kloster."]
    anbieter = Anbieter({"absaetze": [{"nr": 2, "hinweise": [1], "text": "Am Hafen wartete Hanna Kessler mit einem "
                                       "Floß und brachte die Gruppe über den Fluss."},
                                      {"nr": 3, "hinweise": ["H3"], "text": TEILE[2] + " Dort schliefen sie."}]})
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", httpx.Client(transport=httpx.MockTransport(anbieter)))
    ein = {"kampagne": "K", "session_number": 1, "session_nummer": 1, "personen": [], "bibel": []}
    _text, bericht = sm.Ablauf(klient).korrigieren(ein, TEXT, {}, frei, "")
    assert "- H2 (ohne Absatzangabe): Mr. Kessler war nicht dabei." in anbieter.nutzer
    eins, zwei, drei = bericht["pruefliste"]
    assert eins["absaetze"] == [{"absatz": 2, "neu": ["Am Hafen wartete Hanna Kessler mit einem Floß und brachte die "
                                                       "Gruppe über den Fluss."], "weg": [TEILE[1]]}]
    assert not zwei["umgesetzt"] and zwei["absaetze"] == []
    assert drei["absaetze"][0] == {"absatz": 3, "neu": ["Dort schliefen sie."], "weg": []}
    assert bericht["anzahl"] == 3
