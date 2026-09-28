# Taleward Worker (App für Windows und Linux)

Die Worker-App verarbeitet Aufnahmen für einen Taleward-Server auf einem PC mit NVIDIA-Grafikkarte. Der Server kann
irgendwo laufen (z. B. auf einem gemieteten VPS). Der PC verbindet sich von selbst mit ihm, im Router muss nichts
freigegeben werden.

- **Einrichtung in vier Schritten:** Adresse des Servers und Kopplungscode eingeben (aus der Verwaltung:
  Transkription → Weiteren Worker anbinden). Die App prüft dann Grafikkarte, Treiber und Speicherplatz und lädt das
  KI-Paket, einmalig etwa 4 GB. Beim ersten Auftrag kommen die Sprachmodelle dazu (etwa 5 GB).
- **Immer passend zum Server:** Die App installiert das KI-Paket in genau der Fassung des Servers (Git-Tag
  `v<Fassung>`). Nach einem Server-Update zieht sie selbst nach.
- **Updates über den eigenen Server:** Die App fragt nur ihren Taleward-Server nach einer neuen Fassung, nie GitHub.
  Der Server lädt sie einmal am Tag von GitHub und gibt sie frei (Verwaltung → Updates). Unter Windows installiert
  die App sie still, sobald sie nichts zu tun hat, und startet neu. Unter Linux läuft dafür `uv tool install`.
  Abschaltbar unter Einstellungen → „Updates automatisch installieren“.
- **Im Hintergrund:** Auf Wunsch startet die App mit dem Computer und läuft mit einem Symbol im Infobereich. Den
  Autostart wählst du bei der Installation und kannst ihn später in den Einstellungen ändern.
- **Passend zur Hardware:** Die App wählt Modell, Stapelgröße und Gerät selbst aus, je nach Grafikspeicher.
  - Ab 6 GB: `large-v3` komplett auf der Grafikkarte.
  - 3,5 bis 6 GB: `large-v3` mit kleinerem Stapel oder `large-v3-turbo`.
  - 2 bis 3,5 GB: Ausrichtung und Sprechertrennung laufen auf dem Prozessor.
  - Ohne nutzbare NVIDIA-Karte: alles auf dem Prozessor. Das ist langsam, grob 4–10 Stunden für 4 Stunden Aufnahme, und installiert eine kleinere Prozessor-Fassung des KI-Pakets.
- **Schieberegler „Grafikspeicher für Taleward“:** Er legt fest, wie viel Grafikspeicher der Worker höchstens nutzt, der Rest bleibt für Spiele. Die App passt das Modell daran an und begrenzt PyTorch auf diesen Anteil. Nach jedem Auftrag zeigt sie Dauer und höchsten Grafikspeicher als gemessene Werte an. Die Zeiten vorher sind grobe Schätzungen.
- Während eines Auftrags geht der PC nicht in den Ruhezustand.
- Die Aufnahmen werden nur im Arbeitsordner verarbeitet und danach gelöscht.

**Geplant:** Ein zweiter Motor auf Basis von whisper.cpp mit Vulkan für AMD- und Intel-Grafikkarten. Er kommt erst
als „Vorschau“ und wird fertig, sobald jemand mit so einer Karte testen kann.

## Windows

`TalewardWorker-Setup.exe` von der [Release-Seite](https://github.com/Tinkerworkss/taleward-server/releases/latest)
herunterladen und starten. Administratorrechte sind nicht nötig. Voraussetzungen:

- Windows 10 (ab 1809) oder 11
- am besten eine NVIDIA-Grafikkarte (ab 2 GB, gut ab 6 GB) mit aktuellem Treiber (ab 525), sonst arbeitet der Prozessor
- mindestens 8 GB Arbeitsspeicher
- etwa 14 GB freier Speicher

Die App liegt unter `%LOCALAPPDATA%\Programs\Taleward Worker`. KI-Paket, Modelle, Einstellungen und Protokoll liegen
unter `%LOCALAPPDATA%\Taleward Worker`. Beim Deinstallieren wirst du gefragt, ob diese Daten auch weg sollen.

## Linux

Für Ubuntu, Debian, Fedora und Ähnliche (x86_64). Ohne `sudo` ausführen:

```bash
curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/worker-app/install-linux.sh | bash
```

Das Skript installiert [uv](https://docs.astral.sh/uv/) und die App mit eigenem Fenster (Qt). Es legt einen Eintrag im
Anwendungsmenü an und fragt nach dem Autostart. Nochmal ausführen aktualisiert die App. Daten liegen unter
`~/.local/share/taleward-worker`.

Hinweis: Unter GNOME gibt es ohne die Erweiterung „AppIndicator“ kein Symbol im Infobereich. Das Fenster wird dann
beim Schließen nur minimiert.

## Entwicklung

```bash
cd worker-app
uv sync --extra qt          # Linux: Fenster über Qt; Windows nutzt WebView2
uv run pytest
TALEWARD_MOTOR=../.venv/bin/python uv run python -m taleward_worker   # vorhandene Server-Umgebung als KI-Paket
```

- Die Oberfläche liegt in `taleward_worker/ui/` (HTML/CSS/JS, Texte Deutsch/Englisch in `texte.js`).
- Die App startet den Worker als `python -m app.cli worker --app`. Ereignisse kommen als JSON-Zeilen, die Steuerung
  (`pause`, `weiter`, `stopp`) geht über stdin, siehe `app/worker_app.py`.
- `TALEWARD_MOTOR_QUELLE=<Ordner des Server-Codes>` installiert das KI-Paket aus einem lokalen Ordner statt von GitHub.
- `TALEWARD_WORKER_HOME` legt einen anderen Datenordner fest, z. B. für einen zweiten Worker auf demselben PC.

**Neue Fassung veröffentlichen:** Auf GitHub ein Release mit Tag `worker-v0.2.0` anlegen. Der Ablauf „Worker-App“ baut
dann den Windows-Installer und hängt ihn an. Die Server holen sie sich innerhalb eines Tages, und die Worker aktualisieren sich darüber selbst.
