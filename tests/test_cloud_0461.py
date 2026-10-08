"""Server 0.4.61: Artefakt-Filter, eigenes Modell für Vorschläge, Modellauswahl statt Eingabe, 429/Zeitgrenze,
Probeläufe nacheinander."""
import json
import threading
import time

import httpx
import pytest

from app import artefakte
from app import sprachmodell as sm
from tests.test_step2b import _kleine_teile, transkribiert  # noqa: F401
from tests.test_step5 import Modell
from tests.test_verwaltung import admin  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")


# ---------------------------------------------------------------- Artefakt-Filter
def test_du_form_und_abgeschriebene_rede():
    text = ("Die Gruppe erreichte den Platz. Ihr erinnert euch, vor zwei Tagen war hier ein Fest. "
            "Und sobald ihr zu etwas Geld gekommen seid, ihr die Liste wieder abstottert vielleicht.\n\n"
            "Pipo sagte: „Ich will nicht mit euch gesehen werden.“ Dann ging er.")
    aus, b = artefakte.kapitel(text)
    assert aus == ("Die Gruppe erreichte den Platz.\n\n"
                   "Pipo sagte: „Ich will nicht mit euch gesehen werden.“ Dann ging er.")
    assert [x["art"] for x in b] == ["du_form", "du_form"]


def test_rede_ohne_schlusszeichen_und_ueber_mehrere_saetze():
    text = ("Eine Frau in Rüstung sprach: „Deine Prüfung ist gekommen. Bestrafe den wahren Mörder. Dann wurde alles "
            "schwarz.\n\nPipo sagte: „Es tut mir leid. Ich will nicht mit euch gesehen werden. Auf der Straße.“ Er ging.")
    aus, b = artefakte.kapitel(text)
    assert aus == text and b == []
    # „ihr“ als Fürwort der dritten Person bleibt
    assert artefakte.kapitel("Ihr Blick fiel auf den Galgen, ihr Bett war leer.")[1] == []


def test_regelsprache_satzteil_und_satz():
    aus, b = artefakte.kapitel("Lysander schluckte Wasser und verlor weitere Lebenspunkte, doch Orasilas zog ihn "
                               "heraus. Acht Lebenspunkte hatten sie verloren, ihre Kräfte waren geschwunden. "
                               "Der Wächter schlug zu, was ihn vier LeP kostete. Sie war erleichtert.")
    assert aus == ("Lysander schluckte Wasser, doch Orasilas zog ihn heraus. Ihre Kräfte waren geschwunden. "
                   "Der Wächter schlug zu. Sie war erleichtert.")
    assert [x["art"] for x in b] == ["regel", "regel", "regel"]


def test_namen_am_tisch_kraftausdruck_wiederholung_versalien():
    personen = [{"name": "Heiko Brandt", "rolle": "gm", "charakter": None},
                {"name": "Jana", "rolle": "player", "charakter": "Lysander"},
                {"name": "Tom", "rolle": "player", "charakter": None}]
    text = ("Sie fühlten sich Heiko, dem Altvater, ausgeliefert. Jana zog ihr Schwert. Tom lachte. "
            "Das ist doch scheiße, rief jemand. Orasilas wirkte BANBALADIN, doch der Wächter war schneller. "
            "Der Altvater bot ihnen Hilfe gegen einen Gefallen an. Arborn ist im Holzbein. "
            "Der Altvater bot ihnen Hilfe gegen einen Gefallen an.")
    aus, b = artefakte.kapitel(text, personen, ["Arborn"])
    assert aus == ("Lysander zog ihr Schwert. Orasilas wirkte Banbaladin, doch der Wächter war schneller. "
                   "Der Altvater bot ihnen Hilfe gegen einen Gefallen an. Arborn ist im Holzbein.")
    assert [x["art"] for x in b] == ["name_am_tisch", "name_ersetzt", "name_am_tisch", "kraftausdruck",
                                     "wiederholung"]
    # Name, der zugleich Figur oder Bibel-Eintrag ist, bleibt
    assert artefakte.kapitel("Arborn wartet.", [{"name": "Arborn", "rolle": "player", "charakter": None}],
                             ["Arborn"])[0] == "Arborn wartet."


def test_vorschlag_saeubern():
    v = artefakte.vorschlag({"title": "Schwert von Pipo", "detail": "Ein Schwert, das Pipo den Spielern überlässt. "
                             "Ihr findet es im Krater.", "gmNotes": "Kann BANBALADIN brechen."})
    assert v["detail"] == "Ein Schwert, das Pipo der Gruppe überlässt."
    assert v["gmNotes"] == "Kann Banbaladin brechen."


# ---------------------------------------------------------------- zwei Modelle, Kosten, Filter im Ablauf
def test_vorschlaege_mit_eigenem_modell(client, world, dbs, tmp_path):
    from app.models import GameSession
    from app.zusammenfassung import eingabe_bauen, recap_eingabe, vorschlag_eingabe

    s = transkribiert(client, world, dbs, tmp_path)
    basis = eingabe_bauen(dbs, dbs.get(GameSession, s["id"]))
    modell = Modell(vorschlaege=[{"action": "create", "entryType": "npc", "title": "Bolko",
                                  "detail": "Der Wirt hilft den Spielern.", "suggestedVisibility": "public"}])
    transport = httpx.MockTransport(modell)
    kapitel = sm.OpenAIKlient("https://llm.example/v1", "sk", "kapitel-m", httpx.Client(transport=transport),
                              cent_pro_mio=(100.0, 500.0))
    vorschlag = sm.OpenAIKlient("https://llm.example/v1", "sk", "vorschlag-m", httpx.Client(transport=transport),
                                cent_pro_mio=(1000.0, 1000.0))
    ablauf = sm.Ablauf(kapitel, vorschlag_klient=vorschlag, schritt=lambda _n: None)
    d = ablauf.ausfuehren(recap_eingabe(basis), vorschlag_eingabe(dbs, dbs.get(GameSession, s["id"]), basis))
    modelle = [a["body"]["model"] for a in modell.aufrufe]
    assert modelle[-1] == "vorschlag-m" and set(modelle[:-1]) == {"kapitel-m"}
    # Kosten je Modell: (n-1) × (100k × 100 + 4k × 500) / 1 Mio. + 1 × (104k × 1000) / 1 Mio. Cent
    n = len(modell.aufrufe)
    assert d["costCents"] == round((n - 1) * 12 + 104)
    assert d["model"] == "kapitel-m"
    if d["proposals"]:
        assert "den Spielern" not in json.dumps(d["proposals"], ensure_ascii=False)


def test_modell_auswahl_in_der_verwaltung(client, dbs, admin):  # noqa: F811
    from app import modellwahl
    from app.einstellungen import llm_konfig

    url = "/verwaltung/zusammenfassung"
    basis = {"csrf": admin, "art": "api", "anbieter": "mistral", "lokal_modell": "auto", "lokal_kontext": "12288",
             "api_key": "sk-abcdefghijklmnop1234"}
    seite = client.get(url).text
    assert '<select name="api_modell" id="anbieter-modell">' in seite
    assert '<option value="mistral-medium-latest"' in seite and '<option value="mistral-large-latest"' in seite
    assert 'name="api_modell_vorschlaege"' in seite
    # Kapitel Medium, Vorschläge Large
    r = client.post(url, data={**basis, "api_modell": "mistral-medium-latest",
                               "api_modell_vorschlaege": "mistral-large-latest"}, follow_redirects=False)
    assert r.status_code == 303
    k = llm_konfig(dbs)
    assert (k.api_modell, k.api_modell_vorschlaege) == ("mistral-medium-latest", "mistral-large-latest")
    seite = client.get(url).text
    assert '<option value="mistral-large-latest" selected>' in seite
    # gleiches Modell zweimal → „wie für das Kapitel“
    client.post(url, data={**basis, "api_modell": "mistral-large-latest", "api_modell_vorschlaege": "mistral-large-latest"})
    dbs.expire_all()
    assert llm_konfig(dbs).api_modell_vorschlaege == ""
    # nur Modelle aus der Auswahl
    r = client.post(url, data={**basis, "api_modell": "erfunden-latest"})
    assert r.status_code == 400 and "aus der Liste" in r.text
    r = client.post(url, data={**basis, "api_modell": "mistral-medium-latest", "api_modell_vorschlaege": "erfunden"})
    assert r.status_code == 400
    # Liste des Anbieters erweitert die Auswahl, Nicht-Text-Modelle fallen heraus
    modellwahl.merken(dbs, "https://api.mistral.ai/v1", ["magistral-medium-latest", "mistral-embed", "mistral-ocr-latest"])
    dbs.commit()
    seite = client.get(url).text
    assert "magistral-medium-latest" in seite and "mistral-embed" not in seite and "mistral-ocr" not in seite
    assert client.post(url, data={**basis, "api_modell": "magistral-medium-latest"},
                       follow_redirects=False).status_code == 303


def test_modellliste_wird_beim_anbieter_geholt(client, dbs, admin, monkeypatch):  # noqa: F811
    from app import modellwahl
    from app.einstellungen import llm_konfig

    client.post("/verwaltung/zusammenfassung", data={
        "csrf": admin, "art": "api", "anbieter": "mistral", "api_modell": "mistral-medium-latest",
        "lokal_modell": "auto", "lokal_kontext": "12288", "api_key": "sk-abcdefghijklmnop1234"})
    aufrufe = []

    def anbieter(req):
        aufrufe.append(req.url.path)
        return httpx.Response(200, json={"data": [{"id": "mistral-medium-2508"}, {"id": "voxtral-small-latest"}]})

    echt = httpx.Client
    monkeypatch.setattr(modellwahl, "HOLEN", True)
    monkeypatch.setattr(modellwahl.httpx, "Client", lambda **kw: echt(transport=httpx.MockTransport(anbieter)))
    seite = client.get("/verwaltung/zusammenfassung").text
    assert aufrufe == ["/v1/models"] and "mistral-medium-2508" in seite and "voxtral" not in seite
    client.get("/verwaltung/zusammenfassung")
    assert aufrufe == ["/v1/models"]  # nur einmal je Adresse
    assert modellwahl.auswahl(dbs, llm_konfig(dbs))[:2] == ["mistral-medium-latest", "mistral-large-latest"]


# ---------------------------------------------------------------- 429 und Zeitgrenze
def test_429_wartet_wie_vorgegeben(monkeypatch):
    pausen, antworten = [], [httpx.Response(429, headers={"retry-after": "7"}, json={"message": "Rate limit"}),
                             httpx.Response(429, json={"message": "Rate limit"}),
                             httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})]
    monkeypatch.setattr(sm.time, "sleep", pausen.append)
    k = sm.OpenAIKlient("https://llm.example/v1", "sk", "m",
                        client=httpx.Client(transport=httpx.MockTransport(lambda _r: antworten.pop(0))))
    assert k.chat("s", "n").text == "{}"
    assert pausen == [7.0, 30.0]  # Retry-After, sonst 15 · 2^Versuch
    assert sm.wartezeit(httpx.Response(429, headers={"retry-after": "900"}), 0) == 120.0


def test_keine_antwort_bricht_ab(monkeypatch):
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)

    def haengt(req):
        raise httpx.ReadTimeout("zu lange", request=req)

    k = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", client=httpx.Client(transport=httpx.MockTransport(haengt)))
    with pytest.raises(sm.SprachmodellFehler) as f:
        k.chat("s", "n")
    assert "nicht rechtzeitig" in str(f.value)
    assert sm.OpenAIKlient("https://x/v1", "sk", "m").client.timeout.read == 300.0


# ---------------------------------------------------------------- Probeläufe nacheinander
def test_probelaeufe_nacheinander(client, world, dbs, tmp_path, admin):  # noqa: F811
    from app import kapitelprobe as probelauf
    from app.models import Campaign, Member, User
    from tests.test_step5 import einstellen

    s = transkribiert(client, world, dbs, tmp_path)
    einstellen(dbs, art="attrappe")
    chef = dbs.query(User).filter_by(username="chef").one()
    dbs.add(Member(campaign_id=world["cid"], user_id=chef.id, role="gm"))
    dbs.get(Campaign, world["cid"]).allow_cloud_summary = True
    dbs.commit()
    probelauf._REIHE.acquire()  # ein anderer Probelauf rechnet gerade
    try:
        r = client.post("/verwaltung/probelauf", data={"csrf": admin, "session": s["id"]}, follow_redirects=False)
        pid = r.headers["location"].rsplit("/", 1)[1]
        for _ in range(50):
            if probelauf.lesen(pid).schritt == "wartet":
                break
            time.sleep(0.05)
        assert probelauf.lesen(pid).schritt == "wartet"
        assert "Wartet auf den vorigen Probelauf" in client.get(f"/verwaltung/probelauf/{pid}").text
    finally:
        probelauf._REIHE.release()
    for _ in range(100):
        if probelauf.lesen(pid).zustand != "läuft":
            break
        time.sleep(0.05)
    assert probelauf.lesen(pid).zustand == "fertig"
    assert not any(t.name.startswith("probelauf-") and t.is_alive() for t in threading.enumerate())
