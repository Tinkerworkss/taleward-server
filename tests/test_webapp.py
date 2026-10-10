"""Web-Fassung der App (Schnittstelle 0.4.2): CORS für die zentrale Web-App und die eigene Adresse, returnTo."""
from urllib.parse import parse_qs, urlsplit

from tests.test_anmeldung import VERIFIER, dienst  # noqa: F401 (Fixture)
from tests.test_verwaltung import admin  # noqa: F401 (Fixture)

API = "/api/v1"
ZENTRAL = "https://app.taleward.org"
ALT = "https://taleward.org"  # bis 0.4.50; wird nur noch in der Konfiguration gehoben


def vorab(client, herkunft):
    return client.options(f"{API}/campaigns", headers={
        "Origin": herkunft, "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization,content-type,x-taleward-app,x-chunk-sha256,accept-language"})


def test_cors_zentral_eigene_und_fremde(client, dbs, admin):  # noqa: F811
    from app import webapp
    from app.einstellungen import speichern

    webapp.vergessen()
    r = vorab(client, ZENTRAL)
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ZENTRAL
    assert "PATCH" in r.headers["access-control-allow-methods"]
    assert "x-chunk-sha256" in r.headers["access-control-allow-headers"].lower()
    r = client.get(f"{API}/info", headers={"Origin": ZENTRAL})
    assert r.headers["access-control-allow-origin"] == ZENTRAL
    assert "access-control-allow-origin" not in client.get(f"{API}/info", headers={"Origin": "https://boese.example"}).headers
    assert vorab(client, "https://boese.example").status_code == 400
    assert vorab(client, ALT).status_code == 400  # alte Adresse seit der Umleitung der Website nicht mehr erlaubt
    # eigene Adresse (für /app/ auf diesem Server)
    speichern(dbs, public_url="https://taleward.meinverein.de")
    dbs.commit()
    webapp.vergessen()
    assert vorab(client, "https://taleward.meinverein.de").headers["access-control-allow-origin"] == \
        "https://taleward.meinverein.de"
    # In der Verwaltung abschalten
    seite = client.get("/verwaltung/einstellungen").text
    assert "Die Web-App auf https://app.taleward.org darf diesen Server nutzen" in seite
    csrf = admin
    r = client.post("/verwaltung/einstellungen", data={"csrf": csrf, "server_name": "S", "server_operator": "B",
                                                       "min_age": "16", "web_zentral_feld": "1",
                                                       "public_url": "https://taleward.meinverein.de"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert vorab(client, ZENTRAL).status_code == 400 and vorab(client, ALT).status_code == 400
    assert not webapp.zentral_erlaubt(dbs)
    # App im Emulator bleibt immer erlaubt
    assert vorab(client, "http://localhost").headers["access-control-allow-origin"] == "http://localhost"


def test_return_to_fuer_die_web_app(client, world, dbs, dienst):  # noqa: F811
    from tests.test_anmeldung import challenge_von

    params = {"challenge": challenge_von(VERIFIER), "purpose": "login"}
    # fremdes Ziel → abgelehnt, zurück in die App
    r = client.get(f"{API}/auth/oidc/google/start", params={**params, "returnTo": "https://boese.example/#/auth"},
                   follow_redirects=False)
    assert r.headers["location"] == "taleward://auth?error=return_to_not_allowed"
    # unbekannter Dienst mit erlaubtem Ziel → Fehler dorthin; die zentrale Web-App liegt an der Wurzel
    r = client.get(f"{API}/auth/oidc/apple/start", params={**params, "returnTo": f"{ZENTRAL}/#/auth"},
                   follow_redirects=False)
    assert r.headers["location"] == f"{ZENTRAL}/#/auth?error=provider_unknown"
    r = client.get(f"{API}/auth/oidc/apple/start", params={**params, "returnTo": f"{ALT}/app/#/auth"},
                   follow_redirects=False)
    assert r.headers["location"] == "taleward://auth?error=return_to_not_allowed"  # alte Adresse nicht mehr
    r = client.get(f"{API}/auth/oidc/google/start", params={**params, "returnTo": f"{ZENTRAL}/app/#/auth"},
                   follow_redirects=False)
    assert r.headers["location"] == "taleward://auth?error=return_to_not_allowed"  # /app/ gibt es dort nicht
    # ganzer Ablauf über die eigene Adresse
    ziel = "http://testserver/app/#/auth"
    r = client.get(f"{API}/auth/oidc/google/start", params={**params, "returnTo": ziel}, follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    dienst.nonce = q.get("nonce")
    dienst.claims = {"sub": "g-web", "email": "web@example.org", "email_verified": True, "name": "Web"}
    r = client.get("/auth/oidc/google/callback", params={"code": "gut", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(ziel + "?ticket=")
    ticket = parse_qs(r.headers["location"].split("?", 1)[1])["ticket"][0]
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": ticket, "verifier": VERIFIER})
    assert r.status_code == 200 and r.json()["status"] == "register"
    # Abbruch beim Dienst → Fehler in die Web-App
    r = client.get(f"{API}/auth/oidc/google/start", params={**params, "returnTo": ziel}, follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = client.get("/auth/oidc/google/callback", params={"error": "access_denied", "state": state},
                   follow_redirects=False)
    assert r.headers["location"] == ziel + "?error=cancelled"


def test_titelbilder_0_4_3(client, world):
    w = world
    r = client.patch(f"{API}/campaigns/{w['cid']}", json={"coverPreset": "riverside-mystery"}, headers=w["gm"])
    assert r.status_code == 200 and r.json()["coverPreset"] == "riverside-mystery"
    assert client.patch(f"{API}/campaigns/{w['cid']}", json={"coverPreset": "gibtsnicht"},
                        headers=w["gm"]).status_code == 400


def test_info_mit_pruefsumme(client, dbs):
    import hashlib

    from app import aktualisierung
    from tests.test_aktualisierung import APK, EXE, github, release

    gh = github([release("v1.2.0", "t.apk", APK)], [release("worker-v0.1.0", "TalewardWorker-Setup.exe", EXE)], [],
                {"/v1.2.0/t.apk": APK, "/worker-v0.1.0/TalewardWorker-Setup.exe": EXE})
    aktualisierung.pruefen(dbs, gh)
    info = client.get(f"{API}/info").json()
    assert info["appDownloadSha256"] == hashlib.sha256(APK).hexdigest() and info["appDownloadSizeBytes"] == len(APK)
    assert info["apiVersion"] == "0.4.15"


def _web_zip(dateien: dict) -> bytes:
    import io
    import zipfile

    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        for name, inhalt in dateien.items():
            z.writestr(name, inhalt)
    return puffer.getvalue()


def test_web_app_vom_eigenen_server(client, dbs, world):
    from app import aktualisierung
    from tests.test_aktualisierung import github, release

    assert client.get("/app/").status_code == 404
    zip_ = _web_zip({"dist/index.html": "<!doctype html><title>Taleward</title>", "dist/assets/app-1a2b.js": "x=1",
                     "dist/manifest.webmanifest": "{}"})
    gh = github([release("v1.2.0", "taleward-web-1.2.0.zip", zip_)], [], [], {"/v1.2.0/taleward-web-1.2.0.zip": zip_})
    assert aktualisierung.pruefen(dbs, gh)["web"] == "1.2.0"
    assert client.get("/app", follow_redirects=False).headers["location"] == "/app/"
    r = client.get("/app/")
    assert r.status_code == 200 and "Taleward" in r.text and r.headers["cache-control"] == "no-cache"
    r = client.get("/app/assets/app-1a2b.js")
    assert r.text == "x=1" and "immutable" in r.headers["cache-control"]
    assert client.get("/app/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
    assert client.get("/app/../pyproject.toml").status_code == 404
    assert client.get("/app/%2e%2e/%2e%2e/geheimnis.txt").status_code == 404
    # Einladungsseite verweist jetzt auf die Web-App dieses Servers
    code = client.post(f"{API}/campaigns/{world['cid']}/invites", headers=world["gm"]).json()["code"]
    seite = client.get(f"/einladung/{code}").text
    assert "http://testserver/app/#/verbinden?invite=http%3A%2F%2Ftestserver%2Feinladung%2F" in seite
    assert "Für lange Aufnahmen am Handy die App verwenden." in seite


def test_web_zip_mit_boesem_pfad_wird_abgelehnt(client, dbs):
    from app import aktualisierung
    from app.einstellungen import meta_lesen
    from tests.test_aktualisierung import github, release

    zip_ = _web_zip({"index.html": "ok", "../../boese.txt": "x"})
    gh = github([release("v1.2.0", "taleward-web-1.2.0.zip", zip_)], [], [], {"/v1.2.0/taleward-web-1.2.0.zip": zip_})
    aktualisierung.pruefen(dbs, gh)
    assert "unzulässiger Pfad" in meta_lesen(dbs, "update.fehler")
    assert client.get("/app/").status_code == 404
    assert not (aktualisierung.ablage().parent / "boese.txt").exists()


def test_einladung_im_browser_zentral_oder_gar_nicht(client, dbs, world):
    from app.einstellungen import meta_schreiben

    code = client.post(f"{API}/campaigns/{world['cid']}/invites", headers=world["gm"]).json()["code"]
    assert f"{ZENTRAL}/#/verbinden?invite=" in client.get(f"/einladung/{code}").text
    meta_schreiben(dbs, "web.zentral", "aus")
    dbs.commit()
    assert "Im Browser öffnen" not in client.get(f"/einladung/{code}").text


def test_weitere_herkuenfte(client, dbs, admin):  # noqa: F811
    daten = {"csrf": admin, "server_name": "S", "server_operator": "B", "min_age": "16", "web_zentral_feld": "1",
             "web_zentral": "an"}
    r = client.post("/verwaltung/einstellungen", data={**daten, "web_herkuenfte": "kein-link"})
    assert r.status_code == 400
    r = client.post("/verwaltung/einstellungen", data={**daten, "web_herkuenfte": "https://App.Beispiel.de/app/\n"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert vorab(client, "https://app.beispiel.de").headers["access-control-allow-origin"] == "https://app.beispiel.de"
    assert "https://app.beispiel.de" in client.get("/verwaltung/einstellungen").text


def test_alte_zentrale_in_der_konfiguration_wird_gehoben(dbs, monkeypatch):
    """Installationen mit CENTRAL_WEB_ORIGIN=https://taleward.org landen ohne Zutun auf app.taleward.org."""
    from app import webapp
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "central_web_origin", "https://taleward.org")
    assert webapp.konfigurierte_zentrale() == ZENTRAL and webapp.zentrale_herkunft(dbs) == ZENTRAL
    assert webapp.rueckwege(dbs, "http://testserver") == {"http://testserver/app/#/auth", f"{ZENTRAL}/#/auth",
                                                          webapp.APP_LINK}
    monkeypatch.setattr(get_settings(), "central_web_origin", "https://web.anderer-verein.de")
    assert webapp.zentrale_herkunft(dbs) == "https://web.anderer-verein.de"
    assert f"{ALT}/app/#/auth" not in webapp.rueckwege(dbs, "http://testserver")


def test_return_to_app_link_0_4_13(client, world, dbs, dienst, monkeypatch):  # noqa: F811
    """Android-App: fester Rückweg über den App Link, unabhängig von der zentralen Herkunft, wörtlich verglichen."""
    from app import webapp
    from app.config import get_settings
    from tests.test_anmeldung import challenge_von

    params = {"challenge": challenge_von(VERIFIER), "purpose": "login"}
    ziel = "https://app.taleward.org/auth/app"
    assert webapp.APP_LINK == ziel
    # Abwandlungen sind kein erlaubtes Ziel
    for anders in (ziel + "/", ziel + "?x=1", ziel + "/../boese", "http://app.taleward.org/auth/app",
                   "https://app.taleward.org.boese.example/auth/app", ziel.upper()):
        r = client.get(f"{API}/auth/oidc/google/start", params={**params, "returnTo": anders}, follow_redirects=False)
        assert r.headers["location"] == "taleward://auth?error=return_to_not_allowed", anders
    # gilt auch, wenn die Verwaltung eine andere zentrale Web-App eingestellt hat
    with monkeypatch.context() as mp:
        mp.setattr(get_settings(), "central_web_origin", "https://web.anderer-verein.de")
        assert ziel in webapp.rueckwege(dbs, "http://testserver")
    r = client.get(f"{API}/auth/oidc/google/start", params={**params, "returnTo": ziel}, follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    dienst.nonce = q.get("nonce")
    dienst.claims = {"sub": "g-android", "email": "app@example.org", "email_verified": True, "name": "App"}
    r = client.get("/auth/oidc/google/callback", params={"code": "gut", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(ziel + "?ticket=")
    assert "serverId=" in r.headers["location"]
    # ohne returnTo bleibt es bei taleward://auth (ältere Apps)
    r = client.get(f"{API}/auth/oidc/apple/start", params=params, follow_redirects=False)
    assert r.headers["location"].startswith("taleward://auth?")
