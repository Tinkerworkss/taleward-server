"""Server 0.4.66: Ein Name wie „Du“ (häufiger Familienname) ist keine Du-Form. Sätze mit „Herr Du“ oder „Du Hanlin“
dürfen nicht aus Kapitel und Vorschlägen fallen; echte Anrede fällt weiter heraus."""
from app import artefakte


def test_name_bleibt_im_kapitel():
    text = ("Die Gruppe fuhr zur Yacht von Du Hanlin. Du Hanlin holte die Scheibe aus dem Glaskasten.\n\n"
            "Herr Du griff mit einem Messer nach Mara. Mara legte eine Hand auf Du Hanlins Schulter.")
    aus, befunde = artefakte.kapitel(text)
    assert aus.count("Du Hanlin") == 3 and "Herr Du griff" in aus and "Du Hanlins Schulter" in aus
    assert not [b for b in befunde if b["art"] == "du_form"]


def test_anrede_faellt_weiter_heraus():
    for satz in ("Du siehst ein Tor im Schlick.", "Ihr seid umzingelt.", "Dein Schwert glüht.",
                 "Die Wache rief: Du kommst hier nicht rein.", "Euch bleibt nur die Flucht.",
                 "Mara sagt, du sollst warten.", "Du, Mara, bleibst hier."):
        assert artefakte._du_form(satz), satz
    for satz in ("Herr Du lacht.", "Du Hanlin lachte.", "Sie fragte: Du Hanlin?"):
        assert not artefakte._du_form(satz), satz
    text = ("Die Gruppe floh. Du siehst ein Tor im Schlick. Ihr seid umzingelt. Mara sagt, du sollst warten. "
            "Am Ufer wartete das Boot. Mara zählte die Münzen. Der Nebel stieg. Die Lichter gingen aus. "
            "Das Tor schloss sich. Niemand folgte ihnen. Mara schwieg lange. Die Stadt schlief. "
            "Am Morgen fuhr der Zug. Die Scheibe lag im Koffer. Niemand sprach darüber. Die Reise begann. "
            "Hanlin blieb zurück. Das Wasser war ruhig. Die Gruppe schlief. Der Tag brach an. Mara lachte.")
    aus, befunde = artefakte.kapitel(text)
    assert "Tor im Schlick" not in aus and "umzingelt" not in aus and "warten" not in aus
    assert sum(b["art"] == "du_form" for b in befunde) == 3


def test_name_bleibt_im_vorschlag():
    v = artefakte.vorschlag({"title": "Du Hanlin", "detail": "Du Hanlin führt die Bande. Du kennst ihn."})
    assert v["detail"] == "Du Hanlin führt die Bande."


def test_kein_satzende_nach_abkuerzung():
    """0.4.67: „Mr. Du“ wurde an „Mr.“ geteilt; der Rest galt als Anrede und fiel weg, „Mr.“ blieb allein stehen."""
    text = ("Die Empfangsdame kam zurück und sagte, dass Mr. Du sie erwarten würde. Die Gruppe bestellte Getränke. "
            "Dr. Hanlin lachte.")
    aus, befunde = artefakte.kapitel(text)
    assert aus == text and not befunde
    assert [s for s, _ in artefakte._saetze("Er kam z. B. spät. Dann ging er.")] == ["Er kam z. B. spät.", "Dann ging er."]
