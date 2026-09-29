# Mitmachen

Schön, dass du an Taleward mitarbeiten willst. Fehler, Ideen und Fragen gern als
[Issue](https://github.com/Tinkerworkss/taleward-server/issues); Code als Pull Request.

## Bevor du loslegst

- **Umgebung und Tests:** [docs/ENTWICKLUNG.md](docs/ENTWICKLUNG.md) – `uv sync`, `uv run pytest`, fertig.
- **Aufbau und Regeln:** [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md). Die Pflichtregeln zu Spoilerschutz,
  Einwilligung und Stimmprofilen sind nicht verhandelbar – Änderungen daran lehnen wir ab.
- **Größere Vorhaben** bitte vorher in einem Issue beschreiben, besonders wenn sie die Schnittstelle zur App betreffen.

## Pull Requests

- Alle Tests laufen durch (`uv run pytest`). Neue Funktionen bringen eigene Tests mit.
- Änderungen an der App-Schnittstelle stehen zuerst in `contract/session-chronik-api.yaml`, mit Eintrag unter
  „Änderungen in …“. Die Tests prüfen jede Antwort gegen diese Datei.
- Neue Tabellen oder Spalten kommen mit einer Migration unter `migrations/versions/`.
- Neue Texte der Verwaltung und neue Fehlermeldungen gibt es auf Deutsch und Englisch.
- Kein Code, der nur unter Windows läuft; keine Dateien oder Schriften von fremden Servern in der Oberfläche.
- Ein Thema pro Pull Request, mit kurzer Beschreibung, was sich für Betreiber oder Nutzende ändert.

## Sicherheitslücken

Bitte nicht als öffentliches Issue melden, sondern über
[GitHub Security Advisories](https://github.com/Tinkerworkss/taleward-server/security/advisories/new).

## Lizenz

Mit einem Beitrag stimmst du zu, dass er unter der [AGPL-3.0](LICENSE) veröffentlicht wird.
