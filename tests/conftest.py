
import pytest
from fastapi.testclient import TestClient

from tests.contract import CONTRACT


class ContractClient(TestClient):
    """TestClient, der jede Antwort automatisch gegen die YAML prüft."""

    def request(self, method, url, *args, **kwargs):
        resp = super().request(method, url, *args, **kwargs)
        CONTRACT.check(method, resp.request.url.path, resp.status_code, resp.headers.get("content-type"), resp.content)
        return resp


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-" + "x" * 40)
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173,http://localhost")
    monkeypatch.setenv("MAINTENANCE_INTERVAL_SECONDS", "0")  # Wartung rufen die Tests selbst auf
    monkeypatch.setenv("SUMMARIZER_INTERVAL_SECONDS", "0")  # Zusammenfassen ebenso
    monkeypatch.setenv("LOCAL_WORKER_AUTOSTART", "false")
    monkeypatch.setenv("EXTERNAL_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("EXTERNAL_TRANSCRIPTION", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # keine echte .env einlesen
    from app import config, db

    config.get_settings.cache_clear()
    db.reset_engine()
    from app.einstellungen import mindestversion_vergessen

    mindestversion_vergessen()  # Zwischenspeicher der App-Mindestversion gehört zur alten Datenbank
    from app.begrenzung import ZAEHLER

    ZAEHLER.vergessen()  # Begrenzung von Anmelde-, Registrierungs-, Kopplungs- und Einladungsversuchen
    from app.main import create_app

    with ContractClient(create_app(), base_url="http://testserver") as c:
        yield c
    db.reset_engine()
    config.get_settings.cache_clear()


@pytest.fixture()
def dbs(client):
    from app.db import session_factory

    s = session_factory()()
    yield s
    s.close()


@pytest.fixture()
def make_user(dbs):
    from app.cli import _neues_konto

    def _make(username: str, name: str | None = None, password: str = "geheim123"):
        u = _neues_konto(dbs, username.lower(), name or username.capitalize(), password)
        dbs.commit()
        return u

    return _make


@pytest.fixture()
def login(client):
    def _login(username: str, password: str = "geheim123") -> dict:
        r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['accessToken']}"}

    return _login


@pytest.fixture()
def world(client, make_user, login):
    """Kampagne mit SL (anna), Spieler (ben) und Außenstehender (cleo)."""
    for u in ("anna", "ben", "cleo"):
        make_user(u)
    gm, pl, out = login("anna"), login("ben"), login("cleo")
    c = client.post("/api/v1/campaigns", json={"title": "Rabenfels"}, headers=gm).json()
    code = client.post(f"/api/v1/campaigns/{c['id']}/invites", headers=gm).json()["code"]
    joined = client.post("/api/v1/campaigns/join", json={"code": code, "characterName": "Mira"}, headers=pl).json()
    members = {m["displayName"]: m["id"] for m in joined["members"]}
    return {
        "cid": c["id"], "gm": gm, "pl": pl, "out": out,
        "gm_member": members["Anna"], "pl_member": members["Ben"],
    }


@pytest.fixture(autouse=True)
def _freigabe_testschluessel(monkeypatch):
    """Freigaben in Tests mit einem eigenen Schlüssel (tests/freigabe_hilfe.py) statt dem echten."""
    from app import freigabe
    from tests.freigabe_hilfe import OEFFENTLICH

    monkeypatch.setattr(freigabe, "OEFFENTLICHER_SCHLUESSEL", OEFFENTLICH)


@pytest.fixture(autouse=True)
def _webapp_zwischenspeicher_leeren():
    """Erlaubte Herkünfte werden 30 s zwischengespeichert – zwischen Tests nicht mitnehmen."""
    from app import webapp

    webapp.vergessen()
    yield
    webapp.vergessen()


@pytest.fixture(autouse=True)
def _keine_modellliste_aus_dem_netz(monkeypatch):
    """Die Verwaltung holt die Modellliste beim Anbieter (0.4.61) – in Tests nur, wo ein Test es ausdrücklich will."""
    from app import modellwahl

    monkeypatch.setattr(modellwahl, "HOLEN", False)
