"""Server 0.4.55: Absätze im Kapitel, Gewichtung und Zutrauen der Vorschläge, Meldungen der Anbieter."""
import httpx
import pytest

from app import sprachmodell as sm

GRUNDLAGE = "\n".join([
    "[0:01:00] Spielleitung: Der Wirt Bolko stellt euch zwei Krüge hin.",
    "[0:02:00] Mira: Bolko, kennst du den Weg zum Rabenturm?",
    "[0:03:00] Spielleitung: Bolko nickt. Der Rabenturm liegt hinter dem Moor.",
    "[0:04:00] Mira: Dann gehen wir zu Bolkos Bruder.",
    "[0:05:00] Spielleitung: Eine Kutsche fährt vorbei.",
    "[0:06:00] Spielleitung: Bolko sagt: Die Graue Gilde sucht euch.",
    "[0:07:00] Mira: Die Graue Gilde? Was wollen die von uns?",
    "[0:08:00] Spielleitung: Bolko zuckt mit den Schultern.",
])


def _v(titel, typ="npc", detail="Etwas.", zitat="", conf=1.0, aktion="create"):
    return {"entryType": typ, "action": aktion, "title": titel, "detail": detail, "suggestedVisibility": "public",
            "confidence": conf, "evidence": [{"start": "1:00", "quote": zitat}] if zitat else []}


def test_absaetze_teilen_ohne_wortverlust():
    satz = "Die Gruppe ging weiter durch den dunklen Wald und fand dort eine alte Hütte am Fluss. "
    block = satz * 40  # ein Absatz mit 640 Wörtern
    geteilt = sm.absaetze_teilen(block)
    teile = sm.absaetze(geteilt)
    assert len(teile) >= 4 and all(len(t.split()) <= sm.ABSATZ_HOECHSTENS for t in teile)
    assert " ".join(geteilt.split()) == " ".join(block.split())  # kein Wort verändert
    kurz = "Erster Absatz.\n\nZweiter Absatz."
    assert sm.absaetze_teilen(kurz) == kurz


def test_vorschlaege_gewichtet_und_gekuerzt():
    roh = [_v(f"Kulisse {i}", typ="other", zitat="Eine Kutsche fährt vorbei") for i in range(20)]
    roh += [_v("Bolko", zitat="Der Wirt Bolko stellt euch zwei Krüge hin"),
            _v("Graue Gilde", typ="faction", zitat="Die Graue Gilde sucht euch")]
    aus = sm.pruefen(roh, set(), set(), grundlage=GRUNDLAGE)
    assert len(aus) == sm.MAX_VORSCHLAEGE
    titel = [v["title"] for v in aus]
    assert titel[:2] == ["Bolko", "Graue Gilde"]  # häufig genannt, belegt, Figur/Fraktion → vorn


def test_zutrauen_aus_belegen_und_nennungen():
    aus = sm.pruefen([_v("Bolko", zitat="Der Wirt Bolko stellt euch zwei Krüge hin"),
                      _v("Rabenturm", typ="location", zitat="Der Rabenturm liegt hinter dem Moor"),
                      _v("Silberne Feder", typ="item", zitat="Eine silberne Feder lag dort")],
                     set(), set(), grundlage=GRUNDLAGE)
    nach_titel = {v["title"]: v for v in aus}
    assert nach_titel["Bolko"]["confidence"] > nach_titel["Rabenturm"]["confidence"]
    assert nach_titel["Silberne Feder"]["confidence"] < 0.4  # Zitat steht nirgends
    assert "low_confidence" in nach_titel["Silberne Feder"]["flags"]
    assert all(v["confidence"] < 1.0 for v in aus)
    # Das Modell darf das Zutrauen nur senken
    gering = sm.pruefen([_v("Bolko", zitat="Der Wirt Bolko stellt euch zwei Krüge hin", conf=0.2)], set(), set(),
                        grundlage=GRUNDLAGE)
    assert gering[0]["confidence"] == 0.2


def test_vermutungen_fliegen_raus():
    aus = sm.pruefen([_v("Kutsche", typ="item", detail="Eine prunkvolle Kutsche. Sie gehört vermutlich einem Adligen."),
                      _v("Nebel", typ="other", detail="Wahrscheinlich ein Zauber."),
                      _v("Bolko", detail="Bolko scheint den Weg zu kennen. Mira vermutet, dass er lügt.")],
                     set(), set(), grundlage=GRUNDLAGE)
    nach_titel = {v["title"]: v["detail"] for v in aus}
    assert nach_titel["Kutsche"] == "Eine prunkvolle Kutsche."
    assert "Nebel" not in nach_titel  # nur Vermutung → kein Vorschlag
    assert nach_titel["Bolko"] == "Bolko scheint den Weg zu kennen. Mira vermutet, dass er lügt."


def test_zitat_belegt():
    norm = sm._normwoerter(GRUNDLAGE)
    assert sm.zitat_belegt("Der Rabenturm liegt hinter dem Moor.", norm)
    assert sm.zitat_belegt("[0:03:00] Bolko nickt", norm)
    assert not sm.zitat_belegt("Der Rabenturm brennt", norm)
    assert sm.nennungen("Bolko", norm) == 6 and sm.nennungen("Gasthaus Zum Rabenturm", norm) == 0


@pytest.mark.parametrize(("antwort", "erwartet"), [
    (httpx.Response(429, json={"message": "Requests rate limit exceeded"}), "429: Requests rate limit exceeded"),
    (httpx.Response(429, json={"error": {"message": "Service tier capacity exceeded"}}), "Service tier capacity"),
    (httpx.Response(503, text="kaputt"), "503: kaputt"),
])
def test_meldung_des_anbieters(monkeypatch, antwort, erwartet):
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)
    k = sm.OpenAIKlient("https://llm.example/v1", "sk-x", "m",
                        client=httpx.Client(transport=httpx.MockTransport(lambda _r: antwort)))
    with pytest.raises(sm.SprachmodellFehler) as f:
        k.chat("s", "n")
    assert erwartet in str(f.value)


def test_modell_nicht_freigeschaltet():
    def anbieter(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mistral-medium-latest"}, {"id": "mistral-small-latest"}]})
        return httpx.Response(401, json={"message": "Unauthorized"})

    k = sm.OpenAIKlient("https://api.mistral.ai/v1", "sk-x", "mistral-large-latest",
                        client=httpx.Client(transport=httpx.MockTransport(anbieter)))
    with pytest.raises(sm.SprachmodellFehler) as f:
        k.pruefen()
    assert "Schlüssel ist gültig" in str(f.value) and "mistral-large-latest" in str(f.value)
