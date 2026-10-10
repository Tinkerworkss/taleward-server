"""Benachrichtigungen an den Betreiber: per ntfy (Handy-App, ohne Konto) und/oder E-Mail.

Die Wartung ruft `pruefen(db)` regelmäßig auf. Gemeldet wird:

- worker_fehlt: Aufträge warten länger als N Stunden (Standard 6), aber kein passender Worker ist erreichbar
  (nicht in der Betriebsart „Nur Cloud“, dort übernimmt die Cloud).
- fehlschlag: neue endgültig fehlgeschlagene Aufträge (nur Art und Fehlercode, nie Inhalte oder Kampagnennamen –
  die Verwaltung sieht keine Kampagneninhalte).
- kosten_80 / kosten_100: monatliches Kostenlimit zu 80 % bzw. ganz erreicht (je einmal pro Monat).
- sicherung: automatische Sicherung fehlgeschlagen oder älter als 48 Stunden.
- speicher: weniger als 2 GB frei im Datenordner.

Dauerzustände werden höchstens alle 24 Stunden wiederholt und vergessen, sobald sie behoben sind.

Einstellungen (server_meta, Präfix melden.): ntfy_url, ntfy_token (nur schreibbar), smtp_host, smtp_port,
smtp_user, smtp_pass (nur schreibbar), smtp_tls (starttls|ssl|aus), email_von, email_an, sprache (de|en),
stunden (Wartezeit für worker_fehlt).
"""
from __future__ import annotations

import logging
import shutil
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.einstellungen import meta_lesen, meta_schreiben

log = logging.getLogger("benachrichtigung")
WIEDERHOLEN = timedelta(hours=24)
SPEICHER_MIN = 2 * 1024 ** 3
STANDARD_STUNDEN = 6


# ---------------------------------------------------------------- Einstellungen
@dataclass
class Konfig:
    ntfy_url: str
    ntfy_token: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    smtp_tls: str
    email_von: str
    email_an: str
    sprache: str
    stunden: int

    @property
    def ntfy(self) -> bool:
        return bool(self.ntfy_url)

    @property
    def email(self) -> bool:
        return bool(self.smtp_host and self.email_an)

    @property
    def aktiv(self) -> bool:
        return self.ntfy or self.email


def konfig(db: Session) -> Konfig:
    def m(k: str, std: str = "") -> str:
        return meta_lesen(db, f"melden.{k}") or std

    def zahl(k: str, std: int) -> int:
        try:
            return int(m(k, str(std)))
        except ValueError:
            return std

    return Konfig(ntfy_url=m("ntfy_url"), ntfy_token=m("ntfy_token"), smtp_host=m("smtp_host"),
                  smtp_port=zahl("smtp_port", 587), smtp_user=m("smtp_user"), smtp_pass=m("smtp_pass"),
                  smtp_tls=m("smtp_tls", "starttls"), email_von=m("email_von"), email_an=m("email_an"),
                  sprache=m("sprache", "de"), stunden=zahl("stunden", STANDARD_STUNDEN))


# ---------------------------------------------------------------- Versand
class VersandFehler(Exception):
    pass


def _ntfy(k: Konfig, titel: str, text: str, wichtig: bool) -> None:
    teile = urlsplit(k.ntfy_url)
    pfad = teile.path.rstrip("/")
    if teile.scheme not in ("http", "https") or not teile.netloc or "/" not in pfad:
        raise VersandFehler("ntfy-Adresse ungültig (Beispiel: https://ntfy.sh/mein-thema).")
    basis, thema = pfad.rsplit("/", 1)
    kopf = {"Authorization": f"Bearer {k.ntfy_token}"} if k.ntfy_token else {}
    # JSON-Veröffentlichung: Titel mit Umlauten gehen dabei ohne Kopfzeilen-Kodierung
    try:
        r = httpx.post(f"{teile.scheme}://{teile.netloc}{basis}/", headers=kopf, timeout=15,
                       json={"topic": thema, "title": titel, "message": text, "priority": 4 if wichtig else 3,
                             "tags": ["warning" if wichtig else "books"]})
    except httpx.HTTPError as e:
        raise VersandFehler(f"ntfy nicht erreichbar: {e.__class__.__name__}") from None
    if r.status_code >= 400:
        raise VersandFehler(f"ntfy antwortet mit {r.status_code}.")


def _email(k: Konfig, titel: str, text: str) -> None:
    nachricht = EmailMessage()
    nachricht["Subject"] = titel
    nachricht["From"] = k.email_von or k.smtp_user or k.email_an
    nachricht["To"] = k.email_an
    nachricht["Date"] = formatdate(localtime=True)
    nachricht["Message-ID"] = make_msgid(domain=(nachricht["From"].split("@")[-1] or None))
    nachricht.set_content(text)
    try:
        if k.smtp_tls == "ssl":
            verbindung = smtplib.SMTP_SSL(k.smtp_host, k.smtp_port, timeout=20, context=ssl.create_default_context())
        else:
            verbindung = smtplib.SMTP(k.smtp_host, k.smtp_port, timeout=20)
        with verbindung as s:
            if k.smtp_tls == "starttls":
                s.starttls(context=ssl.create_default_context())
            if k.smtp_user:
                s.login(k.smtp_user, k.smtp_pass)
            s.send_message(nachricht)
    except smtplib.SMTPAuthenticationError:
        raise VersandFehler("E-Mail: Anmeldung beim Mailserver abgelehnt (Benutzer oder Passwort).") from None
    except (smtplib.SMTPException, OSError) as e:
        raise VersandFehler(f"E-Mail: {e.__class__.__name__}: {str(e)[:200]}") from None


def senden(db: Session, titel: str, text: str, wichtig: bool = True) -> list[str]:
    """An alle eingerichteten Wege senden. Liefert die Fehler (leer = alles gut)."""
    from app.einstellungen import angaben

    k = konfig(db)
    titel = f"Taleward · {angaben(db).server_name}: {titel}"
    fehler = []
    if k.ntfy:
        try:
            _ntfy(k, titel, text, wichtig)
        except VersandFehler as e:
            fehler.append(str(e))
    if k.email:
        try:
            _email(k, titel, text)
        except VersandFehler as e:
            fehler.append(str(e))
    for f in fehler:
        log.warning("Benachrichtigung nicht zugestellt: %s", f)
    return fehler


# ---------------------------------------------------------------- Texte
TEXTE = {
    "worker_fehlt": ("Aufträge warten, kein Worker erreichbar",
                     "{n} Auftrag/Aufträge warten seit über {h} Stunden, aber kein passender Worker ist verbunden. "
                     "Bitte den lokalen Server und den Worker prüfen (Verwaltung → Transkription)."),
    "worker_pausiert": ("Aufträge warten, Worker pausiert",
                        "{n} Auftrag/Aufträge warten seit über {h} Stunden. Der Worker {namen} ist pausiert – in der "
                        "Worker-App bzw. unter Verwaltung → Transkription auf „Fortsetzen“ tippen."),
    "fehlschlag": ("Aufträge fehlgeschlagen",
                   "{n} Auftrag/Aufträge sind endgültig fehlgeschlagen: {liste}. Details und „Neu starten“ in der "
                   "Verwaltung → Warteschlange."),
    "kosten_80": ("80 % des Kostenlimits erreicht",
                  "Cloud-Kosten diesen Monat: {euro} € von {limit} €. Ab 100 % warten neue Cloud-Aufträge bis zum "
                  "nächsten Monat."),
    "kosten_100": ("Kostenlimit erreicht",
                   "Cloud-Kosten diesen Monat: {euro} € von {limit} €. Neue Cloud-Aufträge warten jetzt bis zum "
                   "nächsten Monat oder bis das Limit steigt (Verwaltung → Einstellungen)."),
    "sicherung": ("Sicherung fehlgeschlagen",
                  "Die automatische Sicherung hat nicht geklappt: {grund}. Bitte in der Verwaltung prüfen und eine "
                  "Sicherung von Hand anlegen."),
    "speicher": ("Speicherplatz knapp",
                 "Im Datenordner sind nur noch {gb} GB frei. Alte Sicherungen löschen oder den Speicher erweitern."),
    "test": ("Test", "Diese Testnachricht zeigt: Benachrichtigungen kommen an."),
    "server_aktualisiert": ("Server aktualisiert auf {nach}",
                            "Der Taleward-Server wurde automatisch von {von} auf {nach} aktualisiert. Vorher wurde eine "
                            "Sicherung angelegt."),
    "server_update_fehler": ("Server-Update hat nicht geklappt",
                             "Das automatische Update von {von} auf {nach} hat nicht geklappt: {meldung} Mehr in der "
                             "Verwaltung → Updates."),
    "server_update": ("Neue Server-Fassung {version}",
                      "Für Taleward gibt es die Server-Fassung {version} (hier läuft {jetzt}). Wie du aktualisierst, "
                      "steht in der Verwaltung → Updates."),
}
EN = {
    "Server aktualisiert auf {nach}": "Server updated to {nach}",
    "Der Taleward-Server wurde automatisch von {von} auf {nach} aktualisiert. Vorher wurde eine "
    "Sicherung angelegt.":
        "The Taleward server was updated automatically from {von} to {nach}. A backup was made first.",
    "Server-Update hat nicht geklappt": "Server update failed",
    "Das automatische Update von {von} auf {nach} hat nicht geklappt: {meldung} Mehr in der "
    "Verwaltung → Updates.":
        "The automatic update from {von} to {nach} failed: {meldung} More in Admin → Updates.",
    "Neue Server-Fassung {version}": "New server version {version}",
    "Für Taleward gibt es die Server-Fassung {version} (hier läuft {jetzt}). Wie du aktualisierst, "
    "steht in der Verwaltung → Updates.":
        "Taleward server version {version} is available (this server runs {jetzt}). How to update is shown in "
        "Admin → Updates.",
    "Aufträge warten, kein Worker erreichbar": "Jobs waiting, no worker reachable",
    "{n} Auftrag/Aufträge warten seit über {h} Stunden, aber kein passender Worker ist verbunden. "
    "Bitte den lokalen Server und den Worker prüfen (Verwaltung → Transkription).":
        "{n} job(s) have been waiting for more than {h} hours, but no suitable worker is connected. Please check "
        "the local server and the worker (Admin → Transcription).",
    "Aufträge warten, Worker pausiert": "Jobs waiting, worker paused",
    "{n} Auftrag/Aufträge warten seit über {h} Stunden. Der Worker {namen} ist pausiert – in der "
    "Worker-App bzw. unter Verwaltung → Transkription auf „Fortsetzen“ tippen.":
        "{n} job(s) have been waiting for more than {h} hours. The worker {namen} is paused – tap “Resume” in the "
        "worker app or under Admin → Transcription.",
    "Aufträge fehlgeschlagen": "Jobs failed",
    "{n} Auftrag/Aufträge sind endgültig fehlgeschlagen: {liste}. Details und „Neu starten“ in der "
    "Verwaltung → Warteschlange.":
        "{n} job(s) failed permanently: {liste}. Details and “Restart” in Admin → Queue.",
    "80 % des Kostenlimits erreicht": "80% of the cost limit reached",
    "Cloud-Kosten diesen Monat: {euro} € von {limit} €. Ab 100 % warten neue Cloud-Aufträge bis zum "
    "nächsten Monat.":
        "Cloud costs this month: €{euro} of €{limit}. From 100%, new cloud jobs wait until next month.",
    "Kostenlimit erreicht": "Cost limit reached",
    "Cloud-Kosten diesen Monat: {euro} € von {limit} €. Neue Cloud-Aufträge warten jetzt bis zum "
    "nächsten Monat oder bis das Limit steigt (Verwaltung → Einstellungen).":
        "Cloud costs this month: €{euro} of €{limit}. New cloud jobs now wait until next month or until the limit "
        "is raised (Admin → Settings).",
    "Sicherung fehlgeschlagen": "Backup failed",
    "Die automatische Sicherung hat nicht geklappt: {grund}. Bitte in der Verwaltung prüfen und eine "
    "Sicherung von Hand anlegen.":
        "The automatic backup did not work: {grund}. Please check the admin area and create a backup manually.",
    "Speicherplatz knapp": "Storage running low",
    "Im Datenordner sind nur noch {gb} GB frei. Alte Sicherungen löschen oder den Speicher erweitern.":
        "Only {gb} GB left in the data folder. Delete old backups or add storage.",
    "Test": "Test",
    "Diese Testnachricht zeigt: Benachrichtigungen kommen an.": "This test message shows: notifications arrive.",
    "Transkription": "transcription", "Zusammenfassung": "summary", "Unterlage": "document",
    "Stimmprofil": "voice profile", "Korrektur": "correction", "älter als 48 Stunden": "older than 48 hours",
}
ARTEN = {"transcribe": "Transkription", "summarize": "Zusammenfassung", "document": "Unterlage",
         "voice_enroll": "Stimmprofil", "revise": "Korrektur"}


def _t(k: Konfig, text: str) -> str:
    return EN.get(text, text) if k.sprache == "en" else text


def melden(db: Session, art: str, wichtig: bool = True, **werte) -> list[str]:
    k = konfig(db)
    titel, text = TEXTE[art]
    werte = {n: (_t(k, v) if isinstance(v, str) else v) for n, v in werte.items()}
    return senden(db, _t(k, titel).format(**werte), _t(k, text).format(**werte), wichtig)


# ---------------------------------------------------------------- Prüfen (aus der Wartung)
def _zeit(db: Session, schluessel: str) -> datetime | None:
    wert = meta_lesen(db, schluessel)
    if not wert:
        return None
    z = datetime.fromisoformat(wert)
    return z if z.tzinfo else z.replace(tzinfo=timezone.utc)


def _zustand(db: Session, art: str, besteht: bool, **werte) -> bool:
    """Dauerzustand: melden, wenn neu oder seit 24 h nicht gemeldet. Behoben → vergessen. True = gemeldet."""
    schluessel = f"melden.zuletzt.{art}"
    if not besteht:
        if meta_lesen(db, schluessel):
            meta_schreiben(db, schluessel, "")
        return False
    zuletzt = _zeit(db, schluessel)
    if zuletzt and utcnow() - zuletzt < WIEDERHOLEN:
        return False
    meta_schreiben(db, schluessel, utcnow().isoformat())
    db.commit()
    melden(db, art, **werte)
    return True


def sicherung_fehlgeschlagen(db: Session, grund: str) -> None:
    meta_schreiben(db, "sicherung.fehler", grund[:300])
    db.commit()


def sicherung_gelungen(db: Session) -> None:
    if meta_lesen(db, "sicherung.fehler"):
        meta_schreiben(db, "sicherung.fehler", "")
        db.commit()


def pruefen(db: Session) -> list[str]:
    """Alle Bedingungen prüfen und ggf. melden. Liefert die gemeldeten Arten (für Tests und Protokoll)."""
    from app import kosten, sicherung
    from app.config import get_settings
    from app.einrichtung import betriebsart
    from app.einstellungen import llm_konfig
    from app.models import Job
    from app.queue import llm_worker_online, pausierte_worker, worker_online

    k = konfig(db)
    if not k.aktiv:
        return []
    gemeldet: list[str] = []
    jetzt = utcnow()

    # Worker fehlt, während Aufträge warten
    grenze = jetzt - timedelta(hours=max(1, k.stunden))
    alt = select(func.count()).select_from(Job).where(Job.state == "queued", Job.engine == "local",
                                                      Job.created_at < grenze)
    n, pausiert = 0, []
    if betriebsart(db) != "cloud" and not worker_online(db, "asr"):
        wartend = db.scalar(alt.where(Job.required_capability == "asr")) or 0
        n += wartend
        pausiert += pausierte_worker(db, "asr") if wartend else []
    if llm_konfig(db).art == "lokal" and not llm_worker_online(db):
        wartend = db.scalar(alt.where(Job.required_capability == "llm")) or 0
        n += wartend
        pausiert += pausierte_worker(db, "llm") if wartend else []
    # Ist ein passender Worker nur pausiert, sagt die Meldung das – statt „kein Worker erreichbar“
    namen = ", ".join(f"„{x}“" for x in dict.fromkeys(pausiert))
    if _zustand(db, "worker_pausiert", n > 0 and bool(namen), n=n, h=k.stunden, namen=namen):
        gemeldet.append("worker_pausiert")
    if _zustand(db, "worker_fehlt", n > 0 and not namen, n=n, h=k.stunden):
        gemeldet.append("worker_fehlt")

    # Neue Fehlschläge (beim allerersten Lauf nur den Stand merken)
    seit = _zeit(db, "melden.fehler_seit")
    if seit is None:
        meta_schreiben(db, "melden.fehler_seit", jetzt.isoformat())
        db.commit()
    else:
        neu = db.execute(select(Job.type, Job.error_code).where(Job.state == "failed", Job.finished_at > seit)).all()
        if neu:
            meta_schreiben(db, "melden.fehler_seit", jetzt.isoformat())
            db.commit()
            liste = ", ".join(sorted({f"{_t(k, ARTEN.get(t, t))} ({c or '?'})" for t, c in neu}))
            melden(db, "fehlschlag", n=len(neu), liste=liste)
            gemeldet.append("fehlschlag")

    # Kostenlimit (je Stufe einmal im Monat)
    limit = kosten.limit_cent(db)
    if limit:
        cent = kosten.monat_cent(db)
        monat = jetzt.strftime("%Y-%m")
        for art, anteil in (("kosten_100", 1.0), ("kosten_80", 0.8)):
            if cent >= limit * anteil:
                schluessel = f"melden.{art}"
                if meta_lesen(db, schluessel) != monat:
                    meta_schreiben(db, schluessel, monat)
                    if art == "kosten_100":
                        meta_schreiben(db, "melden.kosten_80", monat)  # nicht beide auf einmal
                    db.commit()
                    melden(db, art, euro=f"{cent / 100:.2f}", limit=f"{limit / 100:.2f}")
                    gemeldet.append(art)
                break

    # Sicherung
    grund = meta_lesen(db, "sicherung.fehler") or ""
    if not grund and sicherung.einstellungen(db)["auto"]:
        letzte = sicherung.liste()
        if letzte and letzte[0].zeit < jetzt - timedelta(hours=48):
            grund = "älter als 48 Stunden"
    if _zustand(db, "sicherung", bool(grund), grund=grund):
        gemeldet.append("sicherung")

    # Speicherplatz
    try:
        frei = shutil.disk_usage(get_settings().data_dir).free
    except OSError:
        frei = SPEICHER_MIN
    if _zustand(db, "speicher", frei < SPEICHER_MIN, gb=f"{frei / 1024 ** 3:.1f}"):
        gemeldet.append("speicher")

    db.commit()
    return gemeldet
