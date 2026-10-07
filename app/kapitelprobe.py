"""Probelauf der Zusammenfassung (Verwaltung, Modul kapitelprobe – app.probelauf ist der Transkriptions-Probelauf): Kapitel und
Vorschläge für eine vorhandene Abschrift erzeugen, ohne die Runde zu verändern – zum Vergleichen von Modellen und Fassungen, auch unterwegs ohne Worker.

Grundlage ist die Abschrift einer Runde auf diesem Server; wahlweise ersetzt eine hochgeladene Textdatei (Zeilen
„[h:mm:ss] Sprecher: Text“, wie `transkript.txt` des Modellvergleichs) die Abschrift, die Kampagne liefert dann nur
Mitglieder und Bibel. Es rechnet, was unter „Zusammenfassung“ eingestellt ist (Cloud-API oder Testmodus); ein lokales
Modell braucht einen Worker und steht hier nicht zur Verfügung. Kosten landen im Verbrauchsprotokoll (Art „probe“).

Ergebnisse liegen als Dateien unter <data_dir>/proben/<id>/ und bleiben bis zum Löschen; der Zustand eines laufenden
Probelaufs steht in stand.json, damit die Seite ihn nach einem Neustart nicht verliert.
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


DATEIEN = ("recap.txt", "vorschlaege.json", "pruefung.json", "transkript.txt", "ergebnis.json")


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


def starten(db: Session, s: GameSession, abschrift: str | None = None, dateiname: str | None = None) -> Probe:
    """Legt die Probe an und rechnet im Hintergrund. Wirft ValueError mit verständlicher Meldung, wenn nichts zu tun ist."""
    from app.einstellungen import llm_konfig
    from app.models import Campaign
    from app.zusammenfassung import eingabe_bauen, recap_eingabe, vorschlag_eingabe

    k = llm_konfig(db)
    if k.art == "lokal":
        raise ValueError("lokal")
    if k.art == "aus" or (k.art == "api" and not k.api_key):
        raise ValueError("aus")
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
    c = db.get(Campaign, s.campaign_id)
    p = Probe(id=new_id(), session_id=s.id, kampagne=c.title if c else "?", kapitel=s.number, quelle=quelle,
              zeilen=len(recap_ein["transkript"]), modell=(k.api_modell if k.art == "api" else "Testmodus"),
              gestartet=utcnow().isoformat().replace("+00:00", "Z"))
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


def _rechnen(p: Probe, k, recap_ein: dict, vorschlag_ein: dict, campaign_id: str, session_id: str) -> None:
    from app.sprachmodell import Ablauf, SprachmodellFehler
    from app.zusammenfassung import api_klient, attrappe, gegenpruefen_an

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

            ablauf = Ablauf(klient, schritt=schritt)
            with session_factory()() as db:
                gegen = gegenpruefen_an(db)
            d = ablauf.ausfuehren(recap_ein, vorschlag_ein, lambda _p: None, gegenpruefen=gegen)
            p.titel, p.text, p.offene_faeden = d["title"], d["text"], list(d["openThreads"])
            p.vorschlaege = d["proposals"]
            p.pruefung = d.get("review") or {}
            p.aufrufe = ablauf.zaehler.aufrufe
            p.tokens_ein, p.tokens_aus = d["tokensIn"], d["tokensOut"]
            p.kosten_cent = klient.kosten_cent(d["tokensIn"], d["tokensOut"])
            p.warnungen = list(getattr(ablauf, "warnungen", []) or [])
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
        _speichern(p)
        _dateien_schreiben(p)


def _dateien_schreiben(p: Probe) -> None:
    o = ordner(p.id)
    try:
        if p.zustand == "fertig":
            text = f"{p.titel}\n\n{p.text}\n"
            if p.offene_faeden:
                text += "\nOffene Fäden:\n" + "\n".join(f"- {f}" for f in p.offene_faeden) + "\n"
            (o / "recap.txt").write_text(text, encoding="utf-8")
            (o / "vorschlaege.json").write_text(json.dumps(p.vorschlaege, ensure_ascii=False, indent=2), encoding="utf-8")
            (o / "pruefung.json").write_text(json.dumps(p.pruefung, ensure_ascii=False, indent=2), encoding="utf-8")
        (o / "ergebnis.json").write_text(json.dumps({k: v for k, v in p.__dict__.items()
                                                      if k not in ("text", "vorschlaege", "pruefung")},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.warning("Probelauf %s: Dateien konnten nicht geschrieben werden", p.id)
