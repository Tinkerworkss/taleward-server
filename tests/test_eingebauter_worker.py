"""Worker als Docker-Paket: Arbeitsweise nach Grafikspeicher, automatische Kopplung, Compose-Profile, Verwaltung."""
import os
import stat
from pathlib import Path

import yaml

from tests.test_verwaltung import admin  # noqa: F401 (Fixture)

WURZEL = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- Arbeitsweise
def test_stufen_wie_in_der_worker_app():
    from app.arbeitsweise import profil

    assert profil(12288)["modell"] == "large-v3" and profil(12288)["batch"] == 16
    assert (profil(8192)["modell"], profil(8192)["batch"]) == ("large-v3", 8)
    assert profil(8192, grenze_mb=4000)["modell"] == "large-v3-turbo"          # Grenze zählt
    klein = profil(3000)
    assert (klein["modell"], klein["ausrichten"], klein["sprecher"]) == ("large-v3-turbo", "cpu", "cpu")
    for p in (profil(None), profil(1500), profil(8192, prozessor=True)):
        assert (p["geraet"], p["genauigkeit"], p["modell"]) == ("cpu", "int8", "large-v3-turbo")
    assert profil(None, modell_wunsch="large-v3")["modell"] == "large-v3"
    assert profil(4000, modell_wunsch="large-v3")["ausrichten"] == "cpu"       # erzwungen großes Modell
    assert profil(12288, modell_wunsch="large-v3-turbo")["modell"] == "large-v3-turbo"


def test_worker_art_aus_dem_compose_profil(monkeypatch):
    from app.config import Settings

    assert Settings(eingebauter_worker="").worker_art == ""
    assert Settings(eingebauter_worker="worker").worker_art == "gpu"
    assert Settings(eingebauter_worker="worker-cpu").worker_art == "cpu"
    assert Settings(eingebauter_worker="andere,worker").worker_art == "gpu"


# ---------------------------------------------------------------- Kopplung ohne Code
def _eingebaut(monkeypatch, tmp_path, art="worker"):
    from app import config

    datei = tmp_path / "kopplung" / "worker-token"
    monkeypatch.setenv("EINGEBAUTER_WORKER", art)
    monkeypatch.setenv("EINGEBAUTER_WORKER_DATEI", str(datei))
    config.get_settings.cache_clear()
    return datei


def test_server_legt_schluessel_fuer_den_eingebauten_worker_an(client, dbs, monkeypatch, tmp_path):
    from app.models import Worker
    from app.verwaltung.lokaler_worker import eingebauten_worker_koppeln

    datei = _eingebaut(monkeypatch, tmp_path)
    assert eingebauten_worker_koppeln(dbs) == datei
    erster = datei.read_text().strip()
    if os.name == "posix":
        assert stat.S_IMODE(datei.stat().st_mode) == 0o600  # nur für den Besitzer
    assert client.get("/worker/v1/config", headers={"Authorization": f"Bearer {erster}"}).status_code == 200
    w = dbs.query(Worker).filter_by(local=True).one()
    assert w.capabilities == "asr,llm"

    # Neustart des Servers: neuer Schlüssel, der alte gilt nicht mehr
    eingebauten_worker_koppeln(dbs)
    zweiter = datei.read_text().strip()
    assert zweiter != erster
    assert client.get("/worker/v1/config", headers={"Authorization": f"Bearer {erster}"}).status_code == 401
    assert client.get("/worker/v1/config", headers={"Authorization": f"Bearer {zweiter}"}).status_code == 200
    assert dbs.query(Worker).filter_by(local=True).count() == 1  # kein zweiter Eintrag


def test_ohne_profil_kein_schluessel(client, dbs, tmp_path):
    from app.verwaltung.lokaler_worker import eingebauten_worker_koppeln

    assert eingebauten_worker_koppeln(dbs) is None


def test_worker_liest_den_schluessel_aus_der_datei(tmp_path):
    from app.cli import _token_aus_datei

    datei = tmp_path / "worker-token"
    assert _token_aus_datei(None) is None
    assert _token_aus_datei(datei, warten_s=0) is None
    datei.write_text("wk.1.abc\n")
    assert _token_aus_datei(datei, warten_s=0) == "wk.1.abc"


# ---------------------------------------------------------------- Compose-Dateien
def test_compose_profile_fuer_den_worker():
    for name in ("docker-compose.yml", "docker-compose.heimnetz.yml"):
        d = yaml.safe_load((WURZEL / "deploy" / name).read_text(encoding="utf-8"))
        dienste = d["services"]
        server = dienste["server"]
        assert "kopplung:/kopplung" in " ".join(server["volumes"]), name
        assert server["environment"]["EINGEBAUTER_WORKER"] == "${COMPOSE_PROFILES:-}"
        assert "profiles" not in server  # der Server läuft immer
        gpu, cpu = dienste["worker"], dienste["worker-cpu"]
        assert gpu["profiles"] == ["worker"] and cpu["profiles"] == ["worker-cpu"]
        for w in (gpu, cpu):
            assert w["build"]["dockerfile"] == "Dockerfile.worker"
            assert "kopplung:/kopplung:ro" in w["volumes"] and "worker-modelle:/modelle" in w["volumes"]
            assert w["depends_on"]["server"]["condition"] == "service_healthy"
            assert "WORKER_MODELL" in w["environment"]
        assert gpu["build"]["args"]["TORCH_BACKEND"] == "cu128" and cpu["build"]["args"]["TORCH_BACKEND"] == "cpu"
        assert gpu["deploy"]["resources"]["reservations"]["devices"][0]["driver"] == "nvidia"
        assert "deploy" not in cpu and cpu["environment"]["WHISPER_DEVICE"] == "cpu"
        assert {"kopplung", "worker-modelle"} <= set(d["volumes"])


def test_dockerfile_worker():
    text = (WURZEL / "Dockerfile.worker").read_text(encoding="utf-8")
    assert "engine-requirements.txt" in text and "ARG TORCH_BACKEND=cu128" in text
    assert text.index("COPY engine-requirements.txt") < text.index("COPY . .")  # KI-Paket zuerst (Zwischenspeicher)
    assert '"--automatisch"' in text and "USER taleward" in text
    assert "/kopplung" in (WURZEL / "Dockerfile").read_text(encoding="utf-8")


# ---------------------------------------------------------------- Verwaltung
def test_verwaltung_im_docker_betrieb(client, admin, monkeypatch, tmp_path):  # noqa: F811
    from app import config

    monkeypatch.setenv("TALEWARD_DOCKER", "1")
    seite = client.get("/verwaltung/transkription").text
    assert "Auf diesem Server läuft kein Worker" in seite and "taleward-worker/releases" in seite
    assert "uv sync --extra ki" in seite  # alte Anleitung nur noch im eingeklappten Testmodus
    assert seite.index("<details") < seite.index("uv sync --extra ki")

    _eingebaut(monkeypatch, tmp_path, "worker-cpu")
    seite = client.get("/verwaltung/transkription").text
    assert "Eingebauter Worker" in seite and "logs -f worker-cpu" in seite
    assert "Auf diesem Server läuft kein Worker" not in seite
    config.get_settings.cache_clear()


def test_verwaltung_ohne_docker_wie_bisher(client, admin):  # noqa: F811
    seite = client.get("/verwaltung/transkription").text
    assert "Lokaler Worker (auf diesem Server)" in seite and "Auf diesem Server läuft kein Worker" not in seite


# ---------------------------------------------------------------- Einstellungen in der Verwaltung
def test_einstellungen_kommen_ueber_die_config(client, dbs, monkeypatch, tmp_path):
    from app import eingebaut
    from app.verwaltung.lokaler_worker import eingebauten_worker_koppeln
    from tests.test_step2a import worker_token

    datei = _eingebaut(monkeypatch, tmp_path)
    eingebauten_worker_koppeln(dbs)
    h = {"Authorization": f"Bearer {datei.read_text().strip()}"}
    assert client.get("/worker/v1/config", headers=h).json()["eingebaut"] == {}  # nichts gesetzt: .env gilt
    eingebaut.speichern(dbs, "6144", "large-v3-turbo", True, None)
    dbs.commit()
    e = client.get("/worker/v1/config", headers=h).json()["eingebaut"]
    assert (e["vramMb"], e["modell"], e["prozessor"]) == (6144, "large-v3-turbo", True) and e["neustart"]
    assert "threads" not in e
    eingebaut.speichern(dbs, "-5", "unsinn", None, "999999")  # Grenzen, Unbekanntes bleibt
    dbs.commit()
    e2 = client.get("/worker/v1/config", headers=h).json()["eingebaut"]
    assert (e2["vramMb"], e2["modell"], e2["threads"]) == (0, "large-v3-turbo", 256) and e2["neustart"] != e["neustart"]
    # Andere Worker bekommen die Einstellungen des eingebauten nicht
    fremd = {"Authorization": f"Bearer {worker_token(dbs)}"}
    assert "eingebaut" not in client.get("/worker/v1/config", headers=fremd).json()


def test_verwaltung_speichert_und_startet_neu(client, dbs, admin, monkeypatch, tmp_path):  # noqa: F811
    import json

    from app import config, eingebaut
    from app.models import Worker
    from app.verwaltung.lokaler_worker import eingebauten_worker_koppeln

    # ohne eingebauten Worker gibt es die Aktionen nicht
    assert client.post("/verwaltung/worker/eingebaut", data={"csrf": admin}, follow_redirects=False).status_code == 404
    _eingebaut(monkeypatch, tmp_path)
    eingebauten_worker_koppeln(dbs)
    w = dbs.query(Worker).filter_by(local=True).one()
    w.info = json.dumps({"gpu": "NVIDIA GeForce RTX 4060", "vramMb": 8192, "modell": "large-v3"})
    dbs.commit()
    eingebaut.messung_merken(dbs, w.id, 3600, 600, "large-v3", 5600)
    dbs.commit()
    seite = client.get("/verwaltung/transkription").text
    assert 'id="vram-regler"' in seite and 'max="8192"' in seite and "RTX 4060" in seite
    assert "Letzter Auftrag: 60 Minuten Aufnahme in 10.0 Minuten" in seite and "5.5 GB" in seite
    r = client.post("/verwaltung/worker/eingebaut", data={"csrf": admin, "vram_mb": "4096", "modell": "auto",
                                                          "prozessor_feld": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "worker_einstellungen" in r.headers["location"]
    werte = eingebaut.lesen(dbs)
    assert (werte["vramMb"], werte["modell"], werte["prozessor"]) == (4096, "auto", False) and werte["neustart"]
    alt = werte["neustart"]
    r = client.post("/verwaltung/worker/eingebaut/neustart", data={"csrf": admin}, follow_redirects=False)
    assert r.status_code == 303 and eingebaut.lesen(dbs)["neustart"] != alt
    config.get_settings.cache_clear()


def test_worker_beendet_sich_bei_geaenderten_einstellungen(tmp_path):
    import httpx

    from app.worker_prozess import WorkerProzess

    aufrufe = []
    client = httpx.Client(base_url="http://server", transport=httpx.MockTransport(
        lambda r: aufrufe.append(r.url.path) or httpx.Response(204)))
    k = WorkerProzess("http://server", "wk.1.x", tmp_path / "arbeit", lambda *a: {}, client=client,
                      neustart_noetig=lambda: True, nachsehen_s=0)
    k.laufen()  # kehrt zurück, statt auf Aufträge zu warten – Docker startet ihn neu
    assert k.neustart and aufrufe == []


# ---------------------------------------------------------------- Ollama für lokale Recaps
def test_compose_ollama_profile():
    for name in ("docker-compose.yml", "docker-compose.heimnetz.yml"):
        d = yaml.safe_load((WURZEL / "deploy" / name).read_text(encoding="utf-8"))
        dienste = d["services"]
        gpu, cpu = dienste["ollama"], dienste["ollama-cpu"]
        assert gpu["profiles"] == ["ollama"] and cpu["profiles"] == ["ollama-cpu"]
        for o in (gpu, cpu):
            assert o["image"].startswith("ollama/ollama:") and "ollama-modelle:/root/.ollama" in o["volumes"]
            assert o["healthcheck"]["test"] == ["CMD", "ollama", "list"]
        assert gpu["deploy"]["resources"]["reservations"]["devices"][0]["driver"] == "nvidia"
        assert "deploy" not in cpu and cpu["networks"]["default"]["aliases"] == ["ollama"]
        for w in (dienste["worker"], dienste["worker-cpu"]):
            assert w["environment"]["WORKER_LLM_URL"] == "http://ollama:11434"
            assert w["depends_on"]["ollama"] == {"condition": "service_healthy", "required": False}
            assert w["depends_on"]["ollama-cpu"]["required"] is False
        assert "ollama-modelle" in d["volumes"]


def test_ollama_art():
    from app.config import Settings

    assert Settings(eingebauter_worker="worker").ollama_art == ""
    assert Settings(eingebauter_worker="worker,ollama").ollama_art == "gpu"
    assert Settings(eingebauter_worker="worker-cpu,ollama-cpu").ollama_art == "cpu"
    assert Settings(eingebauter_worker="worker,ollama").worker_art == "gpu"


def test_verwaltung_zeigt_lokale_recaps(client, dbs, admin, monkeypatch, tmp_path):  # noqa: F811
    import json

    from app import config
    from app.einstellungen import meta_schreiben
    from app.models import Worker
    from app.verwaltung.lokaler_worker import eingebauten_worker_koppeln

    _eingebaut(monkeypatch, tmp_path, "worker,ollama")
    eingebauten_worker_koppeln(dbs)
    seite = client.get("/verwaltung/transkription").text
    assert "Ollama läuft mit" in seite and 'href="/verwaltung/zusammenfassung"' in seite
    w = dbs.query(Worker).filter_by(local=True).one()
    w.info = json.dumps({"gpu": "RTX", "vramMb": 8192, "llm": "ministral-3:8b"})
    from app.db import utcnow

    w.last_seen_at = utcnow()
    meta_schreiben(dbs, "llm.art", "lokal")
    dbs.commit()
    seite = client.get("/verwaltung/transkription").text
    assert "vom Worker erkannt" in seite and "mit dem lokalen Sprachmodell" in seite
    config.get_settings.cache_clear()
