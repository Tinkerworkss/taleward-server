"""Taleward-Box (Raspberry Pi): Ersteinrichtung ohne Code aus dem Heimnetz, Abbild-Bauskript."""
import re
from pathlib import Path
from types import SimpleNamespace

WURZEL = Path(__file__).resolve().parent.parent


def _anfrage(host):
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None)


def test_heimnetz_ohne_code_nur_wenn_eingeschaltet_und_privat(monkeypatch):
    from app import config
    from app.einrichtung import heimnetz_ohne_code

    config.get_settings.cache_clear()
    assert not heimnetz_ohne_code(_anfrage("192.168.178.20"))  # Standard: aus
    monkeypatch.setenv("EINRICHTUNG_IM_HEIMNETZ", "true")
    config.get_settings.cache_clear()
    for privat in ("192.168.178.20", "10.0.0.5", "172.17.0.1", "127.0.0.1", "fe80::1", "fd00::5"):
        assert heimnetz_ohne_code(_anfrage(privat)), privat
    for fremd in ("8.8.8.8", "2a00:1450::1", "testclient", None):
        assert not heimnetz_ohne_code(_anfrage(fremd)), fremd
    config.get_settings.cache_clear()


def test_box_einrichtung_ohne_code(client, dbs, monkeypatch):
    from app import einrichtung

    einrichtung.beim_start(dbs)
    # ohne Freigabe: wie immer nur mit Code
    assert 'name="username"' not in client.get("/verwaltung/einrichtung").text
    monkeypatch.setattr(einrichtung, "heimnetz_ohne_code", lambda request: True)
    seite = client.get("/verwaltung/einrichtung").text
    assert 'name="username"' in seite
    code = re.search(r'name="code" value="([^"]+)"', seite).group(1)
    body = {"username": "vera", "display_name": "Vera", "password": "Drachenfeuer7", "password2": "Drachenfeuer7",
            "code": code}
    r = client.post("/verwaltung/einrichtung", data=body, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/verwaltung/assistent/verein"
    # nur einmal: danach führt die Seite zur Anmeldung
    client.cookies.clear()
    assert client.get("/verwaltung/einrichtung", follow_redirects=False).headers["location"] == "/verwaltung/anmelden"


def test_box_dateien():
    erst = (WURZEL / "box" / "erststart.sh").read_text(encoding="utf-8")
    assert "TALEWARD_OHNE_FRAGEN=1" in erst and "TALEWARD_OHNE_BAU=1" in erst and "docker load" in erst
    assert "TALEWARD_EINRICHTUNG_IM_HEIMNETZ=true" in erst and "TALEWARD_MODUS=heimnetz" in erst
    dienst = (WURZEL / "box" / "taleward-erststart.service").read_text(encoding="utf-8")
    assert "ConditionPathExists=!/opt/taleward-box/eingerichtet" in dienst
    bauen = (WURZEL / "box" / "bauen.sh").read_text(encoding="utf-8")
    assert 'docker build -t "taleward-server:$FASSUNG"' in bauen  # Name wie in docker-compose.heimnetz.yml
    compose = (WURZEL / "deploy" / "docker-compose.heimnetz.yml").read_text(encoding="utf-8")
    assert "image: taleward-server:${TALEWARD_VERSION:-main}" in compose
    assert "EINRICHTUNG_IM_HEIMNETZ: ${TALEWARD_EINRICHTUNG_IM_HEIMNETZ:-false}" in compose
    install = (WURZEL / "install.sh").read_text(encoding="utf-8")
    assert "TALEWARD_OHNE_FRAGEN" in install and "TALEWARD_OHNE_BAU" in install
