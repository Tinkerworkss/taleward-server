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
