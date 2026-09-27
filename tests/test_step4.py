"""Schritt 4: Stimmen aus der Vorstellungsrunde vorschlagen."""
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app.zuordnung import FENSTER_SEKUNDEN, Hinweis, verteilen, vorstellungsrunde
from tests.test_step2a import API, audio_abschnitte, hochladen, neue_session, worker_token


def mitglied(id_, name, figur=None, rolle="player", benutzer=None):
    return NS(id=id_, role=rolle, character_name=figur,
              user=NS(display_name=name, username=benutzer or name.lower()))


RUNDE = [mitglied("sl", "Zeitiger", rolle="gm"), mitglied("lilio", "Lilio", "Jemma Reed"),
         mitglied("saroman", "Saroman", "Litha Flamel"), mitglied("phybe", "Phybe", "Tubo")]


def seg(sp, start, text, dauer=4.0):
    return NS(speaker_id=sp, start=start, end=start + dauer, text=text)


def vorschlag(segmente, runde=RUNDE):
    return {k: h.member_id for k, h in verteilen(vorstellungsrunde(segmente, runde)).items()}


def test_typische_vorstellungsrunde():
    segmente = [
        seg("A", 5, "Hallo zusammen, ich leite heute wieder, willkommen zu Household."),
        seg("B", 20, "Ich bin Lilio und spiele Jemma Reed."),
        seg("C", 30, "Hi, ich bin der Saroman, ich spiele Litha Flamel."),
        seg("D", 40, "Ich bin", dauer=1.0),
        seg("D", 41.5, "Phybe, und ich spiel den Tubo."),  # Vorstellung über zwei Zeilen
    ]
    assert vorschlag(segmente) == {"A": "sl", "B": "lilio", "C": "saroman", "D": "phybe"}


def test_whisper_schreibweisen_und_nur_figur():
    # Whisper schreibt „Gemma“ statt „Jemma“ und „Litta“ statt „Litha“
    segmente = [seg("B", 10, "Ich spiele die Gemma."), seg("C", 20, "Und ich bin heute Litta Flamel.")]
    ergebnis = verteilen(vorstellungsrunde(segmente, RUNDE))
    assert {k: h.member_id for k, h in ergebnis.items()} == {"B": "lilio", "C": "saroman"}
    assert all(0.5 <= h.sicherheit < 0.9 for h in ergebnis.values())  # unsicherer als ein exakter Treffer


def test_keine_falschen_treffer():
    segmente = [
        seg("A", 60, "Ich bin Graf Borbarad und ihr werdet mir dienen!"),  # SL spricht einen NSC
        seg("B", 70, "Ich bin müde, lasst uns anfangen."),
        seg("C", 80, "Ich spiele heute mal vorsichtig."),
        seg("D", FENSTER_SEKUNDEN + 10, "Ich bin Phybe."),  # zu spät, keine Vorstellungsrunde mehr
    ]
    assert vorschlag(segmente) == {}


def test_nur_anwesende_mit_konto():
    nur_zwei = RUNDE[:2]
    assert vorschlag([seg("C", 10, "Ich bin Saroman.")], nur_zwei) == {}


def test_widerspruch_jede_person_nur_einmal():
    segmente = [seg("B", 10, "Ich bin Lilio und spiele Jemma Reed."), seg("X", 200, "Ich bin Lilio!")]
    ergebnis = vorschlag(segmente)
    assert ergebnis == {"B": "lilio"}  # die stärkere Stimme bekommt den Vorschlag, die andere keinen


def test_englisch():
    runde = [mitglied("anna", "Anna", "Mira"), mitglied("gm", "Ben", rolle="gm")]
    segmente = [seg("A", 5, "Hi everyone, I'm your GM tonight."), seg("B", 12, "Hey, I'm Anna and I play Mira.")]
    assert vorschlag(segmente, runde) == {"A": "gm", "B": "anna"}


def test_stimmprofil_hinweise_docken_an():
    """Schritt 6 liefert Hinweise mit Quelle voice_match – sie werden mit der Vorstellung verrechnet."""
    hinweise = vorstellungsrunde([seg("B", 10, "Ich spiele die Gemma.")], RUNDE) + [
        Hinweis("B", "lilio", 0.7, "voice_match"), Hinweis("C", "saroman", 0.8, "voice_match")]
    ergebnis = verteilen(hinweise)
    assert ergebnis["B"].member_id == "lilio" and ergebnis["B"].quelle == "voice_match"
    assert ergebnis["B"].sicherheit > 0.9  # zwei Quellen zusammen sind sicherer
    assert ergebnis["C"].member_id == "saroman"


# ---------------------------------------------------------------- über den Server
class IntroMotor:
    """Wie WhisperX, liefert aber eine Vorstellungsrunde."""

    modell = "test"

    def audio_laden(self, wav):
        from app import audio
        return audio.dauer(wav)

    def transkribieren(self, dauer, sprache, hotwords, fortschritt):
        return [{"start": 1.0, "end": 5.0, "text": " Ich bin Anna, ich leite heute."},
                {"start": 6.0, "end": 10.0, "text": " Hallo, ich spiele Mira."},
                {"start": 12.0, "end": 20.0, "text": " Dann legen wir los mit dem Abenteuer."},
                {"start": 21.0, "end": 29.0, "text": " Wir betreten die Taverne."}]

    def ausrichten(self, segmente, dauer, sprache, fortschritt):
        return segmente

    def sprecher_trennen(self, dauer, segmente, min_n, max_n, fortschritt):
        for s, sp in zip(segmente, ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_01"]):
            s["speaker"] = sp
        return segmente, {"SPEAKER_00": [0.1], "SPEAKER_01": [0.2]}


@pytest.fixture(autouse=True)
def _kleine_teile(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(64 * 1024))


def test_vorschlaege_in_der_app(client, world, dbs, tmp_path):
    from app.worker_prozess import WorkerProzess
    from app.transkription import verarbeiter

    w = world  # anna = SL, ben = Spieler mit Figur „Mira“
    s = neue_session(client, w)
    hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (30,)))
    knecht = WorkerProzess("http://testserver", worker_token(dbs), tmp_path / "k", verarbeiter(IntroMotor()),
                          client=TestClient(client.app), claim_wait=0)
    assert knecht.einen_auftrag()
    sprecher = {sp["sampleText"][:10]: sp for sp in
                client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()}
    assert len(sprecher) == 2
    for sp in sprecher.values():
        assert sp["source"] == "intro_round" and sp["confidence"] >= 0.5
    assert {sp["suggestedMemberId"] for sp in sprecher.values()} == {w["gm_member"], w["pl_member"]}
    # Bestätigen ohne Änderung übernimmt die Vorschläge
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    zeilen = client.get(f"{API}/sessions/{s['id']}/transcript", headers=w["gm"]).json()
    assert zeilen[0]["memberId"] == w["gm_member"] and zeilen[1]["memberId"] == w["pl_member"]
