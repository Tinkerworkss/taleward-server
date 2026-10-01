# Architektur

Wie Taleward aufgebaut ist, wie eine Aufnahme durch das System läuft und welche Regeln dabei gelten. Für den Einstieg
in den Code siehe [ENTWICKLUNG.md](ENTWICKLUNG.md).

## Überblick

```
  App (Android / Browser)                    Worker (PC mit Grafikkarte, Worker-App oder Container)
        │  HTTPS, /api/v1                            │  HTTPS, /worker/v1 – der Worker fragt an, nie umgekehrt
        ▼                                            ▼
  ┌──────────────────────────── Server ─────────────────────────────┐
  │ Konten · Kampagnen · Spoilerschutz · Warteschlange · Verwaltung │
  │ SQLite + Dateiablage (data/)                                    │
  └─────────────────────────────────────────────────────────────────┘
        │ optional, nur mit Freigabe
        ▼
  Cloud-Dienste (externe Transkription, Cloud-API für Recaps)
```

- **Server:** FastAPI, SQLite (WAL), Alembic-Migrationen, Jinja2 für die Verwaltung. Er rechnet selbst nichts
  Aufwendiges – mit Ausnahme von Zusammenfassungen über eine Cloud-API, die er direkt anfragt.
- **Worker:** Programme, die Aufträge abholen: Transkription (WhisperX mit Whisper large-v3, pyannote für die
  Sprechertrennung), Stimmabdrücke und – mit Ollama – lokale Recaps. Sie melden sich mit einem eigenen Schlüssel an
  und brauchen keinen offenen Port.
- **App:** spricht nur mit dem Server. Die Schnittstelle steht in `contract/session-chronik-api.yaml`.
- Ein Server gehört einer **Organisation** (Verein, Laden, Gruppe); darunter liegen Kampagnen mit Mitgliedern in den
  Rollen `gm` (Spielleitung) und `player`. Tokens sind an ihren Server gebunden (JWT-`aud` = Server-ID).

## Weg einer Aufnahme

1. **Session anlegen** (SL): Anwesende und ihre Einwilligung – dauerhaft in der eigenen App oder vor Ort.
2. **Upload** in Teilen zu 5 MiB, fortsetzbar, optional mit Prüfsumme. Vor Start und Abschluss prüft der Server die
   Einwilligung erneut (409 `consent_missing` / `consent_revoked`).
3. **Transkription** als Auftrag `transcribe` in der Warteschlange. Ein Worker holt ihn per Long-Poll, hält eine
   **Lease** (180 s, durch Lebenszeichen verlängert) und liefert Transkript, Stimmgruppen, Hörproben und Stimmabdrücke.
   Ohne Lebenszeichen holt die Wartung den Auftrag zurück; vorübergehende Fehler werden bis zu dreimal versucht.
4. **Stimmen zuordnen:** Der Server schlägt aus Vorstellungsrunde und Stimmprofilen eine Zuordnung vor, die SL
   bestätigt. Danach werden Hörproben und Stimmabdrücke der Session gelöscht. Bei Discord-Aufnahmen (eine Spur je
   Person) entfällt der Schritt.
5. **Zusammenfassung** als Auftrag `summarize`: Recap, offene Fäden und Vorschläge für die Bibel – über eine Cloud-API
   im Server oder über Ollama auf einem Worker. Zitate in den Vorschlägen werden im Transkript nachgeprüft
   (`app/belege.py`, Kennzeichen `evidence_not_found`).
6. **Gegenprüfung** (ab 0.4.6, abschaltbar): Ein eigener Aufruf bewertet jeden Absatz des Recaps gegen die Grundlage,
   beanstandete Absätze werden einmal nachgebessert und noch einmal geprüft. Die Zentrale setzt die genannten Stellen
   auf das echte Transkript (`app/pruefteil.py`) – `Recap.review`, nur für die SL.
7. **Prüfen und veröffentlichen** (SL): Recap bearbeiten, unsicher erkannte Namen korrigieren (Textersetzung oder,
   solange das Audio da ist, erneute Transkription mit der korrigierten Namenshilfe – `app/unsicher.py`), Vorschläge
   annehmen oder verwerfen. Erst dann sehen Spielende den Recap, und angenommene Vorschläge landen in der Bibel.

Die Aufnahme selbst bleibt je nach Einstellung bis zur Freigabe (höchstens 7 Tage) oder wird gleich nach der
Transkription gelöscht (`app/aufbewahrung.py`).

## Spoilerschutz

Der Server setzt durch, wer was sieht – die App zeigt nur an.

- **Unsichtbar heißt 404.** Geheime Einträge, unveröffentlichte Sessions (auch Status und Recap), Vorschläge,
  Transkripte und Kampagnen, in denen man nicht Mitglied ist, liefern für Unbefugte 404. 403 gibt es nur, wenn die Sache
  ohnehin sichtbar und nur die Aktion der SL vorbehalten ist. Die Prüfungen sitzen gesammelt in `app/access.py`.
- **Geheime Teile werden weggelassen**, nicht auf `null` gesetzt: `gmNotes`, Charakter-Hintergründe,
  `hiddenFromMemberIds`. Die Suche der Spielenden durchsucht `gmNotes` nicht.
- **„Wer weiß was“:** Öffentliche Einträge lassen sich vor einzelnen Mitgliedern verbergen – vollständig, also auch in
  Liste, Suche und Ungelesen-Zählern.
- **Neue Bibeleinträge** ohne Angabe sind `gm_only`. Im Zweifel wird lieber zu wenig verraten.
- **Eingaben für das Sprachmodell** legt jeweils genau eine Funktion fest, und Tests prüfen sie:
  - Der Recap sieht nur öffentliche, niemandem verborgene Einträge ohne `gmNotes`.
  - Die Vorschläge sehen die ganze Bibel samt `gmNotes`, weil sie ergänzen statt doppeln sollen; ihr Ergebnis sieht
    nur die SL.
  - SL-Notizen, Charakter-Hintergründe und Kommentare kommen in keiner Eingabe vor.
  - Übernimmt ein öffentlicher Vorschlagstext Formulierungen aus geheimen Texten, wird er als `gm_only` gespeichert.
- **Aufdecken (`reveal`):** Wird ein geheimer Eintrag am Tisch bekannt, schlägt das Modell die Freigabe vor. Den
  geheimen Teil füllt der Server aus dem bisherigen Eintrag, nie das Modell.
- **SL-Unterlagen:** Handouts werden öffentlich, SL-Unterlagen bleiben geheim, gemischte Unterlagen bleiben geheim mit
  Freigabe-Vorschlag. Markierte Abschnitte (`[SL]`, `Geheim:` …) wandern immer in `gmNotes`.
- **Charaktere (ab 0.4.7, `app/charaktere.py`):**
  - Die App ist Ort der Wahrheit. Der Server hält am Mitglied nur eine Kopie und nimmt Änderungen nur von der Person
    selbst und nur mit höherer Fassung an.
  - Der Bibel-Eintrag der Art `pc` entsteht und ändert sich nur durch den Server; dafür gibt es keine Vorschläge.
  - Mitgebrachte Welt wird nie direkt zum Eintrag, sondern zum Vorschlag für die SL. Geheimes ist vor allen anderen
    Spielern verborgen. Die Einreichende sieht nur den Stand, nie den Vorschlag selbst.
  - Wer neu dazukommt, wird in alle nicht-leeren Verborgen-Listen aufgenommen. Die SL bekommt dazu einen Hinweis
    (`gmNotices`).
  - Charakterbögen werden nur gespeichert, nie ausgewertet. Sichtbar sind sie für die SL und die Person, die sie
    hochgeladen hat.
  - Die Chronik enthält nur, was die Person ohnehin sehen darf, und keine Namen anderer Personen.
  - Ab 0.4.8 sehen `characterId` und `characterVersion` nur die Person selbst und die SL – die Kennung ist der
    Schlüssel zu einem offenen Platz.
- **Kampagnen-Umzug (ab 0.4.8, `app/umzug.py`, Dateiformat in [UMZUG.md](UMZUG.md)):**
  - Server sprechen nie miteinander. Die SL packt die Kampagne als Datei und legt sie auf einem anderen Server wieder
    an. Packen und Anlegen laufen im Server selbst, ohne Worker und ohne Sprachmodell.
  - Nie in der Datei: Konten, Namen von Personen, Einwilligungen, Stimmprofile, Transkripte, Hörproben, Audio, der
    Prüfteil des Recaps, Vorschläge, Terminabstimmungen, Lesemarker, Nutzung, Hinweise an die SL, unveröffentlichte
    Kapitel.
  - Persönliches eines Mitglieds nur mit seiner Zustimmung (`moveConsentAt`), private Kommentare nur, wenn beide
    zugestimmt haben. Die Datei enthält SL-Wissen und ist nur für die SL abrufbar.
  - Beim Import werden alle Plätze offene Plätze; Einwilligungen beginnen bei null. Wer mit passendem Charakter
    beitritt, setzt sich auf seinen Platz (immer als Spieler), die SL bekommt einen Hinweis und kann den Platz wieder
    freigeben.

## Einwilligung und Aufbewahrung

- Jede anwesende Person stimmt selbst zu; eine Zustimmung durch die SL stellvertretend gibt es nicht. Gäste ohne Konto
  stimmen vor Ort auf dem Gerät der SL zu.
- Zustimmung, Widerruf, Vor-Ort-Zustimmung und das Zurücksetzen durch die Verwaltung landen mit Zeitpunkt im
  Zustimmungsprotokoll (`consent_log`). Es hängt bewusst nicht per Fremdschlüssel an Kampagnen oder Konten und bleibt
  als Nachweis bestehen.
- **Aufbewahrung** (`ServerInfo.audioRetention`): `until_release` – bis die SL den Recap freigibt, höchstens
  `maxDays` Tage; `immediate` – gleich nach der Transkription. Wird die Aufbewahrung verlängert, setzt der Server alle
  stehenden Zustimmungen zurück, damit niemand einer längeren Speicherung zugestimmt haben muss, als er wusste.
- Hörproben bleiben höchstens bis zur Bestätigung der Stimmen, nie abgeschlossene Uploads höchstens 7 Tage.

## Stimmprofile

- Freiwillig, mit ausdrücklicher Einwilligung (biometrisches Merkmal). Den Abdruck berechnet ein Worker mit demselben
  Modell wie die Sprechertrennung; die Aufnahme wird danach sofort gelöscht, auch bei Fehlschlag.
- Verglichen werden nur Profile von Anwesenden, per Kosinus-Ähnlichkeit, und nur bei gleicher Modellfassung.
- **Lernen** (optional): nach der Bestätigung durch die SL, je Person und Session einmal, nur aus ausreichend Redezeit
  und nur, wenn die Stimme zum Profil passt. Nie aus Discord-Spuren.
- **Löschen** entfernt Profil, alles Gelernte, offene Aufträge und eine noch nicht verarbeitete Aufnahme.

## Worker-Protokoll (`/worker/v1`)

Nicht Teil der App-Schnittstelle; Worker und Server passen über dieselbe Fassung zusammen.

| Endpunkt | Zweck |
|---|---|
| `POST /pair` | Kopplungscode (15 Minuten) gegen einen Schlüssel tauschen |
| `GET /config` | Einstellungen vom Server: Sprechermodell, Serverfassung, beim eingebauten Worker auch Grafikspeicher und Modell |
| `POST /jobs/claim` | Auftrag abholen (Long-Poll, nach Fähigkeiten `asr`, `llm`, `embed`) |
| `GET /jobs/{id}/files/…/chunks/{n}`, `GET /jobs/{id}/voice-audio` | Audio des Auftrags |
| `POST /jobs/{id}/progress`, `/fail` | Lebenszeichen und Fortschritt bzw. Fehler |
| `POST /jobs/{id}/result`, `/summary-result`, `/document-result`, `/voice-result` | Ergebnisse |
| `GET /models`, `/models/file` | Sprechermodell vom Server in fester Fassung |
| `GET /app-update` | Freigegebene Fassung der Worker-App |

Schlüssel werden nur als Prüfsumme gespeichert und sind einzeln widerrufbar. Außerhalb von localhost verbinden sich
Worker nur per HTTPS. Ein verspätetes Ergebnis nach Ablauf der Lease wird abgelehnt.

## Modelle

- Whisper, Sprechermodell und Ausrichtungsmodell laufen in **fester Fassung**; eine neue Fassung kommt nur mit einem
  geprüften Server-Update. So bleiben Stimmabdrücke vergleichbar.
- Das Sprechermodell holt der Server einmal (von Hugging Face mit dem Zugang aus der Verwaltung oder von einem Spiegel
  mit bekannter Prüfsumme) und gibt es an die Worker weiter. Jede Datei wird per SHA-256 geprüft.
- pyannote würde ohne Abschalten Nutzungsdaten senden; der Worker setzt `PYANNOTE_METRICS_ENABLED=false`.
- Modell und Stapelgröße wählt `app/arbeitsweise.py` nach dem verfügbaren Grafikspeicher; ohne passende Karte läuft
  alles auf dem Prozessor.

## Verwaltung

- Zugang nur für Verwalter einer Organisation, mit eigener Sitzung als Cookie (HttpOnly, SameSite=Strict), CSRF-Merkmal
  in jedem Formular und Bremse nach Fehlversuchen. Strenge Sicherheits-Kopfzeilen (CSP, kein Einbetten).
- Die Verwaltung zeigt keine Kampagneninhalte. Ausnahme ist die Sicherung, eine vollständige Kopie der Datenbank.
- Einstellungen aus der Verwaltung liegen in `server_meta` und haben Vorrang vor der `.env`. So bleibt die `.env`
  für Ersteinrichtung und Docker nutzbar.
- Die Ersteinrichtung ist nur mit Einrichtungscode möglich – oder auf der Taleward-Box ohne Code, dann aber nur aus
  privaten Netzen und nur, solange es keinen Verwalter gibt.

## Betrieb mit Docker

- `install.sh` richtet `/opt/taleward` ein: Compose-Datei, Caddy für HTTPS (oder Heimnetz ohne Caddy), `.env` und
  einen systemd-Timer für nächtliche Updates (`aktualisieren.sh`: Sicherung, Bau der neuesten Fassung, Rückfall auf
  die alte, wenn die neue nicht startet).
- Compose-Profile schalten Zusatzdienste zu: `worker` bzw. `worker-cpu` (eingebauter Worker, Schlüssel über ein
  gemeinsames Volume), `ollama` bzw. `ollama-cpu`.
- Die **Taleward-Box** ist Raspberry Pi OS Lite mit Docker und einem vorgebauten Server-Abbild; ein Dienst richtet
  beim ersten Start alles über `install.sh` ohne Rückfragen ein.
