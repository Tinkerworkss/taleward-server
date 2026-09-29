<p align="center"><img src="app/verwaltung/static/marke/taleward-lockup.svg" alt="Taleward" width="280"></p>

<p align="center"><em>Your table's story, kept safe.</em></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-17313B" alt="License: AGPL-3.0"></a>
  <img src="https://img.shields.io/badge/API-0.4.0-9E2A3A" alt="API 0.4.0">
</p>

**Taleward** records your tabletop session, turns it into a recap you can read aloud, and keeps a campaign bible that
separates what the characters know from what only the game master knows. This is the server; the app lives in
[taleward-app](https://github.com/Tinkerworkss/taleward-app).

**Taleward** nimmt eure Pen-&-Paper-Runde auf, macht daraus einen Recap zum Vorlesen und pflegt die Bibel der Kampagne –
getrennt nach Spielerwissen und Geheimnissen der Spielleitung. Dies ist der Server zum Selbstbetreiben, für Vereine,
Läden und Gruppen.

- **Wer weiß was:** Spoilerschutz setzt der Server durch, nicht die App.
- **Datenschutz ohne Kleingedrucktes:** Jede Person stimmt selbst zu, Audio wird nach der Umwandlung in Text gelöscht,
  der Server kann im eigenen Verein stehen.
- **Für die ganze Runde:** Spielende zahlen nie. Kosten entstehen nur bei der Verarbeitung.

Die Schnittstelle zur App steht in [`contract/session-chronik-api.yaml`](contract/session-chronik-api.yaml) (0.4.0);
die Tests prüfen jede Antwort dagegen.

## Installation

Die Schritt-für-Schritt-Anleitung mit Kontrollpunkten und Fehlerhilfe steht in **[INSTALLATION.md](INSTALLATION.md)**.

Worker für den PC mit Grafikkarte: **[Worker-App für Windows und Linux](https://github.com/Tinkerworkss/taleward-worker)** (eigenes Repository).

Mit Docker – gemieteter Server (VPS), eigener PC zu Hause/im Verein oder Windows 11 mit Ubuntu: **[INSTALLATION-VPS.md](INSTALLATION-VPS.md)** – ein Befehl:

```bash
curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/install.sh | sudo bash
```

Kurzfassung für später:

```bash
cd ~/session-chronik-server
uv run pytest                  # erwartet: 269 passed
uv run chronik serve           # Server starten, Strg+C beendet
uv run chronik worker            # zweites Fenster: Worker (--attrappe ohne Grafikkarte)
uv run chronik worker --selbsttest datei.mp3 --sprecher 4   # Einrichtung prüfen, ohne Zentrale
```

Prüfen: `http://<PC-IP>:8000/api/v1/health` liefert `{"status":"ok"}`.

## Weitere Befehle

```bash
uv run chronik list-users
uv run chronik reset-password -u benjamin   # meldet den Benutzer überall ab
uv run chronik serve --reload               # startet bei Codeänderungen neu
uv run chronik worker-token create --name heim-pc   # Zugang für einen Worker (auch in der Verwaltung)
uv run chronik admin -u benjamin            # Zugang zur Verwaltung /verwaltung
uv run chronik stimmen-vergleich a.m4a b.m4a   # Stimmabdrücke vergleichen (Einstellen der Wiedererkennung)
uv run chronik extern-probe folge.mp3 --dauer 10  # externe Transkription ausprobieren (Mistral-Schlüssel nötig)
uv run chronik worker-token list | revoke --name …
```

## Update

Erst eine Sicherung anlegen (Verwaltung → Übersicht → Sicherung), dann `git pull` bzw. die neue Fassung über den
vorhandenen Ordner entpacken und `uv sync` ausführen.
`.env` und `data/` (Datenbank) bleiben dabei erhalten. Die Datenbank wird beim Start automatisch auf den neuen Stand gebracht.

## Aufbau

```
contract/     Schnittstelle (YAML) – Quelle der Wahrheit
app/
  main.py       App, CORS, Fehlerbehandlung, /api/v1
  config.py     Einstellungen aus .env
  db.py         SQLite (WAL), Zeitstempel immer UTC
  models.py     Tabellen
  schemas.py    Anfragen/Antworten, Feldnamen wie in der YAML
  errors.py     Fehlerformat {code, message}, deutsche Meldungen
  security.py   Argon2-Passwörter, JWT
  access.py     Rollen und Spoilerschutz (zentral)
  services.py   Umwandlung und Fachregeln
  routers/      Endpunkte (worker.py = Worker-Protokoll /worker/v1, nicht Teil der App-YAML)
  queue.py      Warteschlange, Leases, Wartung
  storage.py    Ablage von Upload-Teilen und Hörproben unter data/
  worker_prozess.py, audio.py   Worker (holt Aufträge ab, verarbeitet lokal)
  worker_app.py Anbindung an die Worker-App (Ereignisse als JSON-Zeilen, Steuerung über stdin)
  transkription.py  WhisperX-Motor und Auswertung (Stimmen, Hörproben, Abdrücke)
  namenshilfe.py, begriffe/   Namen und Systembegriffe als Hilfe für die Erkennung
  verwaltung/   Weboberfläche /verwaltung (Seiten, Stil, lokaler Worker)
  einstellungen.py    Server-Angaben (.env, überschreibbar in der Verwaltung)
  zuordnung.py  Stimmvorschläge (Vorstellungsrunde, Stimmprofile)
  stimmprofile.py  Stimmprofile: anlegen, vergleichen, lernen, löschen
  extern.py     externe Transkription als Ersatz (Mistral Voxtral)
  zusammenfassung.py  Recap und Vorschläge (Eingabe mit Spoilerschutz, Attrappe, Arbeitsprozess der Zentrale)
  cli.py        Kommandozeile „chronik“
migrations/   Datenbank-Migrationen (Alembic)
tests/        pytest inkl. automatischer Vertragsprüfung
deploy/       Docker-Paket für einen eigenen Server (VPS), install.sh im Hauptverzeichnis
box/          Taleward-Box: Abbild für den Raspberry Pi (bauen.sh, Einrichtung beim ersten Start)
Dockerfile.worker  Worker als Container (Grafikkarte oder Prozessor), Compose-Profil „worker“/„worker-cpu“
engine-requirements.txt  feste Paketversionen des KI-Pakets (für die Worker-App, aus uv.lock erzeugt)
```

`engine-requirements.txt` nach Änderungen an den Abhängigkeiten neu erzeugen:
`uv export --format requirements-txt --extra ki --no-dev --no-hashes --no-emit-project --no-header -o engine-requirements.txt`


## Lizenz

Taleward-Server ist freie Software unter der **GNU Affero General Public License 3.0** ([LICENSE](LICENSE)). Wer den
Server verändert und anderen über ein Netzwerk anbietet, muss ihnen den veränderten Quelltext ebenfalls zugänglich
machen.

### Fremde Bestandteile

- **Schriften Alegreya und Alegreya Sans** (in `app/verwaltung/static/fonts/`): SIL Open Font License 1.1,
  [OFL-Alegreya.txt](app/verwaltung/static/fonts/OFL-Alegreya.txt), [OFL-Alegreya-Sans.txt](app/verwaltung/static/fonts/OFL-Alegreya-Sans.txt).
- **Sprechermodell** pyannote speaker-diarization-community-1 (nicht im Repository; der Server lädt es und gibt es
  unverändert an die Worker weiter): CC-BY-4.0, © pyannoteAI. Namensnennung in `NOTICE.txt` jeder Ablage.
- **Spracherkennung** Whisper large-v3 (faster-whisper/CTranslate2-Fassung, nicht im Repository): MIT-Lizenz.
- Python-Bibliotheken: siehe `pyproject.toml` und `uv.lock`, jeweils unter ihren eigenen Lizenzen.
- Name und Logo „Taleward“ sind nicht Teil der freien Lizenz des Quelltexts.

## Mitmachen

Fehler und Wünsche gern als Issue. Die Entwicklungsnotizen der einzelnen Schritte stehen in
[docs/ENTWICKLUNG.md](docs/ENTWICKLUNG.md).
