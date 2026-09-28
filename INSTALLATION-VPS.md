# Taleward-Server auf einem gemieteten Server (VPS, z. B. Hostinger)

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
| Update | `sudo docker compose build --pull && sudo docker compose up -d` |
| Bestimmte Version | in `.env` `TALEWARD_VERSION=v0.4.0` eintragen, dann Update-Befehl |
| Neu starten | `sudo docker compose restart` |
| Einrichtungscode neu | `sudo docker compose exec server chronik einrichtungscode` |
| Andere Domain | `install.sh` nochmal ausführen – Daten bleiben |

Vor einem Update legt die Verwaltung auf Wunsch eine Sicherung an (Übersicht → Sicherung). Die Datenbank wird
beim Start selbst auf den neuen Stand gebracht.

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
| `permission denied` im Protokoll | `sudo chown -R 1000:1000 /opt/taleward/daten` |
| Worker verbindet sich nicht | Die Adresse mit `https://` angeben, genau wie in der App. |
