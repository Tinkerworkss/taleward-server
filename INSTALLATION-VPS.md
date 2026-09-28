# Taleward-Server mit Docker: gemieteter Server, zu Hause oder unter Windows 11

Der Server läuft dann rund um die Uhr im Internet. Die Transkription übernimmt ein **Worker** (dein PC mit
Grafikkarte, verbindet sich von selbst mit dem Server) oder die Cloud-API. Dauer: etwa 20 Minuten.

## Was du brauchst

| | |
|---|---|
| **VPS** | Ein *VPS*-Tarif, **kein** Webhosting-Tarif (dort läuft kein Docker). Bei Hostinger z. B. „KVM 1“ (4 GB RAM) – reicht, weil die KI-Arbeit nicht auf dem VPS passiert. Betriebssystem: **Ubuntu 24.04** (ohne vorinstallierte Anwendungen). |
| **Domain** | Z. B. `taleward.meinverein.de`. Beim Domain-Anbieter einen **A-Eintrag** auf die IP-Adresse des VPS anlegen (Hostinger: Domains → DNS-Einträge). |
| **Zugang** | Das Root-Passwort des VPS (legst du beim Einrichten des VPS fest). |

## 1. Installieren

Auf der VPS-Seite bei Hostinger **Browser-Terminal** öffnen (oder `ssh root@<IP>` vom eigenen PC) und einfügen:

```bash
curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-server/main/install.sh | sudo bash
```

Das Skript fragt nach der Adresse (Domain) und einer E-Mail für das HTTPS-Zertifikat. Dann installiert es Docker,
baut den Server (beim ersten Mal 3–5 Minuten) und zeigt am Ende:

```
Im Browser öffnen:  https://taleward.meinverein.de/verwaltung/einrichtung?code=ABCD-EFGH
```

**✅ Kontrolle:** Der Link öffnet im Browser die Ersteinrichtung, mit Schloss-Symbol in der Adresszeile.
Dort legst du das Konto für die Verwaltung an. Danach verbindest du in der App den Server mit
`https://taleward.meinverein.de`.

## Zu Hause, im Verein oder unter Windows 11

Derselbe Befehl funktioniert auch auf einem eigenen Rechner mit Ubuntu oder Debian, etwa einem Mini-PC im
Vereinsheim, und unter **Windows 11 mit Ubuntu** (WSL). Das Skript fragt, wie Handys den Server erreichen:

| Auswahl | Wann | Was nötig ist |
|---|---|---|
| **1 – Internet mit Domain und HTTPS** | VPS, oder zu Hause mit fester Erreichbarkeit | Domain bzw. DynDNS, **Portfreigabe TCP 80 und 443** im Router auf diesen Rechner. Anschlüsse mit DS-Lite oder Mobilfunk können das oft nicht. |
| **2 – Nur im Heimnetz** | Spielen immer am selben Ort (Vereinsheim, Wohnzimmer) | Nichts weiter. Adresse `http://<IP>:8000`. Anmelden mit Google & Co. und die Web-App gehen ohne HTTPS nicht. |

**Unter Windows 11** erledigt das Skript zusätzlich:

1. Es stellt Ubuntu so ein, dass es unter der IP-Adresse des PCs erreichbar ist („gespiegeltes Netz“) und Docker
   läuft (systemd). Danach heißt es einmal: Fenster schließen, in der PowerShell `wsl --shutdown`, Ubuntu wieder
   öffnen und den Befehl noch einmal ausführen.
2. Es gibt die Ports in der Windows-Firewall frei. Windows fragt dafür einmal nach Administratorrechten.
3. Es startet Taleward mit Windows (Autostart) und lässt es im Hintergrund laufen, auch ohne offenes Fenster.
4. Auf Wunsch schaltet es den Ruhezustand am Netzstrom aus. Im Ruhezustand ist der Server nicht erreichbar.

Voraussetzung ist Windows 11 ab Version 22H2. Unter Windows 10 lässt sich Ubuntu nicht für andere Geräte öffnen.
Der Worker (Grafikkarte) kann auf demselben PC laufen: Worker-App installieren und mit der Adresse des Servers
koppeln.

Tipp: Im Router für diesen Rechner immer dieselbe IP-Adresse vergeben (FRITZ!Box: Heimnetz → Netzwerk → Gerät
bearbeiten), sonst ändert sich die Adresse für die App.

## 2. Deinen PC als Worker verbinden

Auf dem PC die **Worker-App** installieren ([worker-app/README.md](worker-app/README.md), Windows oder Linux). In der
Verwaltung **Transkription → Weiteren Worker anbinden → Kopplungscode erzeugen** und Adresse und Code in der App
eintragen. Der PC braucht keine
Portfreigabe im Router – er fragt beim Server nach Arbeit. Ist er aus, warten die Aufnahmen, bis er wieder läuft
(auf Wunsch meldet der Server das, Teil 14).

## Alltag

Alle Befehle im Ordner `/opt/taleward`:

| Was | Befehl |
|---|---|
| Protokoll ansehen | `sudo docker compose logs -f server` |
| Update | läuft **automatisch** nachts (3–5 Uhr) mit Sicherung vorher; abschaltbar unter Verwaltung → Updates |
| Update sofort | Verwaltung → Updates → „Jetzt aktualisieren“ oder `sudo ./aktualisieren.sh --jetzt` |
| Bestimmte Version | in `.env` `TALEWARD_VERSION=v0.4.3` eintragen, `sudo docker compose build && sudo docker compose up -d` |
| Neu starten | `sudo docker compose restart` |
| Einrichtungscode neu | `sudo docker compose exec server chronik einrichtungscode` |
| Andere Domain | `install.sh` nochmal ausführen – Daten bleiben |

Das automatische Update legt vorher selbst eine Sicherung an. Startet die neue Fassung nicht, stellt es die alte
Fassung und die Daten von vorher wieder her und meldet das (Benachrichtigung, Verwaltung → Updates). Die Datenbank
wird beim Start selbst auf den neuen Stand gebracht.

## Sicherung

Alle Daten liegen in `/opt/taleward/daten`. Die nächtlichen Sicherungen landen in
`/opt/taleward/daten/sicherungen`, also **auf demselben VPS**. Lade deshalb regelmäßig eine Sicherung herunter
(Verwaltung → Übersicht → Sicherung) oder schalte bei Hostinger die automatischen VPS-Backups ein.

**Wiederherstellen:**

```bash
cd /opt/taleward
sudo docker compose stop server
sudo docker compose run --rm server chronik wiederherstellen /data/sicherungen/taleward-sicherung-….zip
sudo docker compose start server
```

Eine heruntergeladene Sicherung vorher nach `/opt/taleward/daten/sicherungen/` kopieren
(z. B. über das Datei-Hochladen im Hostinger-Terminal oder `scp`).

## Wenn etwas nicht klappt

| Problem | Lösung |
|---|---|
| „zeigt auf nichts“ / Zertifikatsfehler | A-Eintrag fehlt oder wirkt noch nicht. Warten, dann `sudo docker compose restart caddy`. |
| Seite lädt nicht, Port 80/443 zu | In Hostinger unter VPS → Firewall die Ports 80 und 443 freigeben. |
| 404 auf jeder Seite, Caddy ohne Zertifikat | Ein anderer Webserver (z. B. Traefik aus der Hostinger-Vorlage „Ubuntu mit Docker“) hält Port 80/443. `install.sh` nochmal starten – es erkennt das und bietet an, ihn abzuschalten. |
| Caddy-Protokoll: `lookup … 127.0.0.53 … connection refused` | Docker reicht einen DNS-Dienst durch, den die Container nicht erreichen. `install.sh` nochmal starten – es legt dann `docker-compose.override.yml` mit Quad9 an. |
| `permission denied` im Protokoll | `sudo chown -R 1000:1000 /opt/taleward/daten` |
| Worker verbindet sich nicht | Die Adresse mit `https://` angeben, genau wie in der App. |
