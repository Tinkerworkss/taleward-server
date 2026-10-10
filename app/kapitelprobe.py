"""Probelauf der Zusammenfassung (Verwaltung, Modul kapitelprobe – app.probelauf ist der Transkriptions-Probelauf): Kapitel und
Vorschläge für eine vorhandene Abschrift erzeugen, ohne die Runde zu verändern – zum Vergleichen von Modellen und Fassungen, auch unterwegs ohne Worker.

Grundlage ist die Abschrift einer Runde auf diesem Server; wahlweise ersetzt eine hochgeladene Textdatei (Zeilen
„[h:mm:ss] Sprecher: Text“, wie `transkript.txt` des Modellvergleichs) die Abschrift, die Kampagne liefert dann nur
Mitglieder und Bibel. Es rechnet, was unter „Zusammenfassung“ eingestellt ist (Cloud-API oder Testmodus); ein lokales
Modell braucht einen Worker und steht hier nicht zur Verfügung. Kosten landen im Verbrauchsprotokoll (Art „probe“).

Ergebnisse liegen als Dateien unter <data_dir>/proben/<id>/ und bleiben bis zum Löschen; der Zustand eines laufenden
Probelaufs steht in stand.json, damit die Seite ihn nach einem Neustart nicht verliert.

Ein Probelauf gehört dem Verwalterkonto, das ihn gestartet hat, und nur für Kampagnen, in denen dieses Konto selbst
Spielleitung ist. Für die Cloud-API gilt dieselbe Freigabe der Kampagne wie im normalen Ablauf. Ist das Konto keine
Spielleitung der Kampagne mehr, verschwinden seine Probeläufe dort; mit der Kampagne werden sie gelöscht.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_factory, utcnow
from app.models import GameSession, UsageLog, new_id

log = logging.getLogger("taleward.kapitelprobe")

_ZEILE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*([^:]{1,80}):\s*(.+)$")
HOECHSTENS_PROBEN = 50  # ältere werden beim Anlegen gelöscht


@dataclass
class Probe:
    id: str
    session_id: str
    kampagne: str
    kapitel: int
    quelle: str  # „Runde“ oder Dateiname
    zeilen: int
    modell: str
    gestartet: str
    zustand: str = "läuft"  # läuft | fertig | fehler
    schritt: str = ""
    fehler: str | None = None
    dauer_s: float = 0.0
    aufrufe: int = 0
    tokens_ein: int = 0
    tokens_aus: int = 0
    kosten_cent: int = 0
    titel: str = ""
    text: str = ""
    offene_faeden: list[str] = field(default_factory=list)
    vorschlaege: list[dict] = field(default_factory=list)
    pruefung: dict = field(default_factory=dict)
    warnungen: list[dict] = field(default_factory=list)
    beendet: str | None = None
    campaign_id: str = ""
    besitzer: str = ""  # Benutzer-ID des Verwalterkontos, das gestartet hat
    bereinigt: list[dict] = field(default_factory=list)  # 0.4.61: was der Artefakt-Filter entfernt hat
    weg: str = "abschrift"  # 0.4.63: „abschrift“ (wie echte Runden) oder „notizen“ (Notizen zuerst, zum Vergleich)
    notizen: str = ""  # 0.4.63: Szenennotizen des Wegs „notizen“ (Datei notizen.txt)
    stand: list[str] = field(default_factory=list)  # 0.4.63: laufender Stand am Ende (Datei stand.txt)
    auswahl: list[dict] = field(default_factory=list)  # 0.4.65: Pflichtpunkte (Datei auswahl.txt)
    pflicht: dict = field(default_factory=dict)  # 0.4.65: Prüfung gegen die Pflichtpunkte (Datei pflicht.json)
    entwurf: str = ""  # 0.4.65: erster Entwurf vor der Nachbesserung (Datei entwurf.txt)
    lang: str = ""  # 0.4.68: Kapitel vor einer angenommenen Kürzung (Datei lang.txt)
    korrekturen: list[dict] = field(default_factory=list)  # 0.4.69: Durchgänge „Korrektur per Hinweis“ (korrektur.json)
    korrektur_laeuft: bool = False
    unklar: list[dict] = field(default_factory=list)  # 0.4.73: Stellen „(unklar, wer)“ aus den Notizen (unklar.json)
    zweiter_blick: list[dict] = field(default_factory=list)  # 0.4.74: Ereignisse gegen die Abschrift (zweiter-blick.json)
    teile: list[dict] = field(default_factory=list)  # 0.4.78: Teile im Kapitel [{title, firstParagraph}]
    einschnitte: dict = field(default_factory=dict)  # 0.4.78: Antwort der Suche nach Einschnitten (einschnitte.json)
    kurzer_entwurf: list[dict] = field(default_factory=list)  # 0.4.78: zu kurze Entwürfe mit Rohantwort (rohantwort.txt)


def text_mit_teilen(text: str, teile: list[dict]) -> str:
    """0.4.78: Kapiteltext mit den Überschriften der Teile (für recap.txt)."""
    if not teile:
        return text
    absaetze = [a.strip() for a in re.split(r"\n\s*\n", text.strip()) if a.strip()]
    vor = {t["firstParagraph"]: t["title"] for t in teile}
    return "\n\n".join((f"– {vor[i]} –\n\n" if i in vor else "") + a for i, a in enumerate(absaetze))


def _json_sicher(d) -> dict:
    """Nur, was sich als JSON schreiben lässt (die Antwort des Modells ist es; zur Sicherheit)."""
    try:
        return json.loads(json.dumps(d, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {}


def aktueller_text(p: Probe) -> str:
    """Kapitel nach dem letzten erfolgreichen Korrekturdurchgang, sonst das ursprüngliche."""
    for k in reversed(p.korrekturen):
        if k.get("nachher"):
            return k["nachher"]
    return p.text


def ordner(probe_id: str | None = None) -> Path:
    p = get_settings().data_dir / "proben"
    return p / probe_id if probe_id else p


def _speichern(p: Probe) -> None:
    from app import storage

    o = ordner(p.id)
    o.mkdir(parents=True, exist_ok=True)
    # atomar: die Seite liest, während der Hintergrund schreibt
    storage.write_atomic(o / "stand.json", json.dumps(p.__dict__, ensure_ascii=False, indent=2).encode("utf-8"))


def lesen(probe_id: str) -> Probe | None:
    if not re.fullmatch(r"[0-9a-f-]{36}", probe_id or ""):
        return None
    try:
        d = json.loads((ordner(probe_id) / "stand.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return Probe(**d)
    except TypeError:
        return None


def ist_sl(db: Session, user_id: str, campaign_id: str) -> bool:
    from sqlalchemy import select

    from app.models import Member

    return db.scalar(select(Member.id).where(Member.campaign_id == campaign_id, Member.user_id == user_id,
                                             Member.role == "gm", Member.left_at.is_(None)).limit(1)) is not None


def fuer(db: Session, probe_id: str, user_id: str) -> Probe | None:
    """Probelauf für dieses Konto – None (also 404), wenn er einem anderen gehört oder das Konto in der Kampagne keine
    Spielleitung mehr ist. Im zweiten Fall wird er gleich gelöscht."""
    p = lesen(probe_id)
    if p is None or not p.besitzer or p.besitzer != user_id:
        return None
    if not p.campaign_id or not ist_sl(db, user_id, p.campaign_id):
        loeschen(p.id)
        return None
    return p


def eigene(db: Session, user_id: str) -> list[Probe]:
    return [p for p in alle() if p.besitzer == user_id and p.campaign_id and ist_sl(db, user_id, p.campaign_id)]


def kampagne_entfernt(campaign_id: str) -> None:
    """Beim Löschen einer Kampagne: ihre Probeläufe mit allen Dateien entfernen."""
    for p in alle():
        if p.campaign_id == campaign_id:
            loeschen(p.id)


def rechte_pruefen(db: Session, user_id: str | None = None) -> int:
    """0.4.62: Probeläufe löschen, deren Konto in der Kampagne keine Spielleitung mehr ist – oder die gar keinem Konto
    gehören (ältere Fassungen). Laufende bleiben bis zum Ende stehen und gehen beim nächsten Mal. user_id: nur dessen."""
    weg = 0
    for p in alle():
        if (user_id and p.besitzer != user_id) or p.zustand == "läuft":
            continue
        if not p.besitzer or not p.campaign_id or not ist_sl(db, p.besitzer, p.campaign_id):
            weg += bool(loeschen(p.id))
    return weg


def konto_entfernt(user_id: str) -> None:
    """Beim Löschen eines Kontos: seine Probeläufe entfernen."""
    for p in alle():
        if p.besitzer == user_id and p.zustand != "läuft":
            loeschen(p.id)


def alle() -> list[Probe]:
    aus = []
    if ordner().is_dir():
        for o in ordner().iterdir():
            p = lesen(o.name)
            if p is not None:
                aus.append(p)
    return sorted(aus, key=lambda p: p.gestartet, reverse=True)


def loeschen(probe_id: str) -> bool:
    import shutil

    if lesen(probe_id) is None:
        return False
    shutil.rmtree(ordner(probe_id), ignore_errors=True)
    return True


def datei(probe_id: str, name: str) -> Path | None:
    """Eine Ergebnisdatei zum Herunterladen – nur bekannte Namen, kein Pfad."""
    if name not in DATEIEN or lesen(probe_id) is None:
        return None
    p = ordner(probe_id) / name
    return p if p.is_file() else None


DATEIEN = ("recap.txt", "vorschlaege.json", "pruefung.json", "notizen.txt", "stand.txt", "auswahl.txt", "pflicht.json",
           "entwurf.txt", "lang.txt", "korrektur.json", "recap-korrigiert.txt", "unklar.json", "zweiter-blick.json",
           "transkript.txt", "ergebnis.json", "einschnitte.json", "rohantwort.txt")


def zip_bytes(probe_id: str) -> bytes | None:
    """Alle vorhandenen Ergebnisdateien in einer ZIP-Datei (Namen wie beim Einzeldownload)."""
    import io
    import zipfile

    if lesen(probe_id) is None:
        return None
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name in DATEIEN:
            pfad = datei(probe_id, name)
            if pfad is not None:
                z.write(pfad, arcname=f"probe-{probe_id[:8]}-{name}")
    return puffer.getvalue()


def abschrift_lesen(text: str) -> list[dict]:
    """Textdatei → Transkriptzeilen {start, sprecher, member_id, text}. Zeilen ohne Zeitmarke hängen an der vorigen."""
    zeilen: list[dict] = []
    for roh in text.splitlines():
        roh = roh.strip()
        if not roh:
            continue
        m = _ZEILE.match(roh)
        if m is None:
            if zeilen:
                zeilen[-1]["text"] += " " + roh
            continue
        h, mi, s, sprecher, inhalt = m.groups()
        start = int(h) * 3600 + int(mi) * 60 + int(s or 0) if s is not None else int(h) * 60 + int(mi)
        zeilen.append({"start": float(start), "sprecher": sprecher.strip(), "member_id": None, "text": inhalt.strip()})
    return zeilen


WEGE = ("abschrift", "notizen")


def starten(db: Session, s: GameSession, abschrift: str | None = None, dateiname: str | None = None,
            user_id: str = "", weg: str = "abschrift") -> Probe:
    """Legt die Probe an und rechnet im Hintergrund. Wirft ValueError mit verständlicher Meldung, wenn nichts zu tun ist."""
    from app.einstellungen import llm_konfig
    from app.models import Campaign
    from app.zusammenfassung import eingabe_bauen, recap_eingabe, vorschlag_eingabe

    if not user_id or not ist_sl(db, user_id, s.campaign_id):
        raise ValueError("runde")
    k = llm_konfig(db)
    if k.art == "lokal":
        raise ValueError("lokal")
    if k.art == "aus" or (k.art == "api" and not k.api_key):
        raise ValueError("aus")
    c = db.get(Campaign, s.campaign_id)
    if k.art == "api" and (c is None or not c.allow_cloud_summary):
        raise ValueError("cloud")
    basis = eingabe_bauen(db, s)
    recap_ein, vorschlag_ein = recap_eingabe(basis), vorschlag_eingabe(db, s, basis)
    quelle = "Runde"
    if abschrift is not None:
        zeilen = abschrift_lesen(abschrift)
        if len(zeilen) < 5:
            raise ValueError("datei")
        recap_ein["transkript"] = zeilen
        vorschlag_ein["transkript"] = zeilen
        quelle = (dateiname or "Abschrift")[:80]
    if not recap_ein["transkript"]:
        raise ValueError("leer")
    p = Probe(id=new_id(), session_id=s.id, kampagne=c.title if c else "?", kapitel=s.number, quelle=quelle,
              zeilen=len(recap_ein["transkript"]), modell=_modellname(k),
              gestartet=utcnow().isoformat().replace("+00:00", "Z"), campaign_id=s.campaign_id, besitzer=user_id,
              weg=weg if weg in WEGE else "abschrift")
    _aufraeumen()
    _speichern(p)
    if abschrift is not None:
        (ordner(p.id) / "transkript.txt").write_text(abschrift, encoding="utf-8")
    threading.Thread(target=_rechnen, args=(p, k, recap_ein, vorschlag_ein, s.campaign_id, s.id),
                     name=f"probelauf-{p.id[:8]}", daemon=True).start()
    return p


def _aufraeumen() -> None:
    import shutil

    for alt in alle()[HOECHSTENS_PROBEN - 1:]:
        shutil.rmtree(ordner(alt.id), ignore_errors=True)


# Probeläufe nacheinander: Jeder schickt die ganze Abschrift (oft 200.000 Token und mehr) an den Anbieter; mehrere
# gleichzeitig überschreiten dessen Grenze je Minute (429). Wer wartet, steht auf „wartet“.
_REIHE = threading.Lock()


def _rechnen(p: Probe, k, recap_ein: dict, vorschlag_ein: dict, campaign_id: str, session_id: str) -> None:
    if not _REIHE.acquire(blocking=False):
        p.schritt = "wartet"
        _speichern(p)
        _REIHE.acquire()
    try:
        p.schritt = ""
        _rechnen_jetzt(p, k, recap_ein, vorschlag_ein, campaign_id, session_id)
    finally:
        _REIHE.release()


def _rechnen_jetzt(p: Probe, k, recap_ein: dict, vorschlag_ein: dict, campaign_id: str, session_id: str) -> None:
    from app.sprachmodell import Ablauf, SprachmodellFehler
    from app.zusammenfassung import (api_klient, api_klient_notizen, api_klient_vorschlaege, attrappe, gegenpruefen_an,
                                     pruefansicht_art, zweiter_blick_an)

    t0 = time.monotonic()
    try:
        if k.art == "attrappe":
            from app.zusammenfassung import eingabe_bauen

            with session_factory()() as db:
                erg = attrappe(eingabe_bauen(db, db.get(GameSession, session_id)))
            p.titel, p.text, p.offene_faeden = erg.titel, erg.text, list(erg.offene_faeden)
            p.vorschlaege = [v.__dict__ for v in erg.vorschlaege]
            p.aufrufe = 0
        else:
            klient = api_klient(k)

            def schritt(name: str) -> None:
                p.schritt = name
                _speichern(p)

            with session_factory()() as db:
                gegen, blick, ansicht = gegenpruefen_an(db), zweiter_blick_an(db), pruefansicht_art(db)
            ablauf = Ablauf(klient, schritt=schritt, vorschlag_klient=api_klient_vorschlaege(k),
                            nachbesserung=k.art != "api",  # 0.4.62: wie im echten Ablauf
                            notizen_zuerst=p.weg == "notizen",  # 0.4.63: hier je Probelauf wählbar
                            notiz_klient=api_klient_notizen(k), zweiter_blick_an=blick, pruefansicht=ansicht)
            d = ablauf.ausfuehren(recap_ein, vorschlag_ein, lambda _p: None, gegenpruefen=gegen)
            p.titel, p.text, p.offene_faeden = d["title"], d["text"], list(d["openThreads"])
            p.vorschlaege = d["proposals"]
            p.pruefung = d.get("review") or {}
            p.aufrufe = ablauf.zaehler.aufrufe
            p.tokens_ein, p.tokens_aus = d["tokensIn"], d["tokensOut"]
            p.kosten_cent = d.get("costCents", klient.kosten_cent(d["tokensIn"], d["tokensOut"]))
            p.warnungen = list(getattr(ablauf, "warnungen", []) or [])
            p.bereinigt = list(ablauf.bereinigt)
            p.notizen, p.stand = ablauf.letzte_notizen, list(ablauf.letzter_stand)
            p.auswahl, p.pflicht = list(ablauf.letzte_auswahl), dict(ablauf.letzte_pflicht)
            if p.pflicht.get("nachgebessert"):
                p.entwurf = ablauf.letztes_kapitel1
            p.lang = ablauf.letztes_lang
            p.unklar = list(ablauf.letzte_unklar)
            p.zweiter_blick = list(ablauf.letzter_zweiter_blick)
            p.teile = list(ablauf.letzte_teile)
            p.einschnitte = _json_sicher(ablauf.letzte_einschnitte)
            p.kurzer_entwurf = list(ablauf.letzter_kurzer_entwurf)
            with session_factory()() as db:
                db.add(UsageLog(campaign_id=campaign_id, session_id=session_id, kind="probe", engine="external",
                                model=klient.modell, tokens_in=p.tokens_ein, tokens_out=p.tokens_aus,
                                cost_cents=p.kosten_cent, compute_seconds=time.monotonic() - t0))
                db.commit()
        p.zustand = "fertig"
    except SprachmodellFehler as e:
        p.zustand, p.fehler = "fehler", str(e)
    except Exception as e:  # noqa: BLE001 – ein Probelauf darf nie etwas anderes mitreißen
        log.exception("Probelauf %s fehlgeschlagen", p.id)
        p.zustand, p.fehler = "fehler", f"{type(e).__name__}: {e}"
    finally:
        p.dauer_s = time.monotonic() - t0
        p.schritt = ""
        p.beendet = utcnow().isoformat().replace("+00:00", "Z")
        # erst die Dateien, dann der Stand: Wer „fertig“ liest, findet die Dateien schon vor
        _dateien_schreiben(p)
        _speichern(p)


def _dateien_schreiben(p: Probe) -> None:
    o = ordner(p.id)
    try:
        if p.zustand == "fertig":
            text = f"{p.titel}\n\n{text_mit_teilen(p.text, p.teile)}\n"
            if p.offene_faeden:
                text += "\nOffene Fäden:\n" + "\n".join(f"- {f}" for f in p.offene_faeden) + "\n"
            (o / "recap.txt").write_text(text, encoding="utf-8")
            (o / "vorschlaege.json").write_text(json.dumps(p.vorschlaege, ensure_ascii=False, indent=2), encoding="utf-8")
            (o / "pruefung.json").write_text(json.dumps(p.pruefung, ensure_ascii=False, indent=2), encoding="utf-8")
            if p.notizen:
                (o / "notizen.txt").write_text(p.notizen + "\n", encoding="utf-8")
            if p.stand:
                (o / "stand.txt").write_text("\n".join(f"- {s}" for s in p.stand) + "\n", encoding="utf-8")
            if p.auswahl:
                from app.sprachmodell import auswahl_text

                (o / "auswahl.txt").write_text(
                    "\n".join(f"{z} ({a['rang']})" for z, a in zip(auswahl_text(p.auswahl).split("\n"), p.auswahl))
                    + "\n", encoding="utf-8")
            if p.pflicht:
                (o / "pflicht.json").write_text(json.dumps(p.pflicht, ensure_ascii=False, indent=2), encoding="utf-8")
            if p.entwurf:
                (o / "entwurf.txt").write_text(p.entwurf + "\n", encoding="utf-8")
            if p.lang:
                (o / "lang.txt").write_text(p.lang + "\n", encoding="utf-8")
            if p.unklar:
                (o / "unklar.json").write_text(json.dumps(p.unklar, ensure_ascii=False, indent=2), encoding="utf-8")
            if p.einschnitte:
                (o / "einschnitte.json").write_text(json.dumps(p.einschnitte, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
            if p.kurzer_entwurf:
                (o / "rohantwort.txt").write_text("\n\n".join(
                    f"Entwurf mit {k['woerter']} Wörtern (Untergrenze {k['untergrenze']}), Antwort des Modells:\n{k['antwort']}"
                    for k in p.kurzer_entwurf) + "\n", encoding="utf-8")
            if p.zweiter_blick:
                (o / "zweiter-blick.json").write_text(json.dumps(p.zweiter_blick, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
        (o / "ergebnis.json").write_text(json.dumps({k: v for k, v in p.__dict__.items()
                                                      if k not in ("text", "vorschlaege", "pruefung", "besitzer", "notizen", "auswahl",
                                                                  "pflicht", "entwurf", "lang", "korrekturen", "unklar",
                                                                  "zweiter_blick", "einschnitte", "kurzer_entwurf")},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.warning("Probelauf %s: Dateien konnten nicht geschrieben werden", p.id)


# ---------- 0.4.69: Korrektur per Hinweis (nur Messung, ohne Schnittstelle) ----------
KORREKTUR_DURCHGAENGE = 10  # je Probelauf, wie die geplante Obergrenze je Kapitel und Tag
KORREKTUR_ZEICHEN = 1000  # je Hinweis


def korrigieren_starten(db: Session, p: Probe, hinweise: dict[int, str], fehlt: str, user_id: str) -> None:
    """Einen Korrekturdurchgang im Hintergrund starten. hinweise: je Absatz (optional); fehlt: ein freier Text ohne
    Absatzangabe (0.4.70: das eine Feld, wie in der App geplant). Die Hinweise sind wie SL-Notizen nur für die SL; sie
    landen nur in diesem Probelauf. Wirft ValueError mit einem Code für die Meldung."""
    from app.einstellungen import llm_konfig
    from app.models import Campaign
    from app.zusammenfassung import eingabe_bauen, recap_eingabe

    if p.zustand != "fertig" or p.korrektur_laeuft:
        raise ValueError("korrektur_laeuft")
    if len(p.korrekturen) >= KORREKTUR_DURCHGAENGE:
        raise ValueError("korrektur_genug")
    hinweise = {i: h.strip()[:KORREKTUR_ZEICHEN] for i, h in hinweise.items() if h.strip()}
    fehlt = fehlt.strip()[:KORREKTUR_ZEICHEN]
    if not hinweise and not fehlt:
        raise ValueError("korrektur_leer")
    if not ist_sl(db, user_id, p.campaign_id):
        raise ValueError("weg")
    k = llm_konfig(db)
    if k.art in ("lokal", "aus") or (k.art == "api" and not k.api_key):
        raise ValueError("aus" if k.art != "lokal" else "lokal")
    c = db.get(Campaign, p.campaign_id)
    if k.art == "api" and (c is None or not c.allow_cloud_summary):
        raise ValueError("cloud")
    s = db.get(GameSession, p.session_id)
    if s is None:
        raise ValueError("weg")
    ein = recap_eingabe(eingabe_bauen(db, s))
    p.korrektur_laeuft = True
    _speichern(p)
    threading.Thread(target=_korrigieren, args=(p, k, ein, hinweise, fehlt), name=f"korrektur-{p.id[:8]}",
                     daemon=True).start()


def _korrigieren(p: Probe, k, ein: dict, hinweise: dict[int, str], fehlt: str) -> None:
    from app.sprachmodell import Ablauf, SprachmodellFehler, absaetze, hinweis_liste, pruefliste

    vorher = aktueller_text(p)
    runde = {"nr": len(p.korrekturen) + 1, "gestartet": utcnow().isoformat().replace("+00:00", "Z"),
             "hinweise": [{"absatz": i + 1, "text": h} for i, h in sorted(hinweise.items())], "fehlt": fehlt,
             "vorher": vorher, "nachher": "", "geaendert": [], "verworfen": [], "ohne_aenderung": [],
             "anzahl": len(hinweise) + len(hinweis_liste(fehlt) or ([fehlt] if fehlt.strip() else [])),
             "pruefliste": [],
             "woerter_vorher": len(vorher.split()), "kosten_cent": 0, "fehler": None}
    with _REIHE:
        t0 = time.monotonic()
        try:
            if k.art == "attrappe":  # Testmodus: hängt an jeden genannten Absatz eine Markierung
                teile = absaetze(vorher)
                neu = {i: teile[i] + " (Testmodus: korrigiert)" for i in hinweise if 0 <= i < len(teile)}
                runde["nachher"] = "\n\n".join(neu.get(i, t) for i, t in enumerate(teile))
                runde["pruefliste"] = pruefliste(hinweise, hinweis_liste(fehlt), {}, teile, neu)
                runde["geaendert"] = [i + 1 for i in sorted(hinweise) if 0 <= i < len(teile)]
            else:
                from app.zusammenfassung import api_klient

                klient = api_klient(k)
                ablauf = Ablauf(klient)
                text, bericht = ablauf.korrigieren(ein, vorher, hinweise, fehlt, p.notizen)
                runde.update(bericht)
                runde["nachher"] = text if bericht["geaendert"] else ""
                runde["kosten_cent"] = ablauf.zaehler.kosten_cent()
                runde["tokens_ein"], runde["tokens_aus"] = ablauf.zaehler.tokens_in, ablauf.zaehler.tokens_out
                with session_factory()() as db:
                    db.add(UsageLog(campaign_id=p.campaign_id, session_id=p.session_id, kind="probe", engine="external",
                                    model=klient.modell, tokens_in=ablauf.zaehler.tokens_in,
                                    tokens_out=ablauf.zaehler.tokens_out, cost_cents=runde["kosten_cent"],
                                    compute_seconds=time.monotonic() - t0))
                    db.commit()
        except SprachmodellFehler as e:
            runde["fehler"] = str(e)
        except Exception as e:  # noqa: BLE001 – ein Probelauf darf nie etwas anderes mitreißen
            log.exception("Korrektur im Probelauf %s fehlgeschlagen", p.id)
            runde["fehler"] = f"{type(e).__name__}: {e}"
        finally:
            runde["woerter_nachher"] = len((runde["nachher"] or vorher).split())
            frisch = lesen(p.id) or p  # die Seite kann inzwischen nichts geändert haben, aber sicher ist sicher
            frisch.korrekturen = [*frisch.korrekturen, runde]
            frisch.korrektur_laeuft = False
            _speichern(frisch)
            _korrektur_dateien(frisch)


def _korrektur_dateien(p: Probe) -> None:
    o = ordner(p.id)
    try:
        (o / "korrektur.json").write_text(json.dumps(p.korrekturen, ensure_ascii=False, indent=2), encoding="utf-8")
        (o / "recap-korrigiert.txt").write_text(f"{p.titel}\n\n{aktueller_text(p)}\n", encoding="utf-8")
    except OSError:
        log.warning("Probelauf %s: Korrektur-Dateien konnten nicht geschrieben werden", p.id)


def _modellname(k) -> str:
    if k.art != "api":
        return "Testmodus"
    name = k.api_modell
    if k.api_modell_notizen and k.api_modell_notizen != k.api_modell:
        name += f" + Notizen {k.api_modell_notizen}"
    if k.api_modell_vorschlaege and k.api_modell_vorschlaege != k.api_modell:
        name += f" + Vorschläge {k.api_modell_vorschlaege}"
    return name
