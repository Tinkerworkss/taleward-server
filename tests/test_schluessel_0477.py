"""Server 0.4.77: Ein neu eingetragener API-Schlüssel wird vor dem Speichern beim Anbieter geprüft; lehnt der ihn ab,
bleibt der bisherige. Schlüsselfelder bitten den Browser, kein gespeichertes Passwort einzusetzen."""
import httpx

from app import modellwahl
from tests.test_verwaltung import admin  # noqa: F401

DATEN = {"art": "api", "anbieter": "mistral", "api_modell": "mistral-medium-latest", "lokal_modell": "auto",
         "lokal_kontext": "12288"}


def test_abgelehnter_schluessel_ueberschreibt_nicht(client, dbs, admin, monkeypatch):  # noqa: F811
    from app.einstellungen import extern_konfig, llm_konfig

    r = client.post("/verwaltung/zusammenfassung", data={**DATEN, "csrf": admin, "api_key": "sk-gueltig-abcdefgh1111"})
    assert r.status_code == 200
    monkeypatch.setattr(modellwahl, "schluessel_abgelehnt", lambda url, key: key.endswith("9999"))
    r = client.post("/verwaltung/zusammenfassung", data={**DATEN, "csrf": admin, "api_key": "browser-passwort-9999",
                                                         "api_modell": "mistral-large-latest"})
    assert r.status_code == 400 and "gespeichert wurde nichts" in r.text
    dbs.expire_all()
    k = llm_konfig(dbs)
    assert k.api_key.endswith("1111") and k.api_modell == "mistral-medium-latest"  # nichts geändert
    # leeres Feld: Modell wechseln ohne Probe, Schlüssel bleibt
    client.post("/verwaltung/zusammenfassung", data={**DATEN, "csrf": admin, "api_modell": "mistral-large-latest"})
    dbs.expire_all()
    assert llm_konfig(dbs).api_key.endswith("1111") and llm_konfig(dbs).api_modell == "mistral-large-latest"
    # dasselbe für die Transkription
    url = "/verwaltung/transkription/extern"
    client.post(url, data={"csrf": admin, "anbieter": "mistral", "api_key": "sk-gueltig-abcdefgh2222", "stunden": "12"})
    r = client.post(url, data={"csrf": admin, "anbieter": "mistral", "api_key": "browser-passwort-9999", "stunden": "12"})
    assert r.status_code == 400 and "gespeichert wurde nichts" in r.text
    dbs.expire_all()
    assert extern_konfig(dbs).api_key.endswith("2222")


def test_probe_beim_anbieter(monkeypatch):
    monkeypatch.setattr(modellwahl, "HOLEN", True)
    echt = httpx.Client

    def transport(status):
        def client(**kw):
            return echt(transport=httpx.MockTransport(lambda req: httpx.Response(status, json={"data": []})))
        return client

    monkeypatch.setattr(modellwahl.httpx, "Client", transport(401))
    assert modellwahl.schluessel_abgelehnt("https://llm.example/v1", "sk-falsch") is True
    monkeypatch.setattr(modellwahl.httpx, "Client", transport(200))
    assert modellwahl.schluessel_abgelehnt("https://llm.example/v1", "sk-gut") is False
    monkeypatch.setattr(modellwahl.httpx, "Client", transport(503))  # nicht erreichbar: nicht abgelehnt
    assert modellwahl.schluessel_abgelehnt("https://llm.example/v1", "sk-gut") is False


def test_kein_autofill_in_schluesselfeldern(client, admin):  # noqa: F811
    for seite in ("/verwaltung/zusammenfassung", "/verwaltung/transkription"):
        text = client.get(seite).text
        assert 'name="api_key" autocomplete="new-password"' in text and 'autocomplete="off"' not in text
