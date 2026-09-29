# Entwicklung

Alles, was du brauchst, um am Taleward-Server zu arbeiten: Umgebung, Tests, Kommandozeile und die Regeln im Repository.
Wie das System aufgebaut ist und warum, steht in [ARCHITEKTUR.md](ARCHITEKTUR.md).

**Inhalt**

- [Umgebung einrichten](#umgebung-einrichten)
- [Tests](#tests)
- [Server und Worker lokal starten](#server-und-worker-lokal-starten)
- [Echte Transkription (KI-Paket)](#echte-transkription-ki-paket)
- [Einen Worker von Hand betreiben](#einen-worker-von-hand-betreiben)
- [Kommandozeile `chronik`](#kommandozeile-chronik)
- [Aufbau des Repositorys](#aufbau-des-repositorys)
- [Regeln für Änderungen](#regeln-für-änderungen)
- [Fassungen und Veröffentlichung](#fassungen-und-veröffentlichung)

## Umgebung einrichten

Voraussetzungen: Linux, macOS oder Windows mit WSL2 (Ubuntu), dazu `git`, `curl` und `ffmpeg`.

```bash
sudo apt install -y git curl ffmpeg                  # Ubuntu/Debian/WSL
curl -LsSf https://astral.sh/uv/install.sh | sh      # uv verwaltet Python 3.11 und alle Pakete
git clone https://github.com/Tinkerworkss/taleward-server && cd taleward-server
uv sync
cp .env.example .env                                 # optional, alle Werte haben Standards
```

Unter WSL das Repository im Linux-Dateisystem ablegen (`~/…`), nicht unter `/mnt/c/…` – dort ist alles sehr langsam.

Den geheimen Schlüssel für Anmeldungen erzeugt der Server beim ersten Start selbst (`data/geheimnis.txt`). Datenbank,
Uploads, Hörproben, Bilder und Sicherungen liegen unter `data/` (`DATA_DIR`).

## Tests

```bash
uv run pytest                 # alles, etwa 6 Minuten
uv run pytest tests/test_kampagnen_verwalten.py -x   # einzelne Datei
```

- Die Tests brauchen weder Grafikkarte noch KI-Pakete: Transkription und Sprachmodell laufen über Test-Motoren bzw.
  nachgebaute Anbieter.
- **Jede Antwort wird gegen die Schnittstelle geprüft.** Der Test-Client (`tests/conftest.py`, `tests/contract.py`)
  vergleicht Statuscode, Content-Type und Körper mit `contract/session-chronik-api.yaml`. Ein Endpunkt, der nicht in
  der YAML steht oder anders antwortet, lässt den Test scheitern.
- `tests/test_verwaltung2.py::test_jeder_text_ist_uebersetzt` prüft, dass jeder Text der Verwaltung übersetzt ist und
  die Platzhalter übereinstimmen.
- Für Tests mit echtem Audio wird `ffmpeg` gebraucht.

## Server und Worker lokal starten

```bash
uv run chronik serve                 # http://localhost:8000, Strg+C beendet
uv run chronik serve --reload        # startet bei Codeänderungen neu
```

Beim ersten Start schreibt der Server einen Link zur Ersteinrichtung ins Protokoll
(`http://localhost:8000/verwaltung/einrichtung?code=…`). Alternativ per Kommandozeile:

```bash
uv run chronik create-user -u alex -n Alex --admin     # Konto mit Verwalter-Recht
uv run chronik demo-data                               # Demo-Kampagne mit Konten „sl“ und „spieler“
```

Die Demo-Kampagne hat ein Kapitel, das schon auf die Prüfung wartet – gut, um App oder Oberfläche ohne Aufnahme
auszuprobieren. Passwort beider Konten: `chronik-demo`.

**Worker im Testmodus** (ohne Grafikkarte, schreibt Platzhaltertext statt einer Transkription, verarbeitet das Audio
aber wirklich):

```bash
uv run chronik worker-token create --name dev      # einmalig; Zeile als WORKER_TOKEN=… in die .env
uv run chronik worker --testmodus                  # zweites Terminal
```

Oder in der Verwaltung unter **Transkription → Lokaler Worker** starten; mit „Beim Start des Servers automatisch
mitstarten“ reicht dann ein Terminal.

**Mit der App testen:** Die App erreicht den Server im selben Netz unter `http://<IP-des-Rechners>:8000`. Unter WSL
braucht es dafür das gespiegelte Netz (`networkingMode=mirrored` in `%USERPROFILE%\.wslconfig`, danach
`wsl --shutdown`) und eine Freigabe von Port 8000 in der Windows-Firewall. CORS ist für `http://localhost:5173`
(Browser-Entwicklung der App) und `http://localhost` (Android-App) voreingestellt.

## Echte Transkription (KI-Paket)

Für echte Transkription braucht der Worker WhisperX, pyannote und PyTorch (etwa 5 GB) sowie eine NVIDIA-Grafikkarte
mit aktuellem Treiber (ab 570, CUDA 12.8). Unter WSL kommt der Treiber von Windows – in Ubuntu keinen Treiber
installieren.

```bash
uv sync --extra ki           # ab jetzt immer mit --extra ki, sonst werden die Pakete wieder entfernt
uv run python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

**Sprechermodell:** Am einfachsten liegt es auf dem Server (Verwaltung → Transkription → Sprechermodell, siehe
[BETRIEB.md](BETRIEB.md#transkription)); der Worker lädt es von dort. Ohne Server – etwa für den Probelauf – braucht
der Worker einen eigenen Hugging-Face-Zugang: `HF_TOKEN=hf_…` in der `.env`, nachdem du den Bedingungen des Modells
zugestimmt hast.

**Probelauf** – Transkription und Sprechertrennung mit einer echten Aufnahme, ohne Server und Datenbank:

```bash
uv run chronik probelauf aufnahme.mp3 --dauer 10                   # erste 10 Minuten
uv run chronik probelauf aufnahme.mp3 --sprecher 5 --namen "Gareth,Borbarad"
uv run chronik probelauf aufnahme.mp3 --sprecher 5 --tisch         # Handy am Spieltisch simulieren
```

Die Ergebnisse (Transkript, Bericht mit Zeiten und Grafikspeicher) landen in einem Ordner, den der Befehl am Ende
nennt. `--namen` hilft Whisper bei Eigennamen. Weitere Einstellungen: `uv run chronik probelauf --help`.

**Selbsttest des Workers** – derselbe Weg wie ein echter Auftrag, nur ohne Server:

```bash
uv run chronik worker --selbsttest aufnahme.mp3 --sprecher 4 --namen "Jemma Reed,Litha Flamel"
```

**Modelle in fester Fassung:** Whisper, Sprechermodell und Ausrichtungsmodell werden einmal in fester Fassung geladen
und danach ohne Nachfrage bei Hugging Face genutzt (`app/modelle.py`, `data/modelle.json`). `uv run chronik modelle
--laden` zeigt die Fassungen und lädt fehlende.

| Problem | Lösung |
|---|---|
| „PyTorch sieht keine Grafikkarte“ / „driver … too old“ | Treiber aktualisieren (unter WSL: in Windows), neu starten |
| `libcudnn…` / `libcublas…` nicht gefunden | `uv sync --extra ki` noch einmal |
| „Hugging Face verweigert den Zugriff“ | Bedingungen des Modells akzeptiert? Schlüssel vollständig? |
| `CUDA out of memory` | `--batch 4`, sonst `--modell large-v3-turbo`; andere Programme mit Grafiklast schließen |

## Einen Worker von Hand betreiben

Ohne Worker-App, etwa auf einem Linux-Rechner mit Grafikkarte:

```bash
uv sync --extra ki
uv run chronik worker --koppeln ABCD-EFGH --server https://taleward.meinverein.de   # Code aus der Verwaltung
uv run chronik worker                                                              # danach reicht das
```

Der Worker trägt Schlüssel und Adresse selbst in seine `.env` ein und lädt das Sprechermodell vom Server.
`uv run chronik worker --automatisch` wählt Modell und Stapelgröße nach dem freien Grafikspeicher (so läuft auch der
eingebaute Worker im Docker-Container). Für lokale Recaps braucht er ein laufendes Ollama (`WORKER_LLM_URL`, Standard
`http://localhost:11434`).

## Kommandozeile `chronik`

| Befehl | Zweck |
|---|---|
| `serve [--reload]` | Server starten |
| `worker [--testmodus] [--automatisch] [--koppeln CODE --server URL] [--selbsttest DATEI]` | Worker starten, koppeln oder prüfen |
| `create-user`, `list-users`, `reset-password -u NAME`, `admin -u NAME` | Konten; `admin` vergibt das Verwalter-Recht |
| `einrichtungscode [--neu]` | Link zur Ersteinrichtung anzeigen |
| `worker-token create/list/revoke` | Schlüssel für Worker |
| `demo-data` | Demo-Kampagne anlegen |
| `sicherung`, `wiederherstellen DATEI` | Sicherung anlegen bzw. einspielen |
| `modelle [--laden]`, `modell-holen`, `modell-spiegel ORDNER` | Modellfassungen anzeigen, auf den Server holen, Spiegel erzeugen |
| `probelauf DATEI` | Transkription ohne Server ausprobieren |
| `recap-probe [ID] [--lokal]` | Recap und Vorschläge für eine vorhandene Session erzeugen, ohne zu speichern |
| `unterlage-probe DATEI --art gm/mixed/handout [--lokal]` | Auswertung einer SL-Unterlage, ohne zu speichern |
| `stimmen-vergleich A B [C …]` | Ähnlichkeit von Stimmabdrücken – zum Einstellen der Wiedererkennung |
| `extern-probe DATEI --dauer 10` | Externe Transkription ausprobieren (Schlüssel nötig, kostet wenige Cent) |

Jeder Befehl erklärt sich mit `--help`.

## Aufbau des Repositorys

```
contract/        Schnittstelle App ↔ Server (OpenAPI 3.1) – Quelle der Wahrheit
app/
  main.py          App, CORS, Fehlerbehandlung, Wartung beim Start
  config.py        Einstellungen aus Umgebung bzw. .env (Variablenname = Feldname)
  db.py, models.py SQLite (WAL), Tabellen; Zeitstempel immer UTC
  schemas.py       Anfragen und Antworten, Feldnamen wie in der YAML (camelCase nach außen)
  errors.py        Fehlerformat {code, message}, Meldungen auf Deutsch und Englisch
  access.py        Anmeldung, Rollen und Spoilerschutz – an einer Stelle
  services.py      Umwandlung und Fachregeln
  routers/         Endpunkte der App-Schnittstelle; worker.py = Worker-Protokoll /worker/v1
  queue.py         Warteschlange, Leases, Wartung
  aufbewahrung.py  Wie lange Aufnahmen bleiben
  storage.py       Upload-Teile und Hörproben unter data/
  worker_prozess.py, audio.py, transkription.py   Worker: Aufträge holen, Audio, WhisperX-Motor
  arbeitsweise.py  Modell und Stapelgröße nach Grafikspeicher
  zuordnung.py, stimmprofile.py                   Stimmen zuordnen, Stimmprofile
  sprachmodell.py, zusammenfassung.py             Recap und Vorschläge (Eingaben mit Spoilerschutz)
  belege.py        Zitate der Vorschläge im Transkript nachprüfen
  unterlagen.py    SL-Unterlagen auslesen und auswerten
  extern.py        Externe Transkription als Ausweichlösung
  modelle.py, modellablage.py                     Modelle in fester Fassung, Ablage auf dem Server
  konto.py, anmeldedienste.py, mail.py            Registrierung, Export, Löschen, OIDC, E-Mail
  verwaltung/      Weboberfläche /verwaltung (Seiten, Übersetzungen, Assistent)
  cli.py           Kommandozeile „chronik“
migrations/      Datenbank-Migrationen (Alembic)
tests/           pytest mit automatischer Vertragsprüfung
deploy/          Docker Compose, Caddy, Update-Skript
box/             Taleward-Box: Abbild für den Raspberry Pi
install.sh       Installation mit Docker
Dockerfile, Dockerfile.worker   Server- bzw. Worker-Abbild (Grafikkarte oder Prozessor)
engine-requirements.txt         feste Paketversionen des KI-Pakets für die Worker-App
```

## Regeln für Änderungen

**Schnittstelle.** `contract/session-chronik-api.yaml` ist verbindlich. Neue oder geänderte Endpunkte und Felder
kommen zuerst in die YAML, mit Eintrag unter „Änderungen in …“ und neuer `info.version`; `API_VERSION` in
`app/routers/auth.py` zieht mit. Ändern sich Felder, die die App nutzt, gehört das abgestimmt, bevor es gebaut wird.
Das Worker-Protokoll (`/worker/v1`) ist nicht Teil der YAML; Worker und Server passen über die gemeinsame Fassung
zusammen.

**Pflichtregeln** – sie gelten für jede Änderung und sind durch Tests abgesichert:

- **Spoilerschutz auf dem Server:** Einträge mit `visibility: gm_only`, Vorschläge, SL-Notizen und Transkripte
  erreichen Spielende nie, auch nicht als Anzahl oder in Listen. Für Spielende lieber 404 als 403, wenn schon die
  Existenz etwas verraten würde. SL-Notizen fließen nie in Recaps oder Vorschläge ein.
- **Einwilligung:** Ein Upload wird nur angenommen, wenn alle Anwesenden der Session eingewilligt haben.
- **Stimmprofile:** Gespeichert wird nur der Stimmabdruck, nie das Audio. Löschen entfernt alles daraus Gelernte.
- **CORS:** `http://localhost:5173` (Browser) und `http://localhost` (Android-App) bleiben erlaubt.

**Fehler** kommen immer als `{code, message}`. Neue Codes mit deutschem und englischem Text in `app/errors.py`.

**Datenbank.** Jede Änderung an `app/models.py` braucht eine Migration unter `migrations/versions/` (Alembic, SQLite:
`batch_alter_table`). Der Server migriert beim Start selbst.

**Verwaltung.** Deutsche Texte sind zugleich die Schlüssel für die Übersetzung: jeder neue Text braucht einen Eintrag
in `app/verwaltung/i18n.py`. Die Oberfläche kommt ohne Skripte aus fremden Quellen aus (strenge CSP); nötiges
JavaScript liegt als eigene Datei unter `app/verwaltung/static/`. Keine Schriften oder Dateien von fremden Servern.

**Plattform.** Der Servercode bleibt frei von Windows-Eigenheiten. Plattformabhängig ist höchstens die Verpackung
(Docker, Box, Installer).

**Sprache.** Oberfläche, Meldungen, Kommentare und Bezeichner sind überwiegend deutsch; Begriffe aus der Schnittstelle
bleiben englisch (`campaign`, `session`, `visibility`). Einheitliche Begriffe in Texten: Worker, Server, lokaler
Server, Spielleitung (SL).

**Abhängigkeiten.** Nach Änderungen an den KI-Abhängigkeiten `engine-requirements.txt` neu erzeugen:

```bash
uv export --format requirements-txt --extra ki --no-dev --no-hashes --no-emit-project --no-header -o engine-requirements.txt
```

## Fassungen und Veröffentlichung

- Die Fassung steht in `pyproject.toml`. Jede Änderung, die auf Server ankommen soll, erhöht sie (Tests, die die
  Fassung prüfen, ziehen mit).
- Der Ablauf **„Fassung markieren“** setzt nach jedem Push auf `main` das Tag `v<Fassung>`, falls es neu ist. Laufende
  Server aktualisieren sich nachts auf das neueste Tag; die Worker-App installiert ihr KI-Paket genau aus diesem Tag.
- Der Ablauf **„Taleward-Box“** baut danach das Raspberry-Pi-Abbild und veröffentlicht es als Release `box-v<Fassung>`.
