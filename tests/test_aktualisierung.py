"""Updates: Der Server holt neue Fassungen von GitHub und bietet sie selbst an (App, Worker), meldet neue Server."""
import hashlib
import json

import httpx
import pytest

from tests.test_step2a import worker_token
from tests.test_verwaltung import admin, csrf_von  # noqa: F401 (Fixture)

APK = b"PK\x03\x04 taleward apk " * 100
EXE = b"MZ taleward setup " * 100


DOWNLOADS: dict[str, bytes] = {}  # Freigaben der Releases (release() trägt sie ein)


def github(releases_app, releases_worker, tags, dateien, zaehler=None, unsigniert=()):
    """Nachgebautes GitHub (API + Downloads). Server-Tags sind freigegeben, außer sie stehen in unsigniert."""
    from tests.freigabe_hilfe import freigabe

    server = {}
    for t in tags:
        if t.startswith("v") and t not in unsigniert:
            text, sig = freigabe("Tinkerworkss/taleward-server", t)
            server[f"/Tinkerworkss/taleward-server/releases/download/{t}/freigabe.txt"] = text
            server[f"/Tinkerworkss/taleward-server/releases/download/{t}/freigabe.txt.sig"] = sig.encode()

    def antwort(req: httpx.Request):
        if zaehler is not None:
            zaehler.append(str(req.url))
        pfad = req.url.path
        if pfad == "/repos/Tinkerworkss/taleward-app/releases":
            return httpx.Response(200, json=releases_app)
        if pfad == "/repos/Tinkerworkss/taleward-worker/releases":
            return httpx.Response(200, json=releases_worker)
        if pfad == "/repos/Tinkerworkss/taleward-server/tags":
            return httpx.Response(200, json=[{"name": t} for t in tags])
        for quelle in (dateien, server, DOWNLOADS):
            if pfad in quelle:
                return httpx.Response(200, content=quelle[pfad])
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(antwort), base_url="https://api.github.com")


def release(tag, datei=None, inhalt=b"", body="", digest=True, freigegeben=True, repo=None, freigabe_inhalt=None):
    """Release wie bei GitHub; mit freigegeben=True samt freigabe.txt(.sig) über die Datei (bzw. freigabe_inhalt)."""
    from tests.freigabe_hilfe import freigabe

    assets = []
    if datei:
        assets.append({"name": datei, "size": len(inhalt), "browser_download_url": f"https://dl.example/{tag}/{datei}",
                       **({"digest": "sha256:" + hashlib.sha256(inhalt).hexdigest()} if digest else {})})
    if freigegeben:
        repo = repo or ("Tinkerworkss/taleward-worker" if tag.startswith("worker-v") else "Tinkerworkss/taleward-app")
        text, sig = freigabe(repo, tag, {datei: freigabe_inhalt if freigabe_inhalt is not None else inhalt}
                             if datei else {})
        for name, wert in (("freigabe.txt", text), ("freigabe.txt.sig", sig.encode())):
            DOWNLOADS[f"/{tag}/{name}"] = wert
            assets.append({"name": name, "size": len(wert), "browser_download_url": f"https://dl.example/{tag}/{name}"})
    return {"tag_name": tag, "draft": False, "prerelease": False, "body": body, "html_url": f"https://gh/{tag}",
            "published_at": "2026-09-28T10:00:00Z", "assets": assets}


@pytest.fixture()
def gh():
    return github(
        [release("v1.2.0", "taleward-1.2.0.apk", APK, "Neu: Kapitel-Kommentare"), release("v1.1.0", "alt.apk", b"x")],
        [release("worker-v0.1.0", "TalewardWorker-Setup.exe", EXE, "Erste Fassung"), release("v0.4.1")],
        ["v0.4.1", "v0.5.0", "worker-v0.1.0"],
        {"/v1.2.0/taleward-1.2.0.apk": APK, "/worker-v0.1.0/TalewardWorker-Setup.exe": EXE})


def test_pruefen_laden_und_anbieten(client, dbs, gh):
    from app import aktualisierung

    ergebnis = aktualisierung.pruefen(dbs, gh)
    assert ergebnis == {"app": "1.2.0", "web": None, "worker-windows": "0.1.0", "worker-linux": "0.1.0",
                        "server": "0.5.0"}
    info = client.get("/api/v1/info").json()
    assert info["latestAppVersion"] == "1.2.0" and info["releaseNotes"] == "Neu: Kapitel-Kommentare"
    assert info["appDownloadUrl"] == "http://testserver/downloads/app/1.2.0/taleward-1.2.0.apk"  # eigener Server
    r = client.get("/downloads/app/1.2.0/taleward-1.2.0.apk")
    assert r.status_code == 200 and r.content == APK
    assert r.headers["content-type"] == "application/vnd.android.package-archive"
    assert client.get("/downloads/app/1.2.0/../../geheimnis.txt").status_code == 404
    assert client.get("/downloads/app/1.1.0/alt.apk").status_code == 404  # nicht freigegeben
    # Worker bekommt seine Fassung vom Server
    h = {"Authorization": f"Bearer {worker_token(dbs)}"}
    w = client.get("/worker/v1/app-update", params={"system": "windows"}, headers=h).json()
    assert w["version"] == "0.1.0" and w["sha256"] == hashlib.sha256(EXE).hexdigest()
    assert w["url"].endswith("/downloads/worker-windows/0.1.0/TalewardWorker-Setup.exe")
    assert client.get(w["url"].removeprefix("http://testserver")).content == EXE
    linux = client.get("/worker/v1/app-update", params={"system": "linux"}, headers=h).json()
    assert linux["version"] == "0.1.0" and "url" not in linux and linux["tag"] == "worker-v0.1.0"
    assert client.get("/worker/v1/app-update").status_code == 401


def test_ohne_updates_keine_angaben(client, dbs):
    info = client.get("/api/v1/info").json()
    assert info.get("latestAppVersion") is None
    h = {"Authorization": f"Bearer {worker_token(dbs)}"}
    assert client.get("/worker/v1/app-update", headers=h).status_code == 204


def test_von_hand_eingetragen_gilt_vor(client, dbs, gh):
    from app import aktualisierung
    from app.einstellungen import speichern

    aktualisierung.pruefen(dbs, gh)
    speichern(dbs, app_latest_version="1.3.0", app_download_url="https://example.org/t.apk")
    dbs.commit()
    info = client.get("/api/v1/info").json()
    assert info["latestAppVersion"] == "1.3.0" and info["appDownloadUrl"] == "https://example.org/t.apk"


def test_manuelle_freigabe(client, dbs, admin, gh):  # noqa: F811
    from app import aktualisierung

    aktualisierung.pruefen(dbs, gh)  # erste gefundene Fassung ist freigegeben
    r = client.post("/verwaltung/updates/modus", data={"csrf": admin, "modus": "manuell"}, follow_redirects=False)
    assert r.status_code == 303
    neu = github([release("v1.3.0", "taleward-1.3.0.apk", APK + b"!", "Noch neuer")], [], ["v0.4.1"],
                 {"/v1.3.0/taleward-1.3.0.apk": APK + b"!"})
    aktualisierung.pruefen(dbs, neu)
    assert client.get("/api/v1/info").json()["latestAppVersion"] == "1.2.0"  # noch nicht freigegeben
    assert client.get("/downloads/app/1.2.0/taleward-1.2.0.apk").status_code == 200  # alte Datei bleibt
    seite = client.get("/verwaltung/updates").text
    assert "1.3.0" in seite and "Freigeben" in seite and "Noch neuer" in seite
    r = client.post("/verwaltung/updates/freigeben", data={"csrf": admin, "art": "app", "version": "1.3.0"},
                    follow_redirects=False)
    assert "freigegeben" in r.headers["location"]
    assert client.get("/api/v1/info").json()["latestAppVersion"] == "1.3.0"
    assert client.get("/downloads/app/1.2.0/taleward-1.2.0.apk").status_code == 404


def test_falsche_pruefsumme_wird_verworfen(client, dbs):
    from app import aktualisierung
    from app.einstellungen import meta_lesen

    kaputt = release("v1.2.0", "taleward.apk", APK)
    gh = github([kaputt], [], [], {"/v1.2.0/taleward.apk": APK + b"manipuliert"})
    aktualisierung.pruefen(dbs, gh)
    assert "Prüfsumme" in meta_lesen(dbs, "update.fehler")
    assert client.get("/api/v1/info").json().get("latestAppVersion") is None
    assert not list((aktualisierung.ablage() / "app").rglob("*.apk"))


def test_neue_serverfassung_wird_gemeldet(client, dbs, admin, gh, monkeypatch):  # noqa: F811
    from app import aktualisierung, benachrichtigung

    gemeldet = []
    monkeypatch.setattr(benachrichtigung, "melden", lambda db, art, **w: gemeldet.append((art, w)))
    aktualisierung.pruefen(dbs, gh)
    aktualisierung.pruefen(dbs, gh)
    assert gemeldet == [("server_update", {"wichtig": False, "version": "0.5.0", "jetzt": "0.4.74"})]  # nur einmal
    seite = client.get("/verwaltung/updates").text
    assert "Update verfügbar" in seite and "git pull" in seite
    monkeypatch.setenv("TALEWARD_DOCKER", "1")
    assert "aktualisieren.sh --jetzt" in client.get("/verwaltung/updates").text
    client.cookies.set("tw_sprache", "en")
    assert "Update available" in client.get("/verwaltung/updates").text


def test_automatisch_nur_einmal_am_tag(client, dbs, gh, monkeypatch):
    from app import aktualisierung

    aufrufe = []
    monkeypatch.setattr(aktualisierung, "pruefen", lambda db, k=None: aufrufe.append(1))
    aktualisierung.automatisch(dbs)
    assert aufrufe == [1]
    from app.einstellungen import meta_schreiben
    from app.db import utcnow

    meta_schreiben(dbs, "update.geprueft", utcnow().isoformat())
    meta_schreiben(dbs, "update.fehler", "")
    dbs.commit()
    aktualisierung.automatisch(dbs)
    assert aufrufe == [1]
    monkeypatch.setenv("UPDATE_CHECK", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    meta_schreiben(dbs, "update.geprueft", "")
    dbs.commit()
    aktualisierung.automatisch(dbs)
    assert aufrufe == [1]
    get_settings.cache_clear()
    assert json  # Import genutzt


def test_server_automatisch_im_docker_paket(client, dbs, admin, gh, monkeypatch):  # noqa: F811
    """Das Host-Skript (deploy/aktualisieren.sh) liest server-auto.txt/server-jetzt und schreibt server-status.json."""
    from app import aktualisierung, benachrichtigung

    monkeypatch.setenv("TALEWARD_DOCKER", "1")
    aktualisierung.pruefen(dbs, gh)
    seite = client.get("/verwaltung/updates").text
    assert "Automatisch aktualisieren" in seite and "checked" in seite and "Jetzt aktualisieren" in seite
    client.post("/verwaltung/updates/server-auto", data={"csrf": admin}, follow_redirects=False)
    assert (aktualisierung.ablage() / "server-auto.txt").read_text() == "aus"
    client.post("/verwaltung/updates/server-auto", data={"csrf": admin, "an": "an"}, follow_redirects=False)
    assert aktualisierung.server_auto()
    r = client.post("/verwaltung/updates/server-jetzt", data={"csrf": admin}, follow_redirects=False)
    assert "server_jetzt" in r.headers["location"] and (aktualisierung.ablage() / "server-jetzt").exists()
    assert "Angefordert" in client.get("/verwaltung/updates").text
    # Ergebnis des Skripts: einmal melden, in der Verwaltung zeigen
    gemeldet = []
    monkeypatch.setattr(benachrichtigung, "melden", lambda db, art, **w: gemeldet.append((art, w)))
    (aktualisierung.ablage() / "server-status.json").write_text(
        '{"zeit": "2026-09-29T03:10:00Z", "ergebnis": "zurueckgenommen", "von": "v0.4.3", "nach": "v0.5.0", '
        '"meldung": "Die neue Fassung startete nicht"}')
    aktualisierung.server_status_melden(dbs)
    aktualisierung.server_status_melden(dbs)
    assert [g[0] for g in gemeldet] == ["server_update_fehler"] and gemeldet[0][1]["wichtig"] is True
    assert "Update auf v0.5.0 zurückgenommen" in client.get("/verwaltung/updates").text
