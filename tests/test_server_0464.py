"""Server 0.4.64: Schnittstelle 0.4.14 (Hinweis member_joined) und ruhigerer Prüfteil (ein Satz je Begründung,
Ausschmückung ist kein Befund)."""
import re
from pathlib import Path

from tests.test_step2a import API


def _hinweise(client, w):
    return [(n["code"], n["memberId"]) for n in
            client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()["gmNotices"]]


def _code(client, w):
    return client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]


# ---------------------------------------------------------------- 0.4.14 member_joined
def test_beitritt_meldet_sich_bei_der_sl(client, world):
    w = world
    assert _hinweise(client, w) == [("member_joined", w["pl_member"])]
    n = client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()["gmNotices"][0]
    assert n["entryIds"] == []
    assert "gmNotices" not in client.get(f"{API}/campaigns/{w['cid']}", headers=w["pl"]).json()
    # erledigen
    assert client.delete(f"{API}/campaigns/{w['cid']}/gm-notices/{n['id']}", headers=w["gm"]).status_code == 204
    assert _hinweise(client, w) == []
    # noch einmal beitreten, solange man Mitglied ist: kein neuer Hinweis
    client.post(f"{API}/campaigns/join", headers=w["pl"], json={"code": _code(client, w)})
    assert _hinweise(client, w) == []


def test_austritt_und_wiedereintritt(client, world):
    w = world
    assert client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"]).status_code == 204
    assert ("member_joined", w["pl_member"]) not in _hinweise(client, w)
    client.post(f"{API}/campaigns/join", headers=w["pl"], json={"code": _code(client, w)})
    assert _hinweise(client, w).count(("member_joined", w["pl_member"])) == 1
    # SL entfernt → Hinweis weg
    assert client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["gm"]).status_code == 204
    assert ("member_joined", w["pl_member"]) not in _hinweise(client, w)


def test_kontoloeschung_raeumt_auf(client, world, dbs):
    from app import konto
    from app.models import User

    w = world
    konto.entfernen(dbs, dbs.query(User).filter_by(username="ben").one())
    dbs.commit()
    assert ("member_joined", w["pl_member"]) not in _hinweise(client, w)


def test_schnittstelle_0414(client):
    from app.routers.auth import API_VERSION
    from app.verwaltung.router import MELDUNGEN

    assert API_VERSION == "0.4.15"
    vertrag = Path(__file__).resolve().parents[1] / "contract" / "session-chronik-api.yaml"
    assert "version: 0.4.15" in vertrag.read_text(encoding="utf-8")
    assert "„Kampagne verwalten“ → „Cloud-Dienste“" in MELDUNGEN["probe_cloud"]


# ---------------------------------------------------------------- Prüfteil
def test_begruendung_ein_satz():
    from app.pruefteil import NOTIZ_HOECHSTENS, kurz

    assert kurz("Absatz 3: Der Regen kommt nicht vor. Außerdem fehlt die Brücke.") == "Der Regen kommt nicht vor."
    assert kurz("Klingt nach Gespräch am Tisch: „Mach mal. Alles klar.“") == "Klingt nach Gespräch am Tisch: „Mach mal. Alles klar.“"
    assert kurz("  ") is None and kurz(None) is None
    lang = kurz("wort " * 100)
    assert len(lang) <= NOTIZ_HOECHSTENS and lang.endswith(" …")


def test_pruefteil_ruhiger():
    from app import pruefteil
    from app.pruefteil import Abschnitt, Stellen

    stellen = Stellen([Abschnitt(10.0, 20.0, "Mara gibt das Schwert ab.", None),
                       Abschnitt(30.0, 40.0, "Dann ziehen alle weiter.", None)])
    text = "Mara gab das Schwert ab.\n\nEs regnete.\n\nDann zogen alle weiter.\n\nEin Drache kam."
    roh = {"model": "m", "paragraphs": [
        {"index": 0, "verdict": "supported", "note": "Passt. Wirklich.", "evidence": [{"start": 12, "quote": "Schwert ab"}]},
        {"index": 1, "verdict": "unsupported", "note": "Absatz 2: Regen kommt nicht vor. Mehr dazu …", "evidence": []},
        {"index": 2, "verdict": "unsupported", "note": "Nicht belegt.", "evidence": [{"start": 31, "quote": "ziehen alle weiter"}]},
        {"index": 3, "verdict": "contradicted", "note": "Ein Drache kommt nicht vor.", "evidence": []}]}
    p = pruefteil.bauen(roh, text, stellen)["paragraphs"]
    assert p[0]["verdict"] == "supported" and p[0]["note"] is None
    assert p[1] == {"index": 1, "verdict": "unsupported", "note": "Regen kommt nicht vor.", "evidence": []}
    assert p[2]["verdict"] == "partial"  # das Modell nennt selbst eine Stelle, die es gibt
    assert p[3]["verdict"] == "contradicted"


def test_pruefauftrag_ohne_strenge_bei_ausschmueckung():
    from app.sprachmodell import SYSTEM_PRUEFUNG

    assert "Streng sein" not in SYSTEM_PRUEFUNG
    assert "Ausschmückung" in SYSTEM_PRUEFUNG and "nie \\\n" not in SYSTEM_PRUEFUNG
    assert re.search(r"nur bei \"unbelegt\", \"widerspricht\" und \"witz\"", SYSTEM_PRUEFUNG)
    assert "Pipo" not in SYSTEM_PRUEFUNG and "Nostria" not in SYSTEM_PRUEFUNG


def test_tischgespraech_kurz_und_markiert():
    from app.sprachmodell import hinweise_eintragen

    lang = "„" + "Ja klar, mach mal " * 20 + "“"
    pruefung = {"paragraphs": [{"index": 0, "verdict": "contradicted", "note": "Mara stirbt nicht."},
                               {"index": 1, "verdict": "supported", "note": None}]}
    hinweise_eintragen(pruefung, [{"art": "tischgespraech", "text": "„Ok.“", "absatz": 0},
                                  {"art": "tischgespraech", "text": lang, "absatz": 1}], "en")
    assert pruefung["paragraphs"][0] == {"index": 0, "verdict": "contradicted", "note": "Mara stirbt nicht."}
    b = pruefung["paragraphs"][1]
    assert b["verdict"] == "off_game" and b["note"].startswith("Sounds like table talk: „Ja klar") and len(b["note"]) < 160
