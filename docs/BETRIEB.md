# Taleward im Betrieb

Die Verwaltung unter `https://<server>/verwaltung` ist die Schaltzentrale für alles, was nicht die Spielrunden selbst
betrifft: Konten, Worker, Warteschlange, Einstellungen und Sicherung. Inhalte der Kampagnen – Recaps, Bibel,
Transkripte, SL-Notizen – zeigt sie bewusst nicht.

Die Installation steht in [INSTALLATION.md](../INSTALLATION.md).

**Inhalt**

- [Ersteinrichtung](#ersteinrichtung)
- [Die Seiten der Verwaltung](#die-seiten-der-verwaltung)
- [Transkription](#transkription)
- [Zusammenfassung](#zusammenfassung)
- [Aufnahmen und Einwilligung](#aufnahmen-und-einwilligung)
- [Stimmprofile](#stimmprofile)
- [Konten und Anmeldung](#konten-und-anmeldung)
- [Benachrichtigungen](#benachrichtigungen)
- [Sicherung](#sicherung)
- [Updates für App und Worker](#updates-für-app-und-worker)
- [Datenschutz und Verträge](#datenschutz-und-verträge)

## Ersteinrichtung

Solange es keinen Verwalter gibt, schreibt der Server beim Start einen **Einrichtungscode** ins Protokoll:

```
  Ersteinrichtung: im Browser öffnen
    <Adresse dieses Servers>/verwaltung/einrichtung?code=K7Q2-M9XA
```

Über diesen Link legst du das erste Verwalterkonto an. Ohne den Code kann niemand den Server übernehmen. Code verloren?
`chronik einrichtungscode` zeigt ihn wieder an (mit Docker: `sudo docker compose exec server chronik einrichtungscode`).
Auf der Taleward-Box geht die Ersteinrichtung ausnahmsweise ohne Code, aber nur aus dem eigenen Netz.

Danach führt ein **Assistent** durch die Einrichtung:

1. **Verein** – Name und Betreiber, wie die App sie anzeigt.
2. **Betriebsart** – *Nur Cloud*, *Lokaler Server* (eigene Worker) oder *beides*.
3. **Zusammenfassung** – wer Recaps und Bibel-Vorschläge schreibt.
4. **Datenschutzhinweis** – aus einer Vorlage, die sich aus den Einstellungen füllt.
5. **Sicherung** – automatisch jede Nacht, auf Wunsch zusätzlich in einen weiteren Ordner.
6. **Erste Spielleitung** – Konto anlegen oder einladen.

Jeder Schritt lässt sich überspringen; offene Punkte zeigt die Übersicht als Checkliste.

## Die Seiten der Verwaltung

| Seite | Was du dort findest |
|---|---|
| **Übersicht** | Warteschlange, Fehlschläge, Speicherplatz, Sicherung, offene Punkte der Einrichtung |
| **Transkription** | Worker koppeln, pausieren oder sperren; eingebauter bzw. lokaler Worker; Sprechermodell; externe Transkription |
| **Zusammenfassung** | Cloud-API, lokales Modell oder Testmodus; Preise und Kostenlimit |
| **Warteschlange** | Alle Aufträge mit Zustand und Fehlermeldung, „Erneut versuchen“ |
| **Konten** | Anlegen, Passwort-Link, Verwalter-Recht, Anmeldearten |
| **Anmeldung** | Google, Discord, Microsoft, Apple einrichten |
| **Updates** | Fassungen von Server, App und Worker; automatisch oder nach Freigabe |
| **Einstellungen** | Angaben für die App, neue Konten, Aufnahmen, Kosten, App-Versionen, Benachrichtigungen, Verträge und Übergabe |

Oben rechts lässt sich die Sprache umschalten (Deutsch/English).

## Transkription

Transkribiert wird von **Workern** – Programmen, die Aufträge beim Server abholen, die Aufnahme verarbeiten und nur
das Ergebnis zurückschicken. Ein Worker braucht keinen offenen Port und kann überall stehen, wo es eine Grafikkarte gibt.

**Worker anbinden**

- **Worker-App** (Windows, Linux): In der Verwaltung **Transkription → Weiteren Worker anbinden → Kopplungscode
  erzeugen**, Adresse und Code in der App eintragen. Der Code gilt 15 Minuten.
- **Eingebauter Worker** (Docker, auf dem Server selbst): siehe
  [INSTALLATION.md](../INSTALLATION.md#worker-auf-dem-server-selbst).
- **Kommandozeile** (ohne App): `chronik worker --koppeln ABCD-EFGH --server https://taleward.meinverein.de` – siehe
  [ENTWICKLUNG.md](ENTWICKLUNG.md#einen-worker-von-hand-betreiben).

Ein **pausierter** Worker bleibt verbunden, bekommt aber keine Aufträge – praktisch, während am selben PC gespielt wird.
Ein **gesperrter** Schlüssel gilt sofort nicht mehr.

**Sprechermodell.** Für die Sprechertrennung braucht es das Modell *pyannote speaker-diarization-community-1*. Es ist
kostenlos, aber man muss einmal den Nutzungsbedingungen zustimmen:

1. Auf [huggingface.co](https://huggingface.co) ein Konto anlegen.
2. Die Seite [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
   öffnen, das kurze Formular ausfüllen und **Agree and access repository** klicken.
3. Unter **Settings → Access Tokens** einen Schlüssel vom Typ *Read* anlegen (beginnt mit `hf_`).
4. In der Verwaltung unter **Transkription → Sprechermodell** eintragen und „Jetzt auf den Server laden“.

Der Server lädt das Modell einmal in fester Fassung und gibt es geprüft an alle Worker weiter. Die Worker brauchen dann
keinen eigenen Zugang, und der Schlüssel verlässt den Server nicht.

**Externe Transkription (optional).** Für Zeiten ohne eigenen Worker kann Mistral (Voxtral) einspringen – etwa
0,3 Cent pro Minute. Standardmäßig aus. Sie greift nur, wenn alles zusammenkommt:

- Anbieter und API-Schlüssel sind unter **Transkription → Externe Transkription** eingetragen.
- Die Spielleitung hat sie für die Kampagne erlaubt (Schalter in der App).
- Eine Aufnahme wartet länger als die eingestellte Zeit (Standard 24 Stunden), und in dieser Zeit war kein Worker
  erreichbar. In der Betriebsart *Nur Cloud* geht sie ohne Wartezeit dorthin.

Die Aufnahme verlässt dann den Verein. Dafür brauchst du einen Vertrag zur Auftragsverarbeitung mit dem Anbieter und
einen Absatz im Datenschutzhinweis (die Vorlage ergänzt ihn).

**Dauer:** Eine Aufnahme von 4 Stunden braucht auf einer Mittelklasse-Grafikkarte mit 8 GB etwa 15–30 Minuten. Die
Worker-App zeigt nach jedem Auftrag die gemessenen Werte.

## Zusammenfassung

Nach der Stimmzuordnung schreibt ein Sprachmodell den Recap und die Vorschläge für die Bibel. Wer das übernimmt, legst
du unter **Zusammenfassung** fest:

| Wahl | Wie | Gut zu wissen |
|---|---|---|
| **Cloud-API** | Mistral Large (EU) oder ein anderer OpenAI-kompatibler Anbieter | Etwa 6 Cent pro 4-Stunden-Session. Der Text der Session geht an den Anbieter – Vertrag zur Auftragsverarbeitung nötig. Die Spielleitung muss es je Kampagne erlauben. |
| **Lokales Modell** | Ollama auf einem Worker (Worker-App: „Recaps auch auf diesem PC schreiben“; Docker: [Ollama-Container](../INSTALLATION.md#recaps-auf-eigener-hardware-ollama)) | Nichts verlässt den Verein. Ein Recap dauert etwa 10–20 Minuten; kleinere Modelle schreiben schwächer. |
| **Testmodus** | Platzhaltertext | Zum Ausprobieren ohne Sprachmodell. |

Bei Mistral kann der Schlüssel der externen Transkription mitgenutzt werden. Ein **Kostenlimit** pro Monat
(Einstellungen → Kosten) gilt für Cloud-Transkription und Cloud-Zusammenfassung zusammen; ist es erreicht, warten die
Aufträge bis zum nächsten Monat.

Dasselbe Sprachmodell wertet auch **SL-Unterlagen** aus (Abenteuer-PDFs, Notizen, Spielerhandouts), die die
Spielleitung in der App hochlädt.

## Aufnahmen und Einwilligung

- Hochgeladen wird nur, wenn **alle Anwesenden** zugestimmt haben – dauerhaft in der eigenen App oder vor Ort auf dem
  Gerät der Spielleitung. Jede Zustimmung und jeder Widerruf wird mit Zeitpunkt protokolliert.
- **Wie lange Aufnahmen bleiben**, stellst du unter **Einstellungen → Aufnahmen** ein:
  - *Bis zur Freigabe des Recaps, höchstens N Tage* (Standard, höchstens 7): Die Zusammenfassung lässt sich gegen die
    Aufnahme prüfen, und die Transkription kann wiederholt werden.
  - *Sofort nach der Umwandlung in Text.*
  - Die App nennt die Frist im Einwilligungstext. Wird die Aufbewahrung **verlängert**, setzt der Server alle
    Zustimmungen zurück; die Mitglieder werden in der App neu gefragt. Kürzer stellen geht jederzeit.
  - Danach den Datenschutzhinweis anpassen (Assistent → Datenschutzhinweis → neu aus der Vorlage).
- Kurze **Hörproben** je Stimme bleiben nur, bis die Spielleitung die Stimmen zugeordnet hat.
- Nie abgeschlossene Uploads werden nach 7 Tagen verworfen.

## Stimmprofile

Wer möchte, legt in der App ein Stimmprofil an: 20–30 Sekunden vorlesen, mit ausdrücklicher Einwilligung. Ein Worker
berechnet daraus einen Stimmabdruck – eine Zahlenreihe; die Aufnahme wird sofort gelöscht. Danach erkennt der Server die
Person in Tischaufnahmen wieder, auch ohne Vorstellungsrunde. Wer das Profil löscht, löscht auch alles daraus Gelernte.

Ohne Stimmprofil hilft eine **Vorstellungsrunde** am Anfang der Aufnahme („Ich bin Lilio und spiele Jemma“, „Ich
leite heute“): Der Server schlägt die Zuordnung der Stimmen dann selbst vor.

## Konten und Anmeldung

- **Neue Konten:** Standard ist „mit Einladungscode“ – wer einen Einladungslink hat, legt sein Konto selbst an. Auf
  „geschlossen“ gestellt, legt nur die Verwaltung Konten an.
- **Passwort vergessen:** Unter **Konten → Passwort-Link erstellen** gibt es einen Link mit QR-Code (einmalig,
  24 Stunden). Kann der Server E-Mails verschicken (Einstellungen → Benachrichtigungen → E-Mail-Versand), bietet die App
  „Passwort vergessen?“ auch selbst an; Mitglieder hinterlegen dafür freiwillig eine E-Mail-Adresse.
- **Anmelden mit Google, Discord, Microsoft oder Apple:** Voraussetzung ist eine eigene Domain mit HTTPS, eingetragen
  unter **Einstellungen → Öffentliche Adresse des Servers**. Unter **Anmeldung** steht für jeden Dienst eine kurze
  Anleitung mit der Rückleitungsadresse, die du beim Dienst einträgst. Apple braucht ein Apple-Entwicklerkonto.
  Jeder eingeschaltete Dienst gehört in den Datenschutzhinweis; die Vorlage ergänzt ihn automatisch.
- **App-Versionen:** Unter Einstellungen lassen sich eine Mindestversion und ein Download-Link festlegen. Ältere Apps
  werden dann gesperrt, bis sie aktualisiert sind.

## Benachrichtigungen

Damit du Probleme merkst, ohne in die Verwaltung zu schauen: **Einstellungen → Benachrichtigungen**.

- **ntfy** (am einfachsten): die App „ntfy“ aufs Handy laden, ein Thema abonnieren, das niemand errät (z. B.
  `meinverein-taleward-7k2q`), und `https://ntfy.sh/meinverein-taleward-7k2q` in der Verwaltung eintragen.
- **E-Mail:** Mailserver, Port, Benutzer und Passwort deines E-Mail-Anbieters, dazu der Empfänger.
- **„Speichern und Testnachricht senden“** prüft, ob alles ankommt.

Gemeldet wird, wenn Aufträge länger als 6 Stunden (einstellbar) ohne Worker warten, Aufträge endgültig fehlschlagen,
80 % bzw. 100 % des Kostenlimits erreicht sind, eine Sicherung fehlschlägt oder älter als 48 Stunden ist, weniger als
2 GB Speicher frei sind oder ein Update scheitert. Inhalte aus Kampagnen stehen nie in den Meldungen.

## Sicherung

Der Server sichert jede Nacht selbst: Datenbank, Bilder und SL-Unterlagen als ZIP-Datei unter `sicherungen/` im
Datenordner. Die Aufbewahrung ist einstellbar; die neuesten drei bleiben immer. Unter **Übersicht → Sicherung** kannst
du sofort sichern, jede Sicherung herunterladen und einen zusätzlichen Ordner angeben (z. B. ein eingebundenes
Netzlaufwerk), in den jede Sicherung kopiert wird.

Eine Sicherung ist eine vollständige Kopie der Datenbank, also auch aller Kampagneninhalte – entsprechend sorgsam
aufbewahren. Wiederherstellen: siehe [INSTALLATION.md](../INSTALLATION.md#sicherung).

## Updates für App und Worker

Der Server fragt einmal am Tag bei GitHub nach neuen Fassungen (**Updates**):

- **App und Worker-App:** Der Server lädt die Dateien (Android-APK, Windows-Installer) selbst herunter, prüft sie und
  bietet sie unter seiner eigenen Adresse an. Handys und Worker fragen nur diesen Server, nie GitHub. Standard ist
  „Automatisch freigeben“; mit „Erst nach meiner Freigabe“ testest du eine neue Fassung zuerst.
- **Worker-App:** Sie installiert neue Fassungen selbst, sobald sie nichts zu tun hat (abschaltbar in ihren
  Einstellungen). Das KI-Paket folgt immer der Fassung des Servers.
- **Web-App:** Enthält ein App-Release die Web-Fassung, liefert der Server sie unter `https://<server>/app/` selbst aus.
  Sonst zeigt „Im Browser öffnen“ auf die zentrale Web-App auf taleward.org, sofern das unter Einstellungen erlaubt ist.
- **Server:** siehe [INSTALLATION.md](../INSTALLATION.md#alltag-und-updates).
- Ohne Internetzugang oder zum Abschalten: `UPDATE_CHECK=false` in der `.env`.

## Wortlisten für die Namensprüfung

Damit die SL nur wirklich unsicher erkannte Namen zur Prüfung bekommt (Schnittstelle 0.4.6), lädt der Server einmal
je eine Wortliste für Deutsch und Englisch von GitHub (feste Fassung mit Prüfsumme, zusammen etwa 37 MB Download,
verkleinert gut 1 MB unter `data/woerterbuch/`). Quelle: Häufigkeitslisten aus Filmuntertiteln (OpenSubtitles 2018),
aufbereitet von Hermit Dave ([FrequencyWords](https://github.com/hermitdave/FrequencyWords)), Lizenz
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Ohne Internetzugang fehlt nur diese eine Quelle – die
Prüfung nutzt dann die Texte der Kampagne und frühere Kapitel und meldet etwas mehr. Der Stand steht unter
Verwaltung → Zusammenfassung → Prüfung.

## Datenschutz und Verträge

- Die **Vorlage für den Datenschutzhinweis** füllt sich aus den Einstellungen (Betreiber, Cloud-Dienste,
  Anmeldedienste, Aufbewahrung). Sie ist keine Rechtsberatung: lesen, Stellen in [eckigen Klammern] ausfüllen und im
  Zweifel prüfen lassen. Veröffentlicht wird sie unter `/datenschutz`.
- Unter **Einstellungen → Auftragsverarbeitung und Übergabe der Verwaltung** stehen die Verträge, die der Verein je nach
  Einstellung braucht (Hosting, Cloud-Dienste), mit dem Datum des Abschlusses – und eine Checkliste für die Übergabe an
  eine neue Verwaltung.
