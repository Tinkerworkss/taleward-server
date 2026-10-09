"""Server 0.4.62: Begrenzungen (Zählen vor dem Prüfen, nicht verdrängbar, IPv6 je /64, Grenze über alle Adressen),
Bestätigungsseite vor dem alten Anmelde-Rückweg, Konto löschen ohne Aufzählung, Probeläufe aufräumen, Adresse lokaler
Anbieter, Obergrenze für „Kapitel neu schreiben“, Filter (kurze Doppel, Tischgespräch als Hinweis, Erzählstimme)."""
import threading
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

from app import artefakte, begrenzung
from tests.test_step2a import API
from tests.test_step2b import _kleine_teile, zusammenfassen  # noqa: F401
from tests.test_anmeldung import dienst  # noqa: F401


# ---------------------------------------------------------------- Begrenzung
def test_laufende_sperre_laesst_sich_nicht_verdraengen(monkeypatch):
    monkeypatch.setattr(begrenzung, "MAX_SCHLUESSEL", 100)
    z = begrenzung.Zaehler()
    for _ in range(10):
        z.zaehlen("login-name:opfer", 900)
    assert z.voll("login-name:opfer", 10, 900)
    for i in range(25_000):  # viele neue Namen
        z.versuch(f"login-name:zufall{i}", 10, 900)
    assert z.voll("login-name:opfer", 10, 900)
    assert len(z._daten) <= 100
    # Nachsehen legt nichts an
    vorher = len(z._daten)
    assert not z.voll("login-name:gibtsnicht", 10, 900)
    assert len(z._daten) == vorher


def test_reservieren_alles_oder_nichts_und_adresse_vor_name():
    z = begrenzung.Zaehler()
    for _ in range(3):
        assert z.reservieren([("a", 3, 60), ("n:x", 10, 60)]) is not None
    # Adresse voll → der Name wird gar nicht erst angelegt
    assert z.reservieren([("a", 3, 60), ("n:neu", 10, 60)]) is None
    assert "n:neu" not in z._daten
    # Freigeben nimmt genau diesen Versuch zurück
    r = z.reservieren([("b", 1, 60)])
    assert z.voll("b", 1, 60)
    z.freigeben(r)
    assert not z.voll("b", 1, 60)


def test_ipv6_zaehlt_je_netz():
    assert begrenzung.adresse_aus("2001:db8::1") == begrenzung.adresse_aus("2001:db8::ffff") == "2001:db8::/64"
    assert begrenzung.adresse_aus("2001:db8:0:1::1") != begrenzung.adresse_aus("2001:db8::1")
    assert begrenzung.adresse_aus("::ffff:192.0.2.7") == "192.0.2.7"
    assert begrenzung.adresse_aus("192.0.2.7") == "192.0.2.7"
    assert begrenzung.adresse_aus(None) == "?"
    from app.routers.campaigns import JOIN_JE_ADRESSE, code_versuch

    for i in range(JOIN_JE_ADRESSE):
        assert code_versuch(begrenzung.adresse_aus(f"2001:db8::{i + 1:x}")) is not None
    assert code_versuch(begrenzung.adresse_aus("2001:db8::abcd")) is None


def test_gleichzeitige_fehlversuche_ueberschreiten_die_grenze_nicht(client, make_user, monkeypatch):
    from app import security
    from app.routers.auth import LOGIN_JE_NAME

    make_user("dora")
    langsam = security.verify_password

    def bremse(*a, **kw):
        import time

        time.sleep(0.05)
        return langsam(*a, **kw)

    monkeypatch.setattr("app.routers.auth.verify_password", bremse)
    codes = []

    def versuch():
        codes.append(client.post(f"{API}/auth/login", json={"username": "dora", "password": "falsch"}).status_code)

    faeden = [threading.Thread(target=versuch) for _ in range(25)]
    for f in faeden:
        f.start()
    for f in faeden:
        f.join()
    assert codes.count(401) <= LOGIN_JE_NAME and codes.count(429) >= 25 - LOGIN_JE_NAME


def test_erfolgreiche_anmeldung_zaehlt_nicht(client, make_user):
    make_user("emil")
    for _ in range(20):
        assert client.post(f"{API}/auth/login", json={"username": "emil", "password": "geheim123"}).status_code == 200


def test_falsche_codes_ueber_alle_adressen_begrenzt(client, world, monkeypatch):
    from app.routers import campaigns

    monkeypatch.setattr(campaigns, "JOIN_SERVERWEIT", 3)
    for i in range(3):
        r = client.post(f"{API}/campaigns/join", json={"code": f"RABE-{i:08d}"}, headers=world["out"])
        assert r.status_code == 404
    begrenzung.ZAEHLER.vergessen("join-adr:")
    begrenzung.ZAEHLER.vergessen("join-konto:")
    r = client.post(f"{API}/campaigns/join", json={"code": "RABE-00000009"}, headers=world["out"])
    assert r.status_code == 429


def test_gueltiger_code_zaehlt_nicht(client, world, make_user, login):
    from app.routers.campaigns import JOIN_JE_KONTO

    code = client.post(f"{API}/campaigns/{world['cid']}/invites", headers=world["gm"]).json()["code"]
    for _ in range(JOIN_JE_KONTO + 2):
        assert client.get(f"{API}/invites/{code}").status_code == 200


# ---------------------------------------------------------------- Anmeldung: Bestätigung vor taleward://
def test_alter_rueckweg_nur_nach_bestaetigung(client, world, dbs, dienst):  # noqa: F811
    from tests.test_anmeldung import VERIFIER, challenge_von

    params = {"challenge": challenge_von(VERIFIER), "purpose": "login"}
    r = client.get(f"{API}/auth/oidc/google/start", params=params, follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    dienst.nonce = q["nonce"]
    dienst.claims = {"sub": "g-alt", "email": "alt@example.org", "email_verified": True, "name": "Alt"}
    r = client.get("/auth/oidc/google/callback", params={"code": "gut", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 200 and "location" not in r.headers
    assert "In Taleward anmelden" in r.text and "Google" in r.text and "selbst in Taleward begonnen" in r.text
    assert 'href="taleward://auth?ticket=' in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    # Fehler ohne Ticket gehen weiter direkt zurück
    r = client.get("/auth/oidc/google/callback", params={"error": "access_denied", "state": q["state"]},
                   follow_redirects=False)
    assert r.headers["location"].startswith("taleward://auth?error=")


# ---------------------------------------------------------------- Konto löschen ohne Aufzählung
def test_konto_loeschen_verraet_keine_konten_ohne_passwort(client, dbs):
    from app.models import User

    dbs.add(User(username="nurdienst", display_name="Nur Dienst"))
    dbs.commit()
    r1 = client.post("/konto-loeschen", data={"username": "nurdienst", "password": "x", "bestaetigt": "1"})
    r2 = client.post("/konto-loeschen", data={"username": "gibtsnicht", "password": "x", "bestaetigt": "1"})
    assert r1.status_code == r2.status_code == 401
    assert "stimmen nicht" in r1.text and "kein Passwort (Anmeldung" not in r1.text
    assert begrenzung.ZAEHLER.voll("login-name:nurdienst", 1, 900)


# ---------------------------------------------------------------- Probeläufe aufräumen
def _probe(campaign_id: str, besitzer: str) -> str:
    from app import kapitelprobe
    from app.kapitelprobe import Probe

    p = Probe(id=str(uuid.uuid4()), session_id="s", kampagne="K", kapitel=1, quelle="Runde", zeilen=1, modell="m",
              gestartet="2026-10-08T00:00:00Z", zustand="fertig", campaign_id=campaign_id, besitzer=besitzer)
    kapitelprobe._speichern(p)
    return p.id


def test_probelaeufe_verschwinden_mit_der_sl_rolle(client, world, dbs, make_user, login):
    from app import kapitelprobe
    from app.models import User

    w = world
    anna = dbs.query(User).filter_by(username="anna").one()
    ben = dbs.query(User).filter_by(username="ben").one()
    eigen = _probe(w["cid"], anna.id)
    ohne = _probe(w["cid"], "")
    # Ben wird SL, startet einen Probelauf, wird wieder Spieler → sein Probelauf ist weg
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", json={"role": "gm"}, headers=w["gm"])
    bens = _probe(w["cid"], ben.id)
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", json={"role": "player"}, headers=w["gm"])
    assert kapitelprobe.lesen(bens) is None and kapitelprobe.lesen(eigen) is not None
    # Wartung: ohne Besitzer → weg
    assert kapitelprobe.rechte_pruefen(dbs) == 1 and kapitelprobe.lesen(ohne) is None
    # Konto löschen räumt auf
    make_user("dina")
    dina = dbs.query(User).filter_by(username="dina").one()
    weg = _probe(w["cid"], dina.id)
    kapitelprobe.konto_entfernt(dina.id)
    assert kapitelprobe.lesen(weg) is None


def test_sl_verlaesst_kampagne_probelauf_weg(client, world, dbs):
    from app import kapitelprobe
    from app.models import User

    w = world
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", json={"role": "gm"}, headers=w["gm"])
    ben = dbs.query(User).filter_by(username="ben").one()
    p = _probe(w["cid"], ben.id)
    assert client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"]).status_code == 204
    assert kapitelprobe.lesen(p) is None


# ---------------------------------------------------------------- Adresse lokaler Anbieter
@pytest.mark.parametrize("url,ok", [
    ("https://api.example.org/v1", True), ("http://localhost:11434/v1", True), ("http://127.0.0.1:8080/v1", True),
    ("http://[::1]:8080/v1", True), ("http://localhost.angreifer.example/v1", False),
    ("http://localhost@angreifer.example/v1", False), ("https://nutzer:pw@api.example.org/v1", False),
    ("http://api.example.org/v1", False), ("https:///v1", False), ("https://api.example.org/ v1", False), ("", False),
])
def test_adresse_lokaler_anbieter(url, ok):
    from app.verwaltung.router import _api_adresse_ok

    assert _api_adresse_ok(url) is ok


# ---------------------------------------------------------------- Kapitel neu schreiben: Obergrenze
@pytest.mark.usefixtures("_kleine_teile")
def test_kapitel_neu_schreiben_hat_eine_obergrenze(client, world, dbs, tmp_path, monkeypatch):
    from app.routers import pruefung
    from tests.test_pruefung_046 import _zur_pruefung

    monkeypatch.setattr(pruefung, "NEU_SCHREIBEN_JE_TAG", 3)
    w = world
    sid = _zur_pruefung(client, w, dbs, tmp_path)["id"]  # 1. Zusammenfassung
    for _ in range(2):
        assert client.post(f"{API}/sessions/{sid}/resummarize", headers=w["gm"]).status_code == 202
        assert zusammenfassen(dbs)
    r = client.post(f"{API}/sessions/{sid}/resummarize", headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "resummarize_limit"
    r = client.put(f"{API}/sessions/{sid}/speakers", headers=w["gm"], json=[])
    assert r.status_code == 409 and r.json()["code"] == "resummarize_limit"


# ---------------------------------------------------------------- Filter
def test_kurze_doppel_und_tischgespraech():
    text = ("Eine Frau sprach: „Deine Prüfung ist gekommen. Bestrafe den wahren Mörder.“ Dann war nur noch "
            "Dunkelheit.\n\nBestrafe den wahren Mörder. Dann war nur noch Dunkelheit. Doch das Schicksal hatte "
            "andere Pläne. Arlekin bemerkte: „Alles klar. Ja, irgendwie so. Rondra und Pipo.“ Dann ging er.")
    aus, b = artefakte.kapitel(text)
    assert aus.endswith("Doch das Schicksal hatte andere Pläne. Arlekin bemerkte: „Alles klar. Ja, irgendwie so. "
                        "Rondra und Pipo.“ Dann ging er.")
    assert [x["art"] for x in b] == ["wiederholung", "wiederholung", "tischgespraech"]
    assert b[-1]["absatz"] == 1
    # Figurenrede bleibt ohne Hinweis
    assert artefakte.kapitel("Pipo sagte: „Hab Dank, Orasilas. Ihr steht in meiner Schuld.“")[1] == []


def test_erzaehlstimme_und_einfache_anfuehrungszeichen():
    text = " ".join(["Ihr geht durch das Tor.", "Eure Schritte hallen.", "Ihr seht den Galgen.",
                     "Euch wird kalt.", "Der Wind weht.", "Die Menge ruft."])
    aus, b = artefakte.kapitel(text)
    assert aus == text and b[0]["art"] == "erzaehlstimme"
    aus, b = artefakte.kapitel("Sie rief: 'Deine Prüfung ist gekommen.' Orasilas' Blick war fest.")
    assert b == [] and "Deine Prüfung" in aus


def test_hinweis_landet_im_pruefteil():
    from app.sprachmodell import hinweise_eintragen

    pruefung = {"paragraphs": [{"index": 0, "verdict": "supported", "note": None},
                               {"index": 1, "verdict": "partial", "note": "Zeit fehlt."}]}
    hinweise_eintragen(pruefung, [{"art": "tischgespraech", "text": "„Alles klar. Ja.“", "absatz": 1},
                                  {"art": "du_form", "text": "x"}], "de")
    assert pruefung["paragraphs"][0]["note"] is None
    # 0.4.64: ein Satz, und der Absatz ist als „vermutlich außerhalb des Spiels“ markiert
    assert pruefung["paragraphs"][1] == {"index": 1, "verdict": "off_game",
                                         "note": "Klingt nach Gespräch am Tisch: „Alles klar. Ja.“"}


def test_cloud_ohne_nachbesserung(dbs, monkeypatch):
    from app import sprachmodell, zusammenfassung
    from tests.test_step5 import einstellen

    erzeugt = []

    class Ablauf:
        def __init__(self, klient, **kw):
            erzeugt.append(kw.get("nachbesserung", True))

        def ausfuehren(self, *a, **kw):
            raise RuntimeError("genug")

    monkeypatch.setattr(sprachmodell, "Ablauf", Ablauf)
    for name in ("eingabe_bauen", "recap_eingabe"):
        monkeypatch.setattr(zusammenfassung, name, lambda *a: {})
    monkeypatch.setattr(zusammenfassung, "vorschlag_eingabe", lambda *a: {})
    einstellen(dbs, art="api", api_key="sk-abcdefghijklmnop1234", api_modell="mistral-medium-latest")
    with pytest.raises(RuntimeError):
        zusammenfassung.zusammenfasser(dbs)(dbs, type("S", (), {"id": "x"})())
    assert erzeugt == [False]
