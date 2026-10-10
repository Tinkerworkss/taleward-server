"""Server 0.4.72: Kapitel über die Cloud-API standardmäßig „erst Notizen je Abschnitt, dann das Kapitel“; der frühere
Weg (ganze Abschrift auf einmal) bleibt in der Verwaltung wählbar."""
import pytest

from app import sprachmodell as sm
from tests.test_step2b import _kleine_teile  # noqa: F401 (Fixture)
from tests.test_step5 import api, einstellen  # noqa: F401 (Fixture)
from tests.test_verwaltung import admin  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")


def test_standard_ist_notizen_und_rueckweg_bleibt(client, dbs, admin):  # noqa: F811
    from app.einstellungen import llm_konfig

    assert llm_konfig(dbs).weg == "notizen"
    seite = client.get("/verwaltung/zusammenfassung").text
    assert "Weg zum Kapitel" in seite and 'value="notizen" checked' in seite
    daten = {"csrf": admin, "art": "api", "anbieter": "mistral", "api_modell": "mistral-medium-latest",
             "lokal_modell": "auto", "lokal_kontext": "12288", "api_key": "sk-abcdefghijklmnop1234"}
    client.post("/verwaltung/zusammenfassung", data={**daten, "weg": "abschrift"})
    dbs.expire_all()
    assert llm_konfig(dbs).weg == "abschrift"
    client.post("/verwaltung/zusammenfassung", data=daten)  # ältere Seite ohne das Feld ändert nichts
    dbs.expire_all()
    assert llm_konfig(dbs).weg == "abschrift"
    client.post("/verwaltung/zusammenfassung", data={**daten, "weg": "unsinn"})
    dbs.expire_all()
    assert llm_konfig(dbs).weg == "abschrift"


def test_echte_runde_nutzt_den_eingestellten_weg(dbs, monkeypatch):
    from app import zusammenfassung

    gesehen = []

    class Halt(Exception):
        pass

    def init(self, *_a, **kw):
        gesehen.append(kw.get("notizen_zuerst"))
        raise Halt

    monkeypatch.setattr(sm.Ablauf, "__init__", init)
    monkeypatch.setattr(zusammenfassung, "api_klient", lambda _k: None)
    monkeypatch.setattr(zusammenfassung, "api_klient_vorschlaege", lambda _k: None)
    monkeypatch.setattr(zusammenfassung, "eingabe_bauen", lambda *_a: {})
    einstellen(dbs, art="api", api_key="sk-abcdefghijklmnop1234")
    for weg, erwartet in ((None, True), ("abschrift", False), ("notizen", True)):
        if weg:
            einstellen(dbs, weg=weg)
        fn = zusammenfassung.zusammenfasser(dbs)
        try:
            fn(dbs, type("S", (), {"id": "s"})())
        except Halt:
            pass
        assert gesehen[-1] is erwartet


def test_notizenweg_ohne_geheimes(client, world, dbs, tmp_path, api):  # noqa: F811
    """Auch auf dem Weg „Notizen zuerst“ sieht nur der Vorschlagsaufruf die Bibel samt Geheimem."""
    from tests.test_step5 import mit_geheimnissen, status, zusammenfassen

    s, _oeff, _geheim, _teilweise = mit_geheimnissen(client, world, dbs, tmp_path)
    einstellen(dbs, art="api", api_key="sk-test-schluessel-1234")
    assert zusammenfassen(dbs)
    assert status(client, world["gm"], s["id"])["state"] == "awaiting_review"
    vorschlag = api.vorschlags_aufruf()
    andere = [a for a in api.aufrufe if a is not vorschlag]
    assert len(andere) >= 3 and any("Notizen" in a["system"] or "notizen" in a["system"] for a in andere)
    for a in andere:
        for verboten in ("MARKER", "Der Graue Fürst", "gmNotes"):
            assert verboten not in a["nutzer"] + a["system"], verboten
