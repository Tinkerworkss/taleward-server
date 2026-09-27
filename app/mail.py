"""E-Mails an Kontoinhaber (0.4.0): Adresse bestätigen, Passwort vergessen.

Gleicher Mailserver wie die Benachrichtigungen (Verwaltung → Einstellungen → E-Mail-Versand). Kein Tracking, keine
Bilder, nur Text. Die Adresse dient nur Kontozwecken.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

log = logging.getLogger("mail")
MUSTER = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


class MailFehler(Exception):
    pass


def gueltig(adresse: str) -> bool:
    return bool(MUSTER.match(adresse or "")) and len(adresse) <= 254


def normal(adresse: str) -> str:
    return (adresse or "").strip().lower()


def kann_senden(db: Session) -> bool:
    from app.benachrichtigung import konfig

    return bool(konfig(db).smtp_host)


def senden(db: Session, an: str, betreff: str, text: str) -> None:
    from dataclasses import replace

    from app.benachrichtigung import VersandFehler, _email, konfig

    k = konfig(db)
    if not k.smtp_host:
        raise MailFehler("Kein Mailserver eingerichtet.")
    try:
        _email(replace(k, email_an=an), betreff, text)
    except VersandFehler as e:
        log.warning("Mail an Konto nicht zugestellt: %s", e)
        raise MailFehler(str(e)) from None


TEXTE = {
    "bestaetigen": {
        "de": ("Bitte bestätige deine E-Mail-Adresse für Taleward ({server})",
               "Hallo {name},\n\ndu hast diese Adresse für dein Konto „{benutzer}“ auf {server} eingetragen. Bitte "
               "bestätige sie hier (gilt 24 Stunden):\n\n{link}\n\nWir nutzen die Adresse nur, damit du dein Passwort "
               "selbst zurücksetzen kannst. Warst du das nicht, ignoriere diese Mail einfach.\n\n{betreiber}"),
        "en": ("Please confirm your e-mail address for Taleward ({server})",
               "Hello {name},\n\nyou entered this address for your account “{benutzer}” on {server}. Please confirm "
               "it here (valid for 24 hours):\n\n{link}\n\nWe only use the address so that you can reset your password "
               "yourself. If this wasn't you, simply ignore this e-mail.\n\n{betreiber}"),
    },
    "passwort": {
        "de": ("Neues Passwort für Taleward ({server})",
               "Hallo {name},\n\njemand – hoffentlich du – möchte das Passwort für dein Konto „{benutzer}“ auf "
               "{server} zurücksetzen. Hier wählst du ein neues (gilt eine Stunde und nur einmal):\n\n{link}\n\n"
               "Warst du das nicht, ignoriere diese Mail. Dein bisheriges Passwort bleibt dann gültig.\n\n{betreiber}"),
        "en": ("New password for Taleward ({server})",
               "Hello {name},\n\nsomeone – hopefully you – wants to reset the password for your account “{benutzer}” "
               "on {server}. Choose a new one here (valid for one hour and only once):\n\n{link}\n\nIf this wasn't "
               "you, ignore this e-mail. Your current password stays valid.\n\n{betreiber}"),
    },
}


def text(db: Session, art: str, sprache: str, **werte) -> tuple[str, str]:
    from app.einstellungen import angaben

    a = angaben(db)
    werte = {"server": a.server_name, "betreiber": a.server_operator, **werte}
    betreff, inhalt = TEXTE[art]["en" if sprache == "en" else "de"]
    return betreff.format(**werte), inhalt.format(**werte)
