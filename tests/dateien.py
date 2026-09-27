"""Kleine Test-Unterlagen ohne zusätzliche Bibliotheken: PDF mit Textseiten, Word (.docx), Text."""
import io
import zipfile


def pdf(seiten: list[str]) -> bytes:
    """Seiten mit einfachem Text (Helvetica); leerer Text = Seite ohne Textebene (wie eingescannt)."""
    objekte: list[bytes] = []

    def neu(inhalt: bytes) -> int:
        objekte.append(inhalt)
        return len(objekte)

    font = neu(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    seiten_ids = []
    kinder_platz = neu(b"")  # Pages, wird unten gefüllt
    for text in seiten:
        zeilen = [z.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for z in text.split("\n")]
        strom = b"BT /F1 11 Tf 50 780 Td 14 TL " + b" ".join(
            b"(" + z.encode("cp1252") + b") Tj T*" for z in zeilen if z) + b" ET"
        if not text:
            strom = b"0.5 g 50 50 400 400 re f"  # nur Grafik
        inhalt = neu(b"<< /Length %d >>\nstream\n" % len(strom) + strom + b"\nendstream")
        seiten_ids.append(neu(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
                              b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (kinder_platz, inhalt, font)))
    objekte[kinder_platz - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % i for i in seiten_ids)
                                 + b"] /Count %d >>" % len(seiten_ids))
    katalog = neu(b"<< /Type /Catalog /Pages %d 0 R >>" % kinder_platz)
    aus = io.BytesIO()
    aus.write(b"%PDF-1.4\n")
    positionen = []
    for i, o in enumerate(objekte, 1):
        positionen.append(aus.tell())
        aus.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = aus.tell()
    aus.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objekte) + 1))
    for p in positionen:
        aus.write(b"%010d 00000 n \n" % p)
    aus.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objekte) + 1, katalog, xref))
    return aus.getvalue()


def docx(absaetze: list[str], seitenumbruch_nach: int | None = None) -> bytes:
    """Word-Datei mit Absätzen; optional ein Seitenumbruch nach dem n-ten Absatz."""
    def p(text):
        from xml.sax.saxutils import escape
        return f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>"
    koerper = []
    for i, a in enumerate(absaetze):
        koerper.append(p(a))
        if seitenumbruch_nach is not None and i + 1 == seitenumbruch_nach:
            koerper.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(koerper) + "</w:body></w:document>")
    aus = io.BytesIO()
    with zipfile.ZipFile(aus, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return aus.getvalue()
