<p align="center"><img src="app/verwaltung/static/marke/taleward-lockup.svg" alt="Taleward" width="280"></p>

<p align="center"><em>Your table's story, kept safe.</em></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-17313B" alt="License: AGPL-3.0"></a>
  <a href="contract/session-chronik-api.yaml"><img src="https://img.shields.io/badge/API-OpenAPI%203.1-9E2A3A" alt="API: OpenAPI 3.1"></a>
</p>

**Taleward** records your tabletop session, turns it into a recap you can read aloud, and keeps a campaign bible that
separates what the characters know from what only the game master knows. This repository is the self-hosted server.
The documentation is in German; the API contract and the code comments are, too.

**Taleward** nimmt eure Pen-&-Paper-Runde auf, macht daraus einen Recap zum Vorlesen und pflegt die Bibel der
Kampagne – getrennt nach Spielerwissen und den Geheimnissen der Spielleitung. Dies ist der Server zum Selbstbetreiben,
für Vereine, Läden und private Runden.

## Was Taleward ausmacht

- **Wer weiß was:** Der Server setzt den Spoilerschutz durch, nicht die App. Geheime Bibeleinträge, Vorschläge,
  SL-Notizen und Transkripte erreichen Spielende nie – auch nicht als Anzahl.
- **Einwilligung zuerst:** Aufgenommen wird nur, wenn alle Anwesenden selbst zugestimmt haben. Aufnahmen bleiben nur
  bis zur Freigabe des Recaps (höchstens 7 Tage) oder werden gleich nach der Umwandlung in Text gelöscht.
- **Die Daten bleiben im Verein:** Transkription und Zusammenfassung können komplett auf eigener Hardware laufen.
  Cloud-Dienste sind optional und müssen ausdrücklich freigeschaltet werden.
- **Für die ganze Runde:** Spielende zahlen nie. Kosten entstehen höchstens bei der Verarbeitung.

## Bestandteile

| Teil | Aufgabe | Repository |
|---|---|---|
| **Server** | Konten, Kampagnen, Warteschlange, Spoilerschutz, Verwaltung im Browser | dieses |
| **Worker** | Transkription und Sprechertrennung auf einem PC mit Grafikkarte (oder dem Prozessor) | [taleward-worker](https://github.com/Tinkerworkss/taleward-worker) |
| **App** | Aufnehmen, Stimmen zuordnen, Recap prüfen, Chronik und Bibel lesen (Android und Browser) | [taleward-app](https://github.com/Tinkerworkss/taleward-app) |

Der Server rechnet selbst nichts Aufwendiges. Worker holen sich Aufträge beim Server ab – sie brauchen keinen offenen
Port und können irgendwo stehen, wo eine Grafikkarte ist.

## Installation

Ein Befehl auf einem Linux-Server (gemieteter Server, eigener PC, Windows 11 mit Ubuntu):

```bash
curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/install.sh | sudo bash
```

Das Skript installiert Docker, fragt nach Domain oder Heimnetz, erkennt eine Grafikkarte und zeigt am Ende den Link
zur Ersteinrichtung. Alles Weitere in **[INSTALLATION.md](INSTALLATION.md)** – dort steht auch die **Taleward-Box**, ein
fertiges Speicherkarten-Abbild für den Raspberry Pi.

## Dokumentation

| Dokument | Für wen |
|---|---|
| [INSTALLATION.md](INSTALLATION.md) | Server mit Docker einrichten, Worker verbinden, Updates, Sicherung, Fehlerhilfe |
| [docs/BETRIEB.md](docs/BETRIEB.md) | Die Verwaltung im Alltag: Transkription, Zusammenfassung, Stimmprofile, Anmeldung, Benachrichtigungen |
| [docs/ENTWICKLUNG.md](docs/ENTWICKLUNG.md) | Entwicklungsumgebung, Tests, Kommandozeile, Arbeitsweise im Repository |
| [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md) | Aufbau, Datenfluss und die Regeln, auf denen alles ruht |
| [contract/session-chronik-api.yaml](contract/session-chronik-api.yaml) | Die Schnittstelle zwischen App und Server (OpenAPI 3.1) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Mitmachen |

## Schnellstart für Entwicklung

```bash
git clone https://github.com/Tinkerworkss/taleward-server && cd taleward-server
uv sync                        # Python 3.11 und Abhängigkeiten
uv run pytest                  # alle Tests, ohne Grafikkarte
uv run chronik serve           # Server auf http://localhost:8000
uv run chronik worker --testmodus   # zweites Terminal: Worker mit Platzhaltertext statt KI
```

Die Ersteinrichtung öffnet der Link, den der Server beim ersten Start ins Protokoll schreibt. Details in
[docs/ENTWICKLUNG.md](docs/ENTWICKLUNG.md).

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
