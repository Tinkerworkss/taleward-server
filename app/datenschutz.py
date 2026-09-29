"""Vorlage für den Datenschutzhinweis – aus den Einstellungen des Servers befüllt.

Das ist eine VORLAGE und keine Rechtsberatung. Der Betreiber liest sie, passt sie an und veröffentlicht sie erst dann
(unter /datenschutz). Abschnitte mit „[…]“ muss er selbst ausfüllen.

Format des gespeicherten Texts: Absätze durch Leerzeilen, Überschriften mit „## “, Aufzählungen mit „- “.
"""
from __future__ import annotations

from sqlalchemy.orm import Session


def vorlage(db: Session, hosting: str = "") -> str:
    from app.config import get_settings
    from app.einrichtung import betriebsart
    from app.einstellungen import angaben, extern_konfig, llm_konfig

    a = angaben(db)
    art = betriebsart(db)
    ext = extern_konfig(db)
    k = llm_konfig(db)
    kontakt = a.server_contact or "[E-Mail-Adresse für Datenschutzfragen]"
    mistral_transkription = ext.anbieter == "mistral" or art in ("cloud", "beides")
    mistral_llm = k.art == "api" and k.ist_mistral
    anderer_llm = k.art == "api" and not k.ist_mistral
    tage = get_settings().audio_retention_days
    from app.aufbewahrung import lesen as aufbewahrung

    frist = aufbewahrung(db)
    from app import anmeldedienste, mail

    email = mail.kann_senden(db)
    dienste = anmeldedienste.eingerichtet(db)
    dienst_namen = [anmeldedienste.DIENSTE[d]["name"] for d in dienste]

    t = [f"# Datenschutzhinweis für {a.server_name}",
         f"Stand: [Datum]. Dieser Hinweis gilt für die App Taleward in Verbindung mit dem Server „{a.server_name}“, "
         f"den {a.server_operator} betreibt.",
         "## Verantwortlich",
         f"{a.server_operator}\n[Anschrift]\n{kontakt}",
         "## Was die App macht",
         "Taleward nimmt Pen-&-Paper-Runden auf (nur mit Zustimmung aller Anwesenden), wandelt die Aufnahme in Text um "
         "und erstellt daraus eine Zusammenfassung („Recap“) und Vorschläge für die Kampagnen-Bibel. Die Spielleitung "
         "prüft alles, bevor die Runde es sieht.",
         "## Welche Daten wir verarbeiten",
         "- Konto: Benutzername, Anzeigename, Passwort (nur als Prüfwert gespeichert), Zeitpunkt der Zustimmung zu "
         "diesem Hinweis und der Altersbestätigung.",
         *(["- Freiwillig: eine E-Mail-Adresse – nur, damit du dein Passwort selbst zurücksetzen kannst. Du kannst sie "
            "in der App jederzeit entfernen."] if email else []),
         *([f"- Freiwillig, wenn du dich mit {', '.join(dienst_namen)} anmeldest: deine Kennung bei diesem Dienst, "
            "dein Name und – falls vom Dienst bestätigt – deine E-Mail-Adresse. Zugangsdaten des Dienstes speichern "
            "wir nicht."] if dienst_namen else []),
         "- Mitgliedschaften in Kampagnen: Rolle, Charaktername, Charakterbeschreibung und -hintergrund, "
         "Charakterbild, Kommentare, Antworten in Terminabstimmungen.",
         "- Aufnahmen von Spielrunden und daraus entstehende Transkripte, Zusammenfassungen und Einträge.",
         "- Freiwillig: ein Stimmprofil (Stimmabdruck), mit dem die App Stimmen in Aufnahmen wiedererkennt. Das ist "
         "ein biometrisches Merkmal; wir verarbeiten es nur mit ausdrücklicher Einwilligung (Art. 9 Abs. 2 lit. a "
         "DSGVO), die jederzeit in der App widerrufen werden kann.",
         "- Technisch nötige Daten beim Aufruf des Servers (IP-Adresse, Zeitpunkt), nur für den Betrieb und die "
         "Abwehr von Missbrauch.",
         "## Zweck und Rechtsgrundlage",
         "Wir verarbeiten die Daten, um die Runden unseres Vereins zu dokumentieren und den Mitgliedern die "
         "Zusammenfassungen bereitzustellen (Art. 6 Abs. 1 lit. b DSGVO – Nutzung des Angebots; bei Aufnahmen und "
         "Stimmprofilen Art. 6 Abs. 1 lit. a bzw. Art. 9 Abs. 2 lit. a DSGVO – Einwilligung). Aufgenommen wird nur, "
         "wenn alle Anwesenden zugestimmt haben; die Zustimmung lässt sich in der App jederzeit widerrufen.",
         "## Wie lange wir Daten speichern",
         (f"- Aufnahmen: werden in Text umgewandelt und bleiben zur Prüfung der Zusammenfassung auf dem Server, bis "
          f"die Spielleitung den Recap freigibt, höchstens {frist.tage} {'Tag' if frist.tage == 1 else 'Tage'}, und "
          "werden dann gelöscht." if frist.bis_freigabe else
          f"- Aufnahmen: werden gelöscht, sobald sie in Text umgewandelt sind, spätestens nach {tage} Tagen.")
         + " Kurze Hörproben je Stimme bleiben nur, bis die Spielleitung die Stimmen zugeordnet hat.",
         "- Stimmprofile: bis zum Widerruf; Löschen entfernt auch alles daraus Gelernte.",
         "- Konto und Inhalte: bis zur Löschung des Kontos (in der App möglich). Beiträge zu gemeinsamen "
         "Kampagnen (Kommentare, Zusammenfassungen) bleiben danach ohne Namen als „Gelöschtes Konto“ stehen.",
         "- Sicherungskopien: bis zu [14] Tage.",
         "## Wer die Daten sonst erhält"]
    empfaenger = [f"- Betrieb des Servers: {hosting or '[Hosting-Anbieter, z. B. Hostinger International Ltd., Zypern]'}, "
                  "Rechenzentrum in [Land in der EU] (Auftragsverarbeitung nach Art. 28 DSGVO). [Aus dem Vertrag "
                  "übernehmen: ob der Anbieter Unterauftragsverarbeiter außerhalb der EU einsetzt und auf welcher "
                  "Grundlage, z. B. Standardvertragsklauseln.]"]
    if art in ("lokal", "beides"):
        empfaenger.append("- Umwandlung von Aufnahmen in Text: auf einem Server unseres Vereins [Standort], ohne "
                          "Weitergabe an Dritte.")
    if mistral_transkription or mistral_llm:
        zwecke = " und ".join(z for z in ("die Umwandlung von Aufnahmen in Text" if mistral_transkription else "",
                                          "das Erstellen von Zusammenfassungen" if mistral_llm else "") if z)
        empfaenger.append(f"- Für {zwecke}: Mistral AI SAS, Paris (Frankreich, EU), als Auftragsverarbeiter nach "
                          "Art. 28 DSGVO. [Aus dem Vertrag übernehmen: ob und wie lange der Anbieter die Daten "
                          "speichert und ob er sie zum Training nutzt.]")
    if anderer_llm:
        empfaenger.append("- Für das Erstellen von Zusammenfassungen: [Anbieter, Sitz, Grundlage bei Übermittlung "
                          "außerhalb der EU].")
    if k.art == "lokal":
        empfaenger.append("- Zusammenfassungen entstehen auf einem Server unseres Vereins, ohne Weitergabe an Dritte.")
    anbieter = {"google": "Google Ireland Limited, Irland", "microsoft": "Microsoft Ireland Operations Limited, Irland",
                "apple": "Apple Distribution International Ltd., Irland",
                "discord": "[Discord – Anbieter und Sitz laut Datenschutzerklärung von Discord]"}
    for d in dienste:
        empfaenger.append(f"- Anmeldung mit {anmeldedienste.DIENSTE[d]['name']} (freiwillig): Wählst du sie, meldest "
                          f"du dich bei {anbieter[d]} an; der Dienst erfährt dabei, dass du dich bei uns anmeldest. "
                          "Für diese Anmeldung ist der Dienst selbst verantwortlich; es gilt seine "
                          "Datenschutzerklärung. [Übermittlung außerhalb der EU laut Dienst prüfen.]")
    if email:
        empfaenger.append("- E-Mails zu deinem Konto verschicken wir über [E-Mail-Anbieter des Vereins].")
    empfaenger.append("- Innerhalb einer Kampagne sehen die Mitglieder, was die Spielleitung freigibt. Transkripte, "
                      "Vorschläge und geheime Notizen sieht nur die Spielleitung.")
    t.append("\n".join(empfaenger))
    t += ["## Deine Rechte",
          "Du hast das Recht auf Auskunft, Berichtigung, Löschung, Einschränkung der Verarbeitung, "
          "Datenübertragbarkeit und Widerspruch sowie auf Widerruf erteilter Einwilligungen. In der App kannst du "
          "deine Daten selbst exportieren und dein Konto löschen. Für alles andere wende dich an "
          f"{kontakt}. Du kannst dich außerdem bei einer Datenschutz-Aufsichtsbehörde beschweren, "
          "z. B. [zuständige Landesbehörde].",
          "## Mindestalter",
          f"Die App ist für Personen ab {a.min_age} Jahren gedacht. Jüngere brauchen die Zustimmung ihrer Eltern."]
    return "\n\n".join(t)


def html(text: str) -> str:
    """Gespeicherten Text sicher als HTML ausgeben (keine Formatierung außer Überschriften und Listen)."""
    from html import escape

    teile = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("# "):
            teile.append(f"<h1>{escape(block[2:])}</h1>")
        elif block.startswith("## "):
            teile.append(f"<h2>{escape(block[3:])}</h2>")
        elif all(z.startswith("- ") for z in block.split("\n")):
            teile.append("<ul>" + "".join(f"<li>{escape(z[2:])}</li>" for z in block.split("\n")) + "</ul>")
        else:
            teile.append("<p>" + "<br>".join(escape(z) for z in block.split("\n")) + "</p>")
    return "\n".join(teile)
