# Taleward-Server installieren

Taleward läuft in Docker auf einem Linux-Rechner: einem gemieteten Server (VPS), einem eigenen PC oder Mini-PC, unter
Windows 11 mit Ubuntu oder als fertige **Taleward-Box** auf einem Raspberry Pi. Die Transkription übernimmt ein
**Worker** – ein PC mit Grafikkarte, der sich von selbst beim Server meldet – oder auf Wunsch ein Cloud-Dienst.

Dauer: etwa 20 Minuten, meist Warten auf Downloads.

**Inhalt**

1. [Was du brauchst](#was-du-brauchst)
2. [Installieren](#installieren)
3. [Zu Hause, im Verein oder unter Windows 11](#zu-hause-im-verein-oder-unter-windows-11)
4. [Taleward-Box: Raspberry Pi ohne Terminal](#taleward-box-raspberry-pi-ohne-terminal)
5. [Einen PC als Worker verbinden](#einen-pc-als-worker-verbinden)
6. [Worker auf dem Server selbst](#worker-auf-dem-server-selbst)
7. [Alltag und Updates](#alltag-und-updates)
8. [Sicherung](#sicherung)
9. [Wenn etwas nicht klappt](#wenn-etwas-nicht-klappt)

Wie es nach der Installation weitergeht – Transkription, Zusammenfassung, Anmeldung, Benachrichtigungen –, steht in
[docs/BETRIEB.md](docs/BETRIEB.md).

## Was du brauchst

| | |
|---|---|
| **Rechner** | Ein Linux-Server mit **Ubuntu 24.04** oder Debian, 4 GB Arbeitsspeicher empfohlen. Bei einem Mietserver: ein *VPS*-Tarif, **kein** Webhosting-Tarif – dort läuft kein Docker. Die KI-Arbeit passiert nicht auf dem Server, ein kleiner Tarif reicht. |
| **Adresse** | Für den Zugriff aus dem Internet eine Domain, z. B. `taleward.meinverein.de`, mit einem **A-Eintrag** auf die IP-Adresse des Servers (beim Domain-Anbieter unter „DNS“). Für den Betrieb nur im Heimnetz ist keine Domain nötig. |
| **Zugang** | Ein Terminal mit Root-Rechten: `ssh root@<IP>` vom eigenen PC oder das Browser-Terminal des Anbieters. |

## Installieren

Im Terminal des Servers einfügen:

```bash
curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/install.sh | sudo bash
```

Das Skript

- installiert Docker, falls es fehlt,
- fragt, wie Handys den Server erreichen (Domain mit HTTPS oder nur Heimnetz),
- fragt bei einer Domain nach einer E-Mail-Adresse für das HTTPS-Zertifikat,
- erkennt eine Grafikkarte und bietet an, den Worker gleich mitlaufen zu lassen ([mehr dazu](#worker-auf-dem-server-selbst)),
- baut den Server (beim ersten Mal 3–5 Minuten) und zeigt am Ende:

```
Im Browser öffnen:  https://taleward.meinverein.de/verwaltung/einrichtung?code=ABCD-EFGH
```

**✅ Kontrolle:** Der Link öffnet die Ersteinrichtung, mit Schloss-Symbol in der Adresszeile. Dort legst du das Konto
für die Verwaltung an; danach führt ein Assistent durch Verein, Betriebsart, Zusammenfassung, Datenschutzhinweis,
Sicherung und die erste Spielleitung. In der App verbindest du dich anschließend mit `https://taleward.meinverein.de`.

Das Skript lässt sich jederzeit noch einmal ausführen – zum Beispiel für eine andere Domain oder um den Worker ein- oder
auszuschalten. Daten und Einstellungen bleiben dabei erhalten.

## Zu Hause, im Verein oder unter Windows 11

Derselbe Befehl funktioniert auf einem eigenen PC mit Ubuntu oder Debian, etwa einem Mini-PC im Vereinsheim, und unter
**Windows 11 mit Ubuntu** (WSL). Das Skript fragt, wie Handys den Server erreichen:

| Auswahl | Wann | Was nötig ist |
|---|---|---|
| **1 – Internet mit Domain und HTTPS** | Mietserver, oder zu Hause mit fester Erreichbarkeit | Domain bzw. DynDNS und eine **Portfreigabe TCP 80 und 443** im Router auf diesen Rechner. Anschlüsse mit DS-Lite oder Mobilfunk können das oft nicht. |
| **2 – Nur im Heimnetz** | Gespielt wird immer am selben Ort | Nichts weiter. Adresse `http://<IP>:8000`. Anmelden mit Google & Co. und die Web-App brauchen HTTPS und gehen dann nicht. |

**Unter Windows 11** erledigt das Skript zusätzlich:

1. Es stellt Ubuntu so ein, dass es unter der IP-Adresse des PCs erreichbar ist („gespiegeltes Netz“) und Docker
   läuft (systemd). Danach heißt es einmal: Fenster schließen, in der PowerShell `wsl --shutdown`, Ubuntu wieder
   öffnen und den Befehl noch einmal ausführen.
2. Es gibt die Ports in der Windows-Firewall frei. Windows fragt dafür einmal nach Administratorrechten.
3. Es startet Taleward mit Windows und lässt es im Hintergrund laufen, auch ohne offenes Fenster.
4. Auf Wunsch schaltet es den Ruhezustand am Netzstrom aus – im Ruhezustand ist der Server nicht erreichbar.

Voraussetzung ist Windows 11 ab Version 22H2; unter Windows 10 lässt sich Ubuntu nicht für andere Geräte öffnen. Ubuntu
selbst installierst du vorher in der PowerShell (als Administrator) mit `wsl --install -d Ubuntu-24.04` und einem
Neustart. Der Worker kann auf demselben PC laufen: Worker-App installieren und mit der Adresse des Servers koppeln.

**Tipp:** Im Router für diesen Rechner immer dieselbe IP-Adresse vergeben (bei einer FRITZ!Box: Heimnetz → Netzwerk →
Gerät bearbeiten), sonst ändert sich die Adresse für die App.

## Taleward-Box: Raspberry Pi ohne Terminal

Für einen Verein oder eine Runde, die einfach ein kleines Kästchen ins Netz stecken will: ein fertiges
Speicherkarten-Abbild mit Raspberry Pi OS, Docker und dem Taleward-Server. Kein Terminal, keine Befehle.

- **Du brauchst:** einen Raspberry Pi 4 oder 5 mit 4–8 GB, eine Speicherkarte ab 16 GB (besser 32 GB) oder eine SSD,
  ein Netzteil und ein Netzwerkkabel oder WLAN.
- **Abbild:** auf GitHub unter [Releases](https://github.com/Tinkerworkss/taleward-server/releases) → „Taleward-Box v…“
  → `Taleward-Box-v….img.xz`

So geht's:

1. **Raspberry Pi Imager** öffnen, Gerät wählen, bei „Betriebssystem“ ganz unten **„Eigenes Abbild“** und die
   heruntergeladene Datei wählen. Dann die Speicherkarte wählen.
2. Bei **„Einstellungen anpassen“** als Name `taleward` eintragen, Benutzer und Passwort vergeben, WLAN nur, wenn die
   Box nicht am Kabel hängt.
3. Karte in den Pi, Netz und Strom anschließen. Beim ersten Start richtet sich die Box selbst ein, das dauert einige
   Minuten.
4. Im Browser **`http://taleward.local:8000`** öffnen. Klappt das nicht, die IP-Adresse der Box im Router nachsehen
   und `http://<IP>:8000` öffnen.
5. **Die Ersteinrichtung geht ohne Einrichtungscode**, aber nur aus dem eigenen Netz und nur einmal. Mach sie also
   gleich nach dem ersten Start. In der App trägst du als Server `http://<IP>:8000` ein.

Gut zu wissen:

- **Transkription:** Die Box selbst transkribiert nicht, dafür ist der Pi zu schwach. Das macht ein PC im selben Netz
  mit der Worker-App. Mit „Recaps auch auf diesem PC schreiben“ bleibt alles im Verein, ganz ohne Cloud.
- **Feste IP-Adresse:** Im Router der Box immer dieselbe IP-Adresse geben.
- **Updates:** Mit Internet holt sich die Box neue Fassungen nachts selbst, wie jeder Taleward-Server.
- **Ohne Internet** läuft die Box ganz normal; nur Updates kommen dann nicht.
- **Protokoll des ersten Starts:** `/var/log/taleward-erststart.log`, falls du doch einmal per SSH nachsehen willst.

## Einen PC als Worker verbinden

Auf dem PC mit Grafikkarte die **Worker-App** installieren
([taleward-worker](https://github.com/Tinkerworkss/taleward-worker), Windows oder Linux). In der Verwaltung
**Transkription → Weiteren Worker anbinden → Kopplungscode erzeugen** und Adresse und Code in der App eintragen.

Der PC braucht keine Portfreigabe im Router – er fragt beim Server nach Arbeit. Ist er aus, warten die Aufnahmen, bis
er wieder läuft; auf Wunsch meldet der Server das ([Benachrichtigungen](docs/BETRIEB.md#benachrichtigungen)).

## Worker auf dem Server selbst

Hat der Rechner, auf dem Taleward läuft, eine **NVIDIA-Grafikkarte**, kann der Worker gleich mit darauf laufen – als
eigener Container, ohne Worker-App und ohne Kopplungscode. Das passt für einen gemieteten Server mit Grafikkarte oder
einen PC mit Linux (auch Ubuntu unter Windows 11). Ohne Grafikkarte geht es auch mit dem Prozessor, dann aber langsam:
grob 4–10 Stunden für 4 Stunden Aufnahme auf einem Desktop-Prozessor, auf einem kleinen Mietserver eher einen Tag.

**Am einfachsten mit `install.sh`:**

- Das Skript erkennt eine NVIDIA-Karte und fragt, ob der Worker mitlaufen soll.
- Fehlt der Treiber, installiert es ihn. Danach muss der Rechner einmal neu starten, und du führst den Befehl noch
  einmal aus.
- Das nvidia-container-toolkit richtet es ebenfalls selbst ein.
- Ohne passende Grafikkarte, aber mit mindestens 8 GB Arbeitsspeicher und 4 Kernen, bietet es die Prozessor-Variante
  an.

<details>
<summary>Von Hand einrichten</summary>

**1. NVIDIA-Treiber** (unter Windows 11 mit Ubuntu kommt er von Windows, dort überspringen):

```bash
sudo ubuntu-drivers install && sudo reboot
nvidia-smi          # nach dem Neustart: zeigt die Karte
```

**2. Grafikkarte für Docker freigeben** (nvidia-container-toolkit):

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

**3. Worker einschalten:**

```bash
cd /opt/taleward
echo "COMPOSE_PROFILES=worker" | sudo tee -a .env      # nur Prozessor: COMPOSE_PROFILES=worker-cpu
sudo docker compose up -d --build                       # beim ersten Mal 10–20 Minuten, etwa 8 GB
sudo docker compose logs -f worker                      # „Worker bereit, warte auf Aufträge …“
```

</details>

Der Server legt beim Start selbst einen Schlüssel für den Worker an; in der Verwaltung erscheint er unter
**Transkription** als „Lokaler Worker“. Den Zugang zum Sprechermodell trägst du wie gewohnt im Assistenten ein. Updates
bekommt der Worker zusammen mit dem Server, immer in derselben Fassung.

**Einstellungen** in der Verwaltung unter **Transkription → Eingebauter Worker**: Grafikspeicher für Taleward,
Sprachmodell, „Nur den Prozessor verwenden“, Pausieren und Neu starten. Die Karte zeigt außerdem den Zustand und die
Messwerte des letzten Auftrags. Neue Werte übernimmt der Worker, sobald er keinen Auftrag bearbeitet; er startet dafür
kurz neu.

Was dort nicht gesetzt ist, kommt aus der `.env` (danach `sudo docker compose up -d`):

| Einstellung | Bedeutung |
|---|---|
| `TALEWARD_WORKER_VRAM_MB=6000` | höchstens so viel Grafikspeicher (Standard 0 = alles) |
| `TALEWARD_WORKER_MODELL=large-v3` | festes Modell statt `auto` (auch `large-v3-turbo`) |
| `TALEWARD_WORKER_CPUS=4` | nur `worker-cpu`: so viele Rechenkerne (`install.sh` trägt die Hälfte ein, sonst 2), der Rest bleibt für den Server |

### Recaps auf eigener Hardware (Ollama)

Auf Wunsch läuft neben dem Worker ein zweiter Container mit Ollama. Dann schreibt der Worker auch die Recaps, ganz ohne
Cloud.

- `install.sh` fragt danach, sobald der Worker gewählt ist. Von Hand: `COMPOSE_PROFILES=worker,ollama`
  (bzw. `worker-cpu,ollama-cpu`) in die `.env`, dann `sudo docker compose up -d`.
- In der Verwaltung unter **Zusammenfassung** „Lokales Modell“ wählen.
- Das Sprachmodell (`ministral-3:8b`, etwa 5 GB) lädt der Worker beim ersten Recap.
- Transkription und Recap wechseln sich auf der Grafikkarte ab. Reicht der Grafikspeicher nicht für beides, rechnet
  Ollama teilweise mit dem Prozessor – langsamer, aber es geht. Nur mit dem Prozessor dauert ein Recap grob eine Stunde
  oder mehr.
- Das Ollama-Abbild frischt das automatische Update mit auf; eine feste Fassung legt `TALEWARD_OLLAMA_VERSION=…` in der
  `.env` fest.

Wieder ausschalten: `sudo docker compose rm -sf worker ollama` (bzw. `worker-cpu ollama-cpu`), danach die Zeile
`COMPOSE_PROFILES=…` aus der `.env` löschen.

## Alltag und Updates

Alle Befehle im Ordner `/opt/taleward`:

| Was | Befehl |
|---|---|
| Protokoll ansehen | `sudo docker compose logs -f server` |
| Update | läuft **automatisch** nachts (3–5 Uhr) mit Sicherung vorher; abschaltbar unter Verwaltung → Updates |
| Update sofort | Verwaltung → Updates → „Jetzt aktualisieren“ oder `sudo ./aktualisieren.sh --jetzt` |
| Bestimmte Fassung | in `.env` `TALEWARD_VERSION=v0.4.12` eintragen, dann `sudo docker compose build && sudo docker compose up -d` |
| Neu starten | `sudo docker compose restart` |
| Einrichtungscode neu | `sudo docker compose exec server chronik einrichtungscode` |
| Andere Domain | `install.sh` noch einmal ausführen – Daten bleiben |

Das automatische Update legt vorher selbst eine Sicherung an. Startet die neue Fassung nicht, stellt es die alte
Fassung und die Daten von vorher wieder her und meldet das (Benachrichtigung, Verwaltung → Updates). Die Datenbank wird
beim Start selbst auf den neuen Stand gebracht.

App und Worker-App holen ihre Updates ebenfalls über diesen Server – mehr dazu in
[docs/BETRIEB.md](docs/BETRIEB.md#updates-für-app-und-worker).

## Sicherung

Alle Daten liegen in `/opt/taleward/daten`. Die nächtlichen Sicherungen landen in `/opt/taleward/daten/sicherungen`,
also **auf demselben Rechner**. Lade deshalb regelmäßig eine Sicherung herunter (Verwaltung → Übersicht → Sicherung),
trage einen zusätzlichen Sicherungsordner ein oder nutze die Server-Sicherung deines Anbieters.

**Wiederherstellen:**

```bash
cd /opt/taleward
sudo docker compose stop server
sudo docker compose run --rm server chronik wiederherstellen /data/sicherungen/taleward-sicherung-….zip
sudo docker compose start server
```

Eine heruntergeladene Sicherung vorher nach `/opt/taleward/daten/sicherungen/` kopieren, z. B. mit `scp`. Der
jetzige Stand wird vor dem Wiederherstellen selbst gesichert.

## Wenn etwas nicht klappt

| Problem | Lösung |
|---|---|
| „zeigt auf nichts“ / Zertifikatsfehler | Der A-Eintrag fehlt oder wirkt noch nicht. Warten, dann `sudo docker compose restart caddy`. |
| Seite lädt nicht, Port 80/443 zu | In der Firewall des Anbieters bzw. im Router die Ports 80 und 443 freigeben. |
| 404 auf jeder Seite, Caddy ohne Zertifikat | Ein anderer Webserver (z. B. Traefik aus einer vorinstallierten Docker-Vorlage) belegt Port 80/443. `install.sh` noch einmal starten – es erkennt das und bietet an, ihn abzuschalten. |
| Caddy-Protokoll: `lookup … 127.0.0.53 … connection refused` | Docker reicht einen DNS-Dienst durch, den die Container nicht erreichen. `install.sh` noch einmal starten – es legt dann `docker-compose.override.yml` mit einem öffentlichen DNS-Dienst an. |
| `permission denied` im Protokoll | `sudo chown -R 1000:1000 /opt/taleward/daten` |
| Worker verbindet sich nicht | Die Adresse mit `https://` angeben, genau wie in der App. |
| „Docker sieht die Grafikkarte nicht“ | nvidia-container-toolkit fehlt oder Docker wurde danach nicht neu gestartet – siehe [Worker auf dem Server selbst](#worker-auf-dem-server-selbst). |
| Heimnetz: Handy erreicht den Server nicht | Handy im selben WLAN (kein Gäste-WLAN)? Unter Windows: nach der ersten Ausführung `wsl --shutdown` gemacht und den Befehl wiederholt? |
