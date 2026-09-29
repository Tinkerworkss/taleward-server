"""Ersteinrichtung: Einrichtungscode statt admin/admin, Betriebsart, Stand der Einrichtung (Checkliste).

Einrichtungscode: Solange es keinen Verwalter gibt, erzeugt der Server einen einmaligen Code und schreibt den Link
ins Protokoll (bei Docker: „docker compose logs“; sonst „chronik einrichtungscode“). Nur mit diesem Code lässt sich
der erste Verwalter anlegen – ein frisch gestarteter Server im Internet ist so nicht offen.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.einstellungen import meta_lesen, meta_schreiben
from app.models import Campaign, OrgMember, User, Worker

log = logging.getLogger("einrichtung")
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O, 1/I
BETRIEBSARTEN = ("cloud", "lokal", "beides")


def braucht_einrichtung(db: Session) -> bool:
    """Es gibt noch keinen Verwalter (das alte Einrichtungskonto admin/admin zählt nicht)."""
    return db.scalar(select(func.count()).select_from(OrgMember).join(User, User.id == OrgMember.user_id)
                     .where(OrgMember.role == "admin", User.setup_account.is_(False))) == 0


def code_erzeugen(db: Session, neu: bool = False) -> str:
    code = meta_lesen(db, "einrichtung.code")
    if code and not neu:
        return code
    code = "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(2))
    meta_schreiben(db, "einrichtung.code", code)
    db.commit()
    return code


def code_pruefen(db: Session, code: str) -> bool:
    soll = meta_lesen(db, "einrichtung.code")
    ist = (code or "").strip().upper().replace(" ", "")
    return bool(soll) and braucht_einrichtung(db) and hmac.compare_digest(soll.replace("-", ""), ist.replace("-", ""))


def heimnetz_ohne_code(request) -> bool:
    """Taleward-Box: Ersteinrichtung ohne Code – nur wenn so eingeschaltet (EINRICHTUNG_IM_HEIMNETZ) und die Anfrage
    aus einem privaten Netz kommt (Heimnetz, Vereins-WLAN). Aus dem Internet gilt weiter der Code."""
    import ipaddress

    from app.config import get_settings

    if not get_settings().einrichtung_im_heimnetz or request.client is None:
        return False
    try:
        adresse = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return adresse.is_private or adresse.is_loopback or adresse.is_link_local


def code_verbrauchen(db: Session) -> None:
    meta_schreiben(db, "einrichtung.code", "")


def beim_start(db: Session) -> str | None:
    """Beim Serverstart: Wird noch eingerichtet, Code sicherstellen und deutlich ins Protokoll schreiben."""
    if not braucht_einrichtung(db):
        return None
    code = code_erzeugen(db)
    from app.einstellungen import angaben

    adresse = (angaben(db).public_url or "<Adresse dieses Servers>").rstrip("/")
    log.warning("\n%s\n  Ersteinrichtung: im Browser öffnen\n    %s/verwaltung/einrichtung?code=%s"
                "\n  (Code jederzeit neu anzeigen: chronik einrichtungscode)\n%s", "=" * 64, adresse, code, "=" * 64)
    return code


# ---------------------------------------------------------------- Betriebsart
def betriebsart(db: Session) -> str | None:
    wert = meta_lesen(db, "betrieb.art")
    return wert if wert in BETRIEBSARTEN else None


# ---------------------------------------------------------------- Stand der Einrichtung
@dataclass
class Punkt:
    schluessel: str
    titel: str  # deutscher Text = Übersetzungsschlüssel
    erledigt: bool
    link: str


def stand(db: Session) -> list[Punkt]:
    """Was für den Betrieb noch fehlt – aus dem tatsächlichen Zustand, nicht aus Häkchen im Assistenten."""
    from app import modellablage, sicherung
    from app.config import get_settings
    from app.modelle import SPRECHERMODELL
    from app.einstellungen import angaben, extern_konfig, llm_konfig

    s = get_settings()
    a = angaben(db)
    art = betriebsart(db)
    k = llm_konfig(db)
    lokal = art in ("lokal", "beides")
    worker_da = db.scalar(select(func.count()).select_from(Worker).where(
        Worker.revoked_at.is_(None), Worker.last_seen_at.is_not(None))) > 0
    cloud_da = extern_konfig(db).anbieter is not None
    punkte = [
        Punkt("verein", "Angaben zum Verein", bool(meta_lesen(db, "angabe.server_operator"))
              or a.server_operator != s.server_operator, "/verwaltung/assistent/verein"),
        Punkt("betrieb", "Betriebsart und Transkription",
              art is not None and ((not lokal or worker_da) and (art == "lokal" or cloud_da)),
              "/verwaltung/assistent/betrieb"),
    ]
    if lokal:
        punkte.append(Punkt("hf", "Zugang zum Sprechermodell (Hugging Face)",
                            bool(meta_lesen(db, "hf.token") or s.hf_token or meta_lesen(db, f"modell.{SPRECHERMODELL}")
                                 or modellablage.spiegel_nutzbar()), "/verwaltung/assistent/betrieb"))
    punkte += [
        Punkt("sprachmodell", "Zusammenfassung (Sprachmodell)", k.art == "lokal" or (k.art == "api" and bool(k.api_key)),
              "/verwaltung/assistent/zusammenfassung"),
        Punkt("datenschutz", "Datenschutzhinweis", bool(a.privacy_policy_url), "/verwaltung/assistent/datenschutz"),
        Punkt("sicherung", "Automatische Sicherung", sicherung.einstellungen(db)["auto"] and bool(sicherung.liste()),
              "/verwaltung/assistent/sicherung"),
        Punkt("melden", "Benachrichtigungen (ntfy oder E-Mail)",
              bool(meta_lesen(db, "melden.ntfy_url") or meta_lesen(db, "melden.email_an")),
              "/verwaltung/einstellungen#benachrichtigungen"),
        Punkt("spielleitung", "Erste Spielleitung", db.scalar(select(func.count()).select_from(Campaign)) > 0
              or db.scalar(select(func.count()).select_from(User)) > 1, "/verwaltung/assistent/spielleitung"),
    ]
    return punkte
