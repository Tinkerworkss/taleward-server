"""Schnittstelle 0.3.10 und 0.4.0: Cloud-Erlaubnis je Kampagne, E-Mail, Passwort vergessen/ändern, Anmeldedienste."""
import base64
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.anmeldedienste import challenge_von

API = "/api/v1"
VERIFIER = "v" * 50


@pytest.fixture()
def post(monkeypatch):
    """Mailserver eingerichtet, Mails werden abgefangen."""
    from app import mail
    from app.einstellungen import meta_schreiben

    gesendet = Postfach()
    monkeypatch.setattr(mail, "senden", lambda db, an, betreff, text: gesendet.append((an, betreff, text)))

    def einrichten(dbs):
        meta_schreiben(dbs, "melden.smtp_host", "smtp.example.org")
        meta_schreiben(dbs, "angabe.public_url", "https://chronik.example.org")  # Links in Mails nur damit
        dbs.commit()

    gesendet.einrichten = einrichten
    return gesendet


class Postfach(list):
    einrichten = None


def link_aus(text: str, pfad: str) -> str:
    return re.search(rf"https?://[^\s]+{pfad}/[^\s]+", text).group(0).split("://", 1)[1].split("/", 1)[1]


# ---------------------------------------------------------------- 0.3.10
def test_cloud_zusammenfassung_nur_mit_erlaubnis(client, world, dbs):
    from app.einstellungen import meta_schreiben

    w = world
    c = client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()
    assert c["allowCloudSummary"] is False
    info = client.get(f"{API}/info").json()
    assert info["cloudSummary"] is None and info["externalTranscriptionMode"] is None
    meta_schreiben(dbs, "llm.art", "api")
    meta_schreiben(dbs, "llm.api_key", "sk-test-1234567890")
    dbs.commit()
    assert client.get(f"{API}/info").json()["cloudSummary"] == "mistral"
    assert client.patch(f"{API}/campaigns/{w['cid']}", json={"allowCloudSummary": True},
                        headers=w["pl"]).status_code == 403
    r = client.patch(f"{API}/campaigns/{w['cid']}", json={"allowCloudSummary": True}, headers=w["gm"])
    assert r.json()["allowCloudSummary"] is True
    # „Nur Cloud“: neue Kampagnen erlauben es von selbst, externe Transkription ist Hauptweg
    meta_schreiben(dbs, "betrieb.art", "cloud")
    meta_schreiben(dbs, "extern.anbieter", "mistral")
    meta_schreiben(dbs, "extern.api_key", "sk-extern-1234567890")
    dbs.commit()
    neu = client.post(f"{API}/campaigns", json={"title": "Sturmküste"}, headers=w["gm"]).json()
    assert neu["allowCloudSummary"] is True and neu["allowExternalTranscription"] is True
    assert client.get(f"{API}/info").json()["externalTranscriptionMode"] == "primary"


def test_zusammenfassung_wartet_ohne_erlaubnis(_kleine_teile, client, world, dbs, tmp_path, api):  # noqa: F811
    from app import zusammenfassung
    from app.einstellungen import meta_schreiben
    from tests.test_step2b import transkribiert

    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    meta_schreiben(dbs, "llm.art", "api")
    meta_schreiben(dbs, "llm.api_key", "sk-test-1234567890")
    dbs.commit()
    assert zusammenfassung.einen_auftrag(dbs) is False  # nicht erlaubt → bleibt liegen
    st = client.get(f"{API}/sessions/{s['id']}/status", headers=w["gm"]).json()
    assert st["state"] == "summarizing" and "nicht erlaubt" in st["message"] and "Mistral" in st["message"]
    client.patch(f"{API}/campaigns/{w['cid']}", json={"allowCloudSummary": True}, headers=w["gm"])
    assert zusammenfassung.einen_auftrag(dbs) is True


def test_geloeschtes_konto_hat_deleted_at(client, world):
    w = world
    assert client.request("DELETE", f"{API}/me", json={"password": "geheim123"}, headers=w["pl"]).status_code == 204
    c = client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()
    ben = next(m for m in c["members"] if m["id"] == w["pl_member"])
    assert ben["deletedAt"] and ben["userId"] == "" and ben["displayName"] == "Gelöschtes Konto"


# ---------------------------------------------------------------- E-Mail
def test_email_bestaetigen(client, world, dbs, post):
    w = world
    assert client.get(f"{API}/info").json()["passwordReset"] is False
    assert client.put(f"{API}/me/email", json={"email": "ben@example.org"}, headers=w["pl"]).status_code == 503
    post.einrichten(dbs)
    assert client.get(f"{API}/info").json()["passwordReset"] is True
    assert client.put(f"{API}/me/email", json={"email": "kein-at"}, headers=w["pl"]).status_code == 400
    assert client.put(f"{API}/me/email", json={"email": " Ben@Example.org "}, headers=w["pl"]).status_code == 202
    me = client.get(f"{API}/me", headers=w["pl"]).json()
    assert me["email"] is None and me["emailPending"] == "ben@example.org" and me["hasPassword"] is True
    an, betreff, text = post[-1]
    assert an == "ben@example.org" and "bestätige" in betreff
    pfad = "/" + link_aus(text, "/konto/email")
    seite = client.get(pfad)
    assert seite.status_code == 200 and "ben@example.org" in seite.text and 'method="post"' in seite.text
    assert client.get(f"{API}/me", headers=w["pl"]).json()["email"] is None  # Ansehen bestätigt nicht
    assert "E-Mail bestätigt" in client.post(pfad).text
    me = client.get(f"{API}/me", headers=w["pl"]).json()
    assert me["email"] == "ben@example.org" and me["emailPending"] is None
    assert client.post(pfad).status_code == 404  # nur einmal
    # Andere Konten dürfen die Adresse nicht nehmen, andere Mitglieder sehen sie nie
    r = client.put(f"{API}/me/email", json={"email": "ben@example.org"}, headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "email_taken"
    c = client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()
    assert "ben@example.org" not in json.dumps(c)
    export = client.get(f"{API}/me/export", headers=w["pl"]).json()
    assert export["account"]["email"] == "ben@example.org"
    assert client.delete(f"{API}/me/email", headers=w["pl"]).status_code == 204
    assert client.get(f"{API}/me", headers=w["pl"]).json()["email"] is None


def test_passwort_vergessen(client, world, dbs, post, login):
    from app.models import User

    w = world
    post.einrichten(dbs)
    ben = dbs.query(User).filter_by(username="ben").one()
    ben.email = "ben@example.org"
    dbs.commit()
    for login_ in ("gibtsnicht", "cleo", "nie@example.org"):  # immer 202, keine Mail
        assert client.post(f"{API}/auth/password-reset", json={"login": login_}).status_code == 202
    assert post == []
    assert client.post(f"{API}/auth/password-reset", json={"login": "BEN@example.org"}).status_code == 202
    an, betreff, text = post[-1]
    assert an == "ben@example.org" and "/passwort/" in text and "eine Stunde" in text
    pfad = "/" + link_aus(text, "/passwort")
    r = client.post(pfad, data={"password": "Laternenmann9", "password2": "Laternenmann9"})
    assert r.status_code == 200 and "Passwort gespeichert" in r.text
    assert client.get(f"{API}/me", headers=w["pl"]).status_code == 401
    assert login("ben", "Laternenmann9")
    # Begrenzung je Konto: höchstens 3 Mails pro Stunde, Antwort bleibt 202
    for _ in range(5):
        assert client.post(f"{API}/auth/password-reset", json={"login": "ben"}).status_code == 202
    assert len(post) == 3  # die erste Mail zählt mit


def test_passwort_aendern_dieses_geraet_bleibt(client, world, login):
    w = world
    zweites = login("ben")
    r = client.put(f"{API}/me/password", json={"currentPassword": "falsch", "newPassword": "Laternenmann9"},
                   headers=w["pl"])
    assert r.status_code == 401 and r.json()["code"] == "wrong_password"
    r = client.put(f"{API}/me/password", json={"currentPassword": "geheim123", "newPassword": "kurz"}, headers=w["pl"])
    assert r.status_code == 400
    r = client.put(f"{API}/me/password", json={"currentPassword": "geheim123", "newPassword": "Laternenmann9"},
                   headers=w["pl"])
    assert r.status_code == 204
    assert client.get(f"{API}/me", headers=w["pl"]).status_code == 200  # dieses Gerät
    assert client.get(f"{API}/me", headers=zweites).status_code == 401  # andere abgemeldet
    assert login("ben", "Laternenmann9")


# ---------------------------------------------------------------- Anmeldedienste
class Dienst:
    """Nachgebaute Anbieter: Google/Microsoft/Apple mit signiertem id_token, Discord mit Profil-API."""

    def __init__(self):
        self.schluessel = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = self.schluessel.public_key().public_numbers()

        def b64(n: int) -> str:
            return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

        self.jwks = {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig", "n": b64(pub.n),
                               "e": b64(pub.e)}]}
        self.claims = {}
        self.discord = {}
        self.nonce = None
        self.token_anfragen = []

    def id_token(self, iss: str, aud: str) -> str:
        jetzt = int(time.time())
        daten = {"iss": iss, "aud": aud, "iat": jetzt, "exp": jetzt + 300, "nonce": self.nonce, **self.claims}
        return jwt.encode(daten, self.schluessel, algorithm="RS256", headers={"kid": "k1"})

    def transport(self, request: httpx.Request) -> httpx.Response:
        host, pfad = request.url.host, request.url.path
        if pfad.endswith(("/certs", "/keys")):
            return httpx.Response(200, json=self.jwks)
        if pfad in ("/token", "/common/oauth2/v2.0/token", "/auth/token", "/api/oauth2/token"):
            form = parse_qs(request.content.decode())
            self.token_anfragen.append(form)
            if form.get("code") != ["gut"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            aud = form["client_id"][0]
            iss = {"oauth2.googleapis.com": "https://accounts.google.com",
                   "appleid.apple.com": "https://appleid.apple.com"}.get(
                host, f"https://login.microsoftonline.com/{self.claims.get('tid', '')}/v2.0")
            return httpx.Response(200, json={"access_token": "at", "id_token": self.id_token(iss, aud)})
        if pfad == "/api/users/@me":
            return httpx.Response(200, json=self.discord)
        return httpx.Response(404)


@pytest.fixture()
def dienst(dbs, monkeypatch):
    from app import anmeldedienste
    from app.einstellungen import meta_schreiben

    d = Dienst()
    echt = httpx.Client
    monkeypatch.setattr(anmeldedienste.httpx, "Client", lambda **kw: echt(transport=httpx.MockTransport(d.transport)))
    for name in ("google", "discord", "microsoft"):
        meta_schreiben(dbs, f"oidc.{name}.client_id", f"{name}-client")
        meta_schreiben(dbs, f"oidc.{name}.client_secret", f"{name}-secret")
    dbs.commit()
    return d


def anmelden_mit(client, dienst: Dienst, name: str, purpose="login", link_token=None, code="gut",
                 verifier=VERIFIER) -> dict:
    params = {"challenge": challenge_von(verifier), "purpose": purpose}
    if link_token:
        params["linkToken"] = link_token
    r = client.get(f"{API}/auth/oidc/{name}/start", params=params, follow_redirects=False)
    assert r.status_code == 302, r.text
    ziel = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(ziel.query).items()}
    assert q["redirect_uri"] == f"http://testserver/auth/oidc/{name}/callback"
    dienst.nonce = q.get("nonce")
    r = client.get(f"/auth/oidc/{name}/callback", params={"code": code, "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 303
    zurueck = urlsplit(r.headers["location"])
    assert zurueck.scheme == "taleward" and zurueck.netloc == "auth"
    return {k: v[0] for k, v in parse_qs(zurueck.query).items()}


def test_anmelden_mit_google_neues_konto(client, world, dbs, dienst):
    from app.einstellungen import meta_schreiben

    w = world
    info = client.get(f"{API}/info").json()
    assert [p["id"] for p in info["authProviders"]] == ["google", "discord", "microsoft"]
    assert "oidc" in info["authMethods"]
    dienst.claims = {"sub": "g-123", "email": "Lena@Gmail.com", "email_verified": True, "name": "Lena Laterne"}
    zurueck = anmelden_mit(client, dienst, "google")
    assert "ticket" in zurueck and zurueck["serverId"]
    # Falscher verifier (andere App) → abgelehnt
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"], "verifier": "x" * 50})
    assert r.status_code == 400 and r.json()["code"] == "ticket_invalid"
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"], "verifier": VERIFIER}).json()
    assert r["status"] == "register" and r["email"] == "lena@gmail.com" and r["suggestedDisplayName"] == "Lena Laterne"
    assert client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"],
                                                           "verifier": VERIFIER}).status_code == 400  # einmalig
    code = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    meta_schreiben(dbs, "registrierung", "invite_only")
    dbs.commit()
    r = client.post(f"{API}/auth/register", json={"inviteCode": code, "registrationToken": r["registrationToken"],
                                                  "displayName": "Lena", "acceptPrivacy": True, "ageConfirmed": True})
    assert r.status_code == 201, r.text
    u = r.json()["user"]
    assert u["username"] == "lena" and u["hasPassword"] is False and u["providers"] == ["google"]
    assert u["email"] == "lena@gmail.com"
    # Nächstes Mal: direkt angemeldet
    zurueck = anmelden_mit(client, dienst, "google")
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"], "verifier": VERIFIER}).json()
    assert r["status"] == "ok" and r["accessToken"] and r["provider"] == "google"
    h = {"Authorization": f"Bearer {r['accessToken']}"}
    # Einziger Weg → nicht trennbar; Konto löschen mit Benutzername
    r2 = client.delete(f"{API}/me/providers/google", headers=h)
    assert r2.status_code == 409 and r2.json()["code"] == "last_login_method"
    assert client.request("DELETE", f"{API}/me", json={"confirmUsername": "falsch"}, headers=h).status_code == 400
    assert client.request("DELETE", f"{API}/me", json={"confirmUsername": "lena"}, headers=h).status_code == 204


def test_email_gehoert_schon_jemandem(client, world, dbs, dienst):
    from app.models import User

    ben = dbs.query(User).filter_by(username="ben").one()
    ben.email = "ben@example.org"
    dbs.commit()
    dienst.claims = {"sub": "g-999", "email": "ben@example.org", "email_verified": True}
    zurueck = anmelden_mit(client, dienst, "google")
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"], "verifier": VERIFIER}).json()
    assert r["status"] == "email_in_use" and r["accessToken"] is None and r["registrationToken"] is None
    # Unbestätigte Adresse zählt gar nicht
    dienst.claims = {"sub": "g-998", "email": "ben@example.org", "email_verified": False}
    zurueck = anmelden_mit(client, dienst, "google")
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"], "verifier": VERIFIER}).json()
    assert r["status"] == "register" and r["email"] is None


def test_discord_verbinden_und_trennen(client, world, dbs, dienst, login):
    w = world
    dienst.discord = {"id": "777", "username": "benni", "global_name": "Ben", "email": "b@example.org", "verified": True}
    lt = client.post(f"{API}/me/providers/discord/link", headers=w["pl"]).json()["linkToken"]
    zurueck = anmelden_mit(client, dienst, "discord", purpose="link", link_token=lt)
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"], "verifier": VERIFIER}).json()
    assert r["status"] == "ok" and r["accessToken"] is None and r["user"]["providers"] == ["discord"]
    assert r["user"]["email"] == "b@example.org"  # bestätigte Adresse übernommen
    # Link-Token nur einmal; dasselbe Discord-Konto nicht an ein zweites Taleward-Konto
    r = client.get(f"{API}/auth/oidc/discord/start", params={"challenge": challenge_von(VERIFIER), "purpose": "link",
                                                             "linkToken": lt}, follow_redirects=False)
    assert "error=link_invalid" in r.headers["location"]
    lt2 = client.post(f"{API}/me/providers/discord/link", headers=w["gm"]).json()["linkToken"]
    zurueck = anmelden_mit(client, dienst, "discord", purpose="link", link_token=lt2)
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"], "verifier": VERIFIER})
    assert r.status_code == 409 and r.json()["code"] == "provider_linked_elsewhere"
    # Mit Discord anmelden → Bens Konto
    zurueck = anmelden_mit(client, dienst, "discord")
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"], "verifier": VERIFIER}).json()
    assert r["status"] == "ok" and r["user"]["username"] == "ben"
    assert client.delete(f"{API}/me/providers/discord", headers=w["pl"]).status_code == 204  # Passwort bleibt
    assert client.get(f"{API}/me", headers=w["pl"]).json()["providers"] == []


def test_microsoft_firmenkonto_ohne_email_und_abbruch(client, world, dienst):
    dienst.claims = {"sub": "m-1", "tid": "11111111-2222-3333-4444-555555555555", "email": "chef@firma.example"}
    zurueck = anmelden_mit(client, dienst, "microsoft")
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": zurueck["ticket"], "verifier": VERIFIER}).json()
    assert r["status"] == "register" and r["email"] is None  # bei Firmenkonten nicht vertrauenswürdig
    # Abbruch beim Dienst, falscher Code, unbekannter Zustand
    r = client.get(f"{API}/auth/oidc/google/start", params={"challenge": challenge_von(VERIFIER), "purpose": "login"},
                   follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = client.get("/auth/oidc/google/callback", params={"error": "access_denied", "state": state},
                   follow_redirects=False)
    assert r.headers["location"] == "taleward://auth?error=cancelled"
    assert anmelden_mit(client, dienst, "google", code="schlecht") == {"error": "oidc_failed"}
    r = client.get("/auth/oidc/google/callback", params={"code": "gut", "state": "erfunden"}, follow_redirects=False)
    assert r.headers["location"] == "taleward://auth?error=oidc_failed"
    # Nicht eingerichteter Dienst; ungültige challenge
    r = client.get(f"{API}/auth/oidc/apple/start", params={"challenge": challenge_von(VERIFIER), "purpose": "login"},
                   follow_redirects=False)
    assert r.headers["location"] == "taleward://auth?error=provider_unknown"
    r = client.get(f"{API}/auth/oidc/google/start", params={"challenge": "kurz", "purpose": "login"},
                   follow_redirects=False)
    assert "error=oidc_failed" in r.headers["location"]


def test_falsche_signatur_und_nonce(client, world, dienst):
    dienst.claims = {"sub": "g-5", "email_verified": True}
    fremd = Dienst()
    dienst.schluessel = fremd.schluessel  # signiert mit einem Schlüssel, der nicht im JWKS steht
    assert anmelden_mit(client, dienst, "google") == {"error": "oidc_failed"}


def test_apple_formular_und_secret(client, world, dbs, dienst):
    from app.einstellungen import meta_schreiben

    schluessel = ec.generate_private_key(ec.SECP256R1())
    pem = schluessel.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()).decode()
    for k, v in {"client_id": "app.taleward.verein", "team_id": "TEAM123", "key_id": "KEY123",
                 "private_key": pem}.items():
        meta_schreiben(dbs, f"oidc.apple.{k}", v)
    dbs.commit()
    dienst.claims = {"sub": "a-1", "email": "x@privaterelay.appleid.com", "email_verified": "true"}
    r = client.get(f"{API}/auth/oidc/apple/start", params={"challenge": challenge_von(VERIFIER), "purpose": "login"},
                   follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    assert q["response_mode"] == "form_post"
    dienst.nonce = q["nonce"]
    r = client.post("/auth/oidc/apple/callback", data={"code": "gut", "state": q["state"],
                                                       "user": json.dumps({"name": {"firstName": "Lena",
                                                                                    "lastName": "L"}})},
                    follow_redirects=False)
    ticket = parse_qs(urlsplit(r.headers["location"]).query)["ticket"][0]
    secret = dienst.token_anfragen[-1]["client_secret"][0]
    kopf = jwt.get_unverified_header(secret)
    daten = jwt.decode(secret, schluessel.public_key(), algorithms=["ES256"], audience="https://appleid.apple.com")
    assert kopf["kid"] == "KEY123" and daten["iss"] == "TEAM123" and daten["sub"] == "app.taleward.verein"
    r = client.post(f"{API}/auth/oidc/exchange", json={"ticket": ticket, "verifier": VERIFIER}).json()
    assert r["status"] == "register" and r["suggestedDisplayName"] == "Lena L"


def test_start_geht_auch_bei_mindestversion(client, world, dbs, dienst):
    from app.einstellungen import meta_schreiben, mindestversion_vergessen

    meta_schreiben(dbs, "angabe.app_min_version", "9.9.9")
    dbs.commit()
    mindestversion_vergessen()
    r = client.get(f"{API}/auth/oidc/google/start", params={"challenge": challenge_von(VERIFIER), "purpose": "login"},
                   follow_redirects=False)
    assert r.status_code == 302  # Systembrowser schickt keinen App-Header
    assert client.post(f"{API}/auth/oidc/exchange", json={"ticket": "x", "verifier": "y"}).status_code == 426
    meta_schreiben(dbs, "angabe.app_min_version", "")
    dbs.commit()
    mindestversion_vergessen()


def test_verwaltung_anmeldung(client, dbs, admin):  # noqa: F811
    seite = client.get("/verwaltung/anmeldung").text
    assert "/auth/oidc/google/callback" in seite and "ohne HTTPS" in seite
    r = client.post("/verwaltung/anmeldung/google", data={"csrf": admin, "client_id": "abc.apps",
                                                          "client_secret": "GOCSPX-geheim"}, follow_redirects=False)
    assert r.status_code == 303
    seite = client.get("/verwaltung/anmeldung").text
    assert "GOCSPX-geheim" not in seite and "abc.apps" in seite
    assert client.get(f"{API}/info").json()["authProviders"] == [{"id": "google", "name": "Google"}]
    client.post("/verwaltung/anmeldung/google", data={"csrf": admin, "entfernen": "1"})
    assert client.get(f"{API}/info").json()["authProviders"] == []
    r = client.post("/verwaltung/anmeldung/apple", data={"csrf": admin, "client_id": "x", "private_key": "kaputt"})
    assert r.status_code == 400


from tests.test_step2b import _kleine_teile  # noqa: E402,F401 (Fixture)
from tests.test_step5 import api  # noqa: E402,F401 (Fixture)
from tests.test_verwaltung import admin, csrf_von  # noqa: E402,F401 (Fixture)
