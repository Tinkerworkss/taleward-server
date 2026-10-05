"""Strukturierte A/B-Systemwortlisten für Whisper und die Korrekturhilfe."""
from app import systembegriffe


def _liste(tmp_path, name: str, text: str):
    (tmp_path / f"{name}.txt").write_text(text, encoding="utf-8")
    systembegriffe.cache_leeren()


def test_strukturierte_liste_trennt_hotwords_wortbuch_und_verhoerer(tmp_path, monkeypatch):
    monkeypatch.setattr(systembegriffe, "_ORDNER", tmp_path)
    _liste(tmp_path, "dsa", """# Das Schwarze Auge
# Erkennungsnamen: das schwarze auge, dsa, dsa5, aventurien
# Stufe A (höchstens 40, häufigste zuerst)
Aventurien
Praios
# Stufe B
Borbarad
Nostria
Praios
# Verhörer
Praeus => Praios
""")
    liste = systembegriffe.lesen("dsa")
    assert liste is not None
    assert liste.stufe_a == ("Aventurien", "Praios")
    assert liste.stufe_b == ("Borbarad", "Nostria")
    assert systembegriffe.verhoerer_map("dsa") == {"praeus": "Praios"}
    assert systembegriffe.erkennen("other", "DSA5 Aventurien").schluessel == "dsa"
    assert systembegriffe.promovierte_stufe_b("dsa", None, ["Wir reisen nach Nostria."]) == ("Nostria",)


def test_alte_einfache_liste_bleibt_kompatibel(tmp_path, monkeypatch):
    monkeypatch.setattr(systembegriffe, "_ORDNER", tmp_path)
    _liste(tmp_path, "legacy", """# alte Liste
Alpha
Beta
""")
    liste = systembegriffe.lesen("legacy")
    assert liste.stufe_a == ("Alpha", "Beta")
    assert liste.stufe_b == ()
    assert liste.verhoerer == ()


def test_fantasy_warhammer_wird_nicht_aus_40k_abgeleitet(tmp_path, monkeypatch):
    monkeypatch.setattr(systembegriffe, "_ORDNER", tmp_path)
    _liste(tmp_path, "warhammer", """# Erkennungsnamen: warhammer, wfrp, warhammer fantasy
# Stufe A
Sigmar
""")
    assert systembegriffe.erkennen("other", "Warhammer Fantasy 4e").schluessel == "warhammer"
    assert systembegriffe.erkennen("other", "Warhammer 40k") is None
    assert systembegriffe.erkennen("other", "Dark Heresy") is None


def test_eingecheckte_kernlisten_haben_a_b_und_keine_pfeile_in_hotwords():
    erwartet = {
        "dsa": (40, 436, 6),
        "dnd": (40, 427, 8),
        "pathfinder": (40, 336, 4),
        "cthulhu": (40, 321, 7),
        "shadowrun": (40, 306, 8),
        "splittermond": (40, 310, 1),
    }
    for system, (a, b, v) in erwartet.items():
        liste = systembegriffe.lesen(system)
        assert liste is not None
        assert len(liste.stufe_a) == a
        assert len(liste.stufe_b) == b
        assert len(liste.verhoerer) == v
        assert all("=>" not in x for x in liste.stufe_a)
        assert not ({x.casefold() for x in liste.stufe_a} & {x.casefold() for x in liste.stufe_b})


def test_stufe_b_bleibt_aus_dem_normalen_whisper_prompt():
    from app import namenshilfe

    hotwords = namenshilfe.systembegriffe("dsa")
    assert "Aventurien" in hotwords
    assert "Borbarad" not in hotwords
    assert "Fex => Phex" not in hotwords


def test_neue_systeme_koennen_ueber_freien_systemnamen_erkannt_werden():
    assert systembegriffe.erkennen("other", "Vampire V5").schluessel == "vampire"
    assert systembegriffe.erkennen("other", "Cyberpunk RED").schluessel == "cyberpunk"
    assert systembegriffe.erkennen("other", "Der Eine Ring 2e").schluessel == "herrderringe"
    assert systembegriffe.erkennen("other", "Starfinder 1e").schluessel == "starfinder"
    assert systembegriffe.erkennen("other", "Genesys") is None
    assert systembegriffe.erkennen("other", "Imperium") is None


def test_zusaetzliche_listen_sind_formal_sauber():
    erwartet = {
        "warhammer": (40, 309, 5),
        "midgard": (40, 316, 2),
        "vampire": (40, 303, 6),
        "cyberpunk": (40, 310, 4),
        "starwars": (40, 326, 5),
        "degenesis": (40, 251, 1),
        "starfinder": (40, 219, 5),
    }
    for system, (a, b, v) in erwartet.items():
        liste = systembegriffe.lesen(system)
        assert liste is not None
        assert (len(liste.stufe_a), len(liste.stufe_b), len(liste.verhoerer)) == (a, b, v)
        assert not ({x.casefold() for x in liste.stufe_a} & {x.casefold() for x in liste.stufe_b})

    # Die übergebene Sammelübersicht nennt 313 B-Begriffe, die konkret übergebene Datei enthält 311.
    # Nicht künstlich auffüllen: Datensatz gesondert gegen die Quelle prüfen.
    ring = systembegriffe.lesen("herrderringe")
    assert ring is not None and len(ring.stufe_a) == 40 and len(ring.stufe_b) == 311 and len(ring.verhoerer) == 6
