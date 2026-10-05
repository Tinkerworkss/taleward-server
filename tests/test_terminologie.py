"""Nachgelagerte Terminologie-Korrektur: ASR-Inhalt bleibt vollständig, nur belegte Schreibweisen ändern sich."""
from types import SimpleNamespace

from app import namenshilfe, terminologie


def kampagne():
    return SimpleNamespace(system="shadowrun", system_name=None, language="de", namenshilfe=None)


def test_system_verhoerer_werden_exakt_nach_asr_korrigiert():
    c = kampagne()
    text = "Der Tschummer zahlt 50 Nujen an Saeder Krupp. Yen und Aeris bleiben absichtlich stehen."
    neu, zaehler = terminologie.korrigieren(text, c)

    assert neu == "Der Chummer zahlt 50 Nuyen an Saeder-Krupp. Yen und Aeris bleiben absichtlich stehen."
    assert dict(zaehler) == {"system": 3}


def test_fuzzy_faelle_werden_nicht_blind_korrigiert():
    c = kampagne()
    neu, zaehler = terminologie.korrigieren("Seda Group, Aeris, Yen und Lofwir.", c)

    assert neu == "Seda Group, Aeris, Yen und Lofwyr."
    assert dict(zaehler) == {"system": 1}


def test_sl_korrektur_wird_gelernt_und_ganzwortig_angewandt():
    c = kampagne()
    namenshilfe.korrektur_lernen(c, "Tharvok", "Darvok")
    # Andere Änderungen an der Namenshilfe dürfen die Lernregel nicht verlieren.
    namenshilfe.hinzufuegen(None, c, "Darvok")

    neu, zaehler = terminologie.korrigieren("Tharvok kommt. Tharvokian bleibt.", c)
    assert neu == "Darvok kommt. Tharvokian bleibt."
    assert dict(zaehler) == {"learned": 1}
    assert namenshilfe.korrekturen(c) == {"tharvok": "Darvok"}

    namenshilfe.ignorieren(c, "Tharvok")
    assert namenshilfe.korrekturen(c) == {}


def test_kampagnenbegriff_normalisiert_nur_orthografie():
    c = kampagne()
    neu, zaehler = terminologie.korrigieren(
        "Mr Johnson wartet bei Saeder Krupp.", c, kanonische_begriffe=["Mr. Johnson"]
    )
    assert neu == "Mr. Johnson wartet bei Saeder-Krupp."
    assert dict(zaehler) == {"orthography": 1, "system": 1}


def test_echter_kampagnenname_schuetzt_vor_globaler_systemkorrektur():
    c = kampagne()
    neu, zaehler = terminologie.korrigieren(
        "Tschummer kommt herein.", c, kanonische_begriffe=["Tschummer"]
    )
    assert neu == "Tschummer kommt herein."
    assert dict(zaehler) == {}


def test_regeln_lassen_sich_reproduzierbar_einfrieren():
    c = kampagne()
    namenshilfe.korrektur_lernen(c, "Tharvok", "Darvok")
    regeln = terminologie.regeln(c, ["Mr. Johnson"])
    snap = terminologie.snapshot(regeln)
    wieder = terminologie.aus_snapshot(snap)

    assert wieder == regeln
    assert terminologie.fingerprint(wieder) == terminologie.fingerprint(regeln)
    assert terminologie.aus_snapshot([{"heard": "", "correct": "x", "source": "learned"}]) is None
