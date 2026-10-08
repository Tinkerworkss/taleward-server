"""Sprechermodell einmal auf dem Server, Worker laden es von dort (ohne Hugging-Face-Konto); Taleward-Spiegel."""
import hashlib
import json

import httpx
import pytest

REPO = "pyannote/speaker-diarization-community-1"
SHA = "c0ffee" * 6 + "abcd"
DATEIEN = {"config.yaml": b"pipeline: {}\n", "embedding/pytorch_model.bin": b"\x00gewichte" * 1000,
           "README.md": b"# community-1\n"}


def hf_transport(dateien=None, token="hf_gut", falsche_lfs=False, protokoll=None):
    dateien = dateien or DATEIEN

    def antwort(request: httpx.Request) -> httpx.Response:
        if protokoll is not None:
            protokoll.append(str(request.url))
        if request.headers.get("authorization") != f"Bearer {token}":
            return httpx.Response(401)
        pfad = request.url.path
        if pfad in (f"/api/models/{REPO}", f"/api/models/{REPO}/revision/{SHA}"):
            return httpx.Response(200, json={"sha": SHA})
        if pfad == f"/api/models/{REPO}/tree/{SHA}":
            eintraege = [{"type": "directory", "path": "embedding"}, {"type": "file", "path": "../boese"}]
            for p, inhalt in dateien.items():
                e = {"type": "file", "path": p, "size": len(inhalt)}
                if p.endswith(".bin"):
                    e["lfs"] = {"oid": "0" * 64 if falsche_lfs else hashlib.sha256(inhalt).hexdigest()}
                eintraege.append(e)
            return httpx.Response(200, json=eintraege)
        pre = f"/{REPO}/resolve/{SHA}/"
        if pfad.startswith(pre) and pfad[len(pre):] in dateien:
            return httpx.Response(200, content=dateien[pfad[len(pre):]])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(antwort))


@pytest.fixture()
def hf_zugang(dbs):
    from app.einstellungen import meta_schreiben

    meta_schreiben(dbs, "hf.token", "hf_gut")
    dbs.commit()


def test_holen_von_hugging_face(client, dbs, hf_zugang):
    from app import modellablage

    stand = modellablage.holen(dbs, klient=hf_transport())
    assert stand.fassung == SHA and stand.quelle == "huggingface"
    assert {d["pfad"] for d in stand.dateien} == set(DATEIEN)  # ../boese ignoriert
    ordner = modellablage._ordner(REPO, SHA)
    assert "CC-BY-4.0" in (ordner / "NOTICE.txt").read_text() and SHA in (ordner / "NOTICE.txt").read_text()
    assert modellablage.vorhanden(dbs).fassung == SHA
    assert modellablage.datei(dbs, REPO, SHA, "../chronik.db") is None
    assert modellablage.datei(dbs, REPO, SHA, "config.yaml").read_bytes() == DATEIEN["config.yaml"]
    # schon da → kein Netz
    assert modellablage.holen(dbs, klient=httpx.Client(transport=httpx.MockTransport(lambda r: 1 / 0))).fassung == SHA


def test_falsche_pruefsumme_und_kein_zugang(client, dbs, hf_zugang):
    from app import modellablage

    with pytest.raises(modellablage.AblageFehler, match="Prüfsumme"):
        modellablage.holen(dbs, klient=hf_transport(falsche_lfs=True))
    assert modellablage.vorhanden(dbs) is None
    assert not any(p.name.endswith(".laden") for p in modellablage.ablage().rglob("*"))  # aufgeräumt
    with pytest.raises(modellablage.AblageFehler, match="verweigert"):
        modellablage.holen(dbs, klient=hf_transport(token="anderer"))


def test_worker_laden_vom_server_ohne_hf(client, dbs, hf_zugang, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import modellablage, modelle
    from tests.test_step2a import worker_token

    h = {"Authorization": f"Bearer {worker_token(dbs)}"}
    wc = TestClient(client.app)
    assert wc.get("/worker/v1/models", params={"repo": REPO}, headers=h).status_code == 404
    assert wc.get("/worker/v1/config", headers=h).json()["hfToken"] == "hf_gut"  # Übergang: noch nicht auf dem Server
    modellablage.holen(dbs, klient=hf_transport())
    assert wc.get("/worker/v1/config", headers=h).json() == {"hfToken": None, "models": {REPO: SHA}, "serverVersion": "0.4.60"}
    assert wc.get("/worker/v1/models", params={"repo": REPO}).status_code == 401  # nur mit Worker-Token
    r = wc.get("/worker/v1/models/file", params={"repo": REPO, "fassung": SHA, "pfad": "../../geheimnis.txt"},
               headers=h)
    assert r.status_code == 404

    quelle = modelle.ServerQuelle("http://testserver", h["Authorization"].removeprefix("Bearer "), klient=wc)
    b = modelle.vom_server(REPO, quelle)
    assert b.quelle == "server" and b.fassung == SHA
    assert (b.pfad / "embedding" / "pytorch_model.bin").read_bytes() == DATEIEN["embedding/pytorch_model.bin"]
    assert modelle.gemerkt()[REPO] == SHA
    # zweiter Start: aus dem Zwischenspeicher
    monkeypatch.setattr(quelle, "laden", lambda *a: (_ for _ in ()).throw(AssertionError("nicht neu laden")))
    assert modelle.vom_server(REPO, quelle).pfad == b.pfad
    # Server gerade nicht erreichbar (WLAN beim Anmelden): die gemerkte, vollständige Fassung reicht
    import httpx

    monkeypatch.setattr(quelle, "verzeichnis", lambda repo: (_ for _ in ()).throw(httpx.ConnectError("aus")))
    offline = modelle.vom_server(REPO, quelle)
    assert offline.pfad == b.pfad and offline.quelle == "gemerkt"
    monkeypatch.undo()
    monkeypatch.setattr(quelle, "laden", lambda *a: (_ for _ in ()).throw(AssertionError("nicht neu laden")))

    # Manipulierte Datei auf dem Weg → Abbruch, nichts halb Geladenes bleibt
    import shutil

    shutil.rmtree(b.pfad)
    monkeypatch.setattr(modelle.ServerQuelle, "laden", lambda self, *a: "falsch")
    quelle2 = modelle.ServerQuelle("http://testserver", h["Authorization"].removeprefix("Bearer "), klient=wc)
    with pytest.raises(modelle.ModellFehler, match="Prüfsumme"):
        modelle.vom_server(REPO, quelle2)
    assert not b.pfad.exists()


def test_festgelegte_fassung_muss_passen(client, dbs, hf_zugang, monkeypatch):
    from fastapi.testclient import TestClient

    from app import modellablage, modelle
    from tests.test_step2a import worker_token

    modellablage.holen(dbs, klient=hf_transport())
    monkeypatch.setitem(modelle.FASSUNGEN, REPO, "f" * 40)
    quelle = modelle.ServerQuelle("http://testserver", worker_token(dbs), klient=TestClient(client.app))
    monkeypatch.setattr(modellablage, "FASSUNGEN", {})  # Server kennt die neue Festlegung noch nicht
    with pytest.raises(modelle.ModellFehler, match="dieselbe Version"):
        modelle.vom_server(REPO, quelle)


def test_spiegel(client, dbs, hf_zugang, tmp_path, monkeypatch):
    from app import modellablage
    from app.einstellungen import meta_schreiben

    modellablage.holen(dbs, klient=hf_transport())
    ordner, summe = modellablage.spiegel_erstellen(dbs, tmp_path / "spiegel")
    assert (ordner / "NOTICE.txt").exists()
    # Zweiter Server ohne Hugging-Face-Konto holt vom Spiegel
    import shutil

    shutil.rmtree(modellablage.ablage())
    meta_schreiben(dbs, "hf.token", "")
    meta_schreiben(dbs, f"modell.{REPO}", "")
    dbs.commit()
    monkeypatch.setenv("MODEL_MIRROR_URL", "https://spiegel.taleward.example/modelle")
    from app.config import get_settings

    get_settings.cache_clear()

    def spiegel(request: httpx.Request) -> httpx.Response:
        rel = request.url.path.removeprefix("/modelle/")
        p = tmp_path / "spiegel" / rel
        return httpx.Response(200, content=p.read_bytes()) if p.is_file() else httpx.Response(404)

    klient = httpx.Client(transport=httpx.MockTransport(spiegel))
    with pytest.raises(modellablage.AblageFehler, match="Hugging-Face-Zugang"):  # ohne Prüfsumme im Code: nicht genutzt
        modellablage.holen(dbs, klient=klient)
    monkeypatch.setitem(modellablage.SPIEGEL, REPO, (SHA, "0" * 64))
    with pytest.raises(modellablage.AblageFehler, match="Prüfsumme"):
        modellablage.holen(dbs, klient=klient)
    monkeypatch.setitem(modellablage.SPIEGEL, REPO, (SHA, summe))
    stand = modellablage.holen(dbs, klient=klient)
    assert stand.quelle == "spiegel" and stand.fassung == SHA
    # Manipulierte Datei im Spiegel
    shutil.rmtree(modellablage.ablage())
    meta_schreiben(dbs, f"modell.{REPO}", "")
    dbs.commit()
    (ordner / "config.yaml").write_bytes(b"boese")
    with pytest.raises(modellablage.AblageFehler, match="Prüfsumme"):
        modellablage.holen(dbs, klient=klient)


def test_automatisch_und_verwaltung(client, dbs, monkeypatch):  # noqa: F811
    from app import modellablage
    from app.einstellungen import meta_lesen, meta_schreiben
    from tests.test_verwaltung import admin as _admin  # noqa: F401

    assert modellablage.automatisch(dbs, klient=hf_transport()) is None  # nicht gebraucht
    meta_schreiben(dbs, "betrieb.art", "lokal")
    dbs.commit()
    assert modellablage.automatisch(dbs, klient=hf_transport()) is None  # keine Quelle
    meta_schreiben(dbs, "hf.token", "falsch")
    dbs.commit()
    assert modellablage.automatisch(dbs, klient=hf_transport()) is None
    assert "verweigert" in meta_lesen(dbs, "modellablage.fehler")
    meta_schreiben(dbs, "hf.token", "hf_gut")
    dbs.commit()
    assert modellablage.automatisch(dbs, klient=hf_transport()) is None  # frühestens nach einer Stunde
    meta_schreiben(dbs, "modellablage.versuch", "")
    dbs.commit()
    assert modellablage.automatisch(dbs, klient=hf_transport()).fassung == SHA
    assert meta_lesen(dbs, "modellablage.fehler") == ""
    manifest = json.loads((modellablage._ordner(REPO, SHA) / "manifest.json").read_text())
    assert manifest["fassung"] == SHA


def test_verwaltung_zeigt_modell(client, dbs, monkeypatch):
    from app import modellablage
    from tests.test_verwaltung import csrf_von  # noqa: F401

    from app.cli import _neues_konto

    _neues_konto(dbs, "chef", "Chefin", "geheim123", admin=True)
    dbs.commit()
    r = client.post("/verwaltung/anmelden", data={"username": "chef", "password": "geheim123"})
    seite = client.get("/verwaltung/transkription").text
    assert "noch nicht auf dem Server" in seite
    from app.einstellungen import meta_schreiben

    meta_schreiben(dbs, "hf.token", "hf_gut")
    dbs.commit()
    echt = modellablage.holen
    monkeypatch.setattr(modellablage, "holen", lambda db, repo=REPO, klient=None: echt(db, repo, hf_transport()))
    import re

    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/verwaltung/transkription").text).group(1)
    r = client.post("/verwaltung/transkription/modell", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    seite = client.get("/verwaltung/transkription").text
    assert "auf dem Server" in seite and SHA[:12] in seite and "CC-BY-4.0" in seite
    assert r
