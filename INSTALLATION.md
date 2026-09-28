# Installation des Session-Chronik-Servers

Schritt-für-Schritt-Anleitung für Windows 11 mit NVIDIA-Grafikkarte.
Für einen gemieteten Server im Internet (VPS, z. B. Hostinger) gibt es eine kürzere Anleitung mit Docker:
[INSTALLATION-VPS.md](INSTALLATION-VPS.md).
Dauer: etwa 30–45 Minuten für Teil 1–4, dazu etwa 30 Minuten für den Probelauf (Teil 5), davon viel Warten auf Downloads.

**So liest du die Anleitung:**

- Jeder Teil endet mit **✅ Kontrolle**. Geh erst weiter, wenn die Kontrolle klappt.
- Kästen mit Befehlen kopierst du komplett und fügst sie ein.
  - In PowerShell: Strg+V
  - In Ubuntu: Rechtsklick oder Strg+Umschalt+V. Strg+V geht dort nicht.
- Klappt etwas nicht, schau in die Tabelle **„Wenn etwas nicht klappt“** am Ende. Oder schick mir einen Screenshot oder die Ausgabe.

**Zwei Fenster, die du brauchst:**

| Fenster | So öffnest du es | Woran du es erkennst |
|---|---|---|
| **PowerShell (Administrator)** | Startmenü → „PowerShell“ eintippen → Rechtsklick → **Als Administrator ausführen** | Titel „Administrator: Windows PowerShell“, Zeile beginnt mit `PS C:\…>` |
| **Ubuntu** | Startmenü → „Ubuntu“ | Zeile endet mit `$`, z. B. `benjamin@PC:~$` |

---

## Teil 1 – Linux (WSL2) installieren

### 1.1 Windows-Version prüfen

Drück `Windows-Taste + R`, tippe `winver` ein und drück Enter.

✅ **Kontrolle:** Dort steht **Windows 11, Version 22H2 oder neuer** (23H2 und 24H2 sind auch in Ordnung).

### 1.2 WSL mit Ubuntu installieren

In der **PowerShell (Administrator)**:

```powershell
wsl --install -d Ubuntu-24.04
```

Danach den **PC neu starten**.

Nach dem Neustart öffnet sich ein Ubuntu-Fenster, oder du öffnest „Ubuntu“ über das Startmenü. Es fragt nach:

- **Benutzername:** klein, ohne Leerzeichen, z. B. `benjamin`
- **Passwort:** Beim Tippen erscheinen keine Zeichen, das ist normal. Merk es dir, du brauchst es für `sudo`.

✅ **Kontrolle:** In der PowerShell eingeben:

```powershell
wsl -l -v
```

In der Zeile `Ubuntu-24.04` steht unter VERSION eine **2**.

### 1.3 WSL einstellen (Netzwerk und Arbeitsspeicher)

In der **PowerShell**:

```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

Der Editor fragt, ob er die Datei neu anlegen soll. Bestätige mit **Ja**. Füge dann diesen Text ein:

```ini
[wsl2]
memory=20GB
networkingMode=mirrored
```

Speichern (Strg+S) und den Editor schließen. Dann in der PowerShell:

```powershell
wsl --shutdown
```

Was die Einstellungen bewirken:

- **mirrored:** WSL bekommt dieselbe Netzwerkadresse wie Windows. Nur so erreicht das Handy später den Server.
- **20 GB:** Das ist der Arbeitsspeicher für die Tonverarbeitung. Windows behält 12 GB.

✅ **Kontrolle:** Die Datei darf nicht versehentlich `.wslconfig.txt` heißen. In der PowerShell prüfen:

```powershell
Get-ChildItem $env:USERPROFILE -Force -Filter ".wslconfig*"
```

Es erscheint genau **`.wslconfig`**, ohne `.txt`.

### 1.4 Port 8000 in der Firewall freigeben

In der **PowerShell (Administrator)**, beide Befehle nacheinander:

```powershell
New-NetFirewallRule -DisplayName "Session-Chronik 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

```powershell
New-NetFirewallHyperVRule -Name "SessionChronik8000" -DisplayName "Session-Chronik 8000 (WSL)" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 8000
```

Die erste Regel gilt für Windows, die zweite für WSL. Beide sind nötig.

Dann dein Heimnetz als **privat** markieren:
Einstellungen → Netzwerk und Internet → WLAN (bzw. Ethernet) → dein Netz → **Netzwerkprofiltyp: Privates Netzwerk**.

✅ **Kontrolle:** Beide Befehle laufen ohne rote Fehlermeldung durch.

---

## Teil 2 – Werkzeuge in Ubuntu

Ab hier arbeitest du im **Ubuntu**-Fenster.

### 2.1 Grundprogramme

```bash
sudo apt update && sudo apt install -y unzip curl ffmpeg
```

Beim ersten Mal fragt Ubuntu nach deinem Ubuntu-Passwort aus 1.2.

### 2.2 uv (verwaltet Python und alle Pakete)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
source $HOME/.local/bin/env
```

✅ **Kontrolle:**

```bash
uv --version
```

Es erscheint eine Versionsnummer, z. B. `uv 0.8.17`.

### 2.3 Grafikkarte prüfen

```bash
nvidia-smi
```

✅ **Kontrolle:** Eine Tabelle erscheint, darin **NVIDIA GeForce RTX 3060 Ti** und rechts oben eine CUDA-Version.

Wichtig: In Ubuntu **keinen** NVIDIA-Treiber installieren. WSL nutzt den Treiber von Windows mit. Fehlt die Tabelle, siehe Fehlertabelle.

---

## Teil 3 – Server installieren

### 3.1 Zip-Datei nach Ubuntu kopieren

Lade `session-chronik-server.zip` unter Windows herunter. Sie landet normalerweise im Ordner **Downloads**.

Deinen Windows-Benutzernamen findest du so heraus:

```bash
ls /mnt/c/Users/
```

Dort steht z. B. `Benjamin`. Dann kopieren und entpacken. **Ersetze `Benjamin`**, falls dein Name anders ist:

```bash
cp /mnt/c/Users/Benjamin/Downloads/session-chronik-server.zip ~/
cd ~ && unzip -o session-chronik-server.zip && cd session-chronik-server
```

Das Projekt liegt damit in `~/session-chronik-server`. Leg es nicht unter `/mnt/c/…` ab, denn dort ist es sehr langsam.

### 3.2 Pakete installieren

```bash
uv sync
```

Beim ersten Mal lädt uv Python 3.11 und alle Pakete herunter. Das dauert 1–3 Minuten.

✅ **Kontrolle:** Am Ende steht eine Liste mit `+ fastapi…`, `+ uvicorn…` usw. und keine Zeile mit `error`.

### 3.3 Konfiguration anlegen

```bash
cp .env.example .env
```

Mehr ist nicht nötig. Den geheimen Schlüssel für Anmeldungen erzeugt der Server beim ersten Start selbst und legt ihn
in `data/geheimnis.txt` ab. Eine alte `.env` mit eigenem `JWT_SECRET` funktioniert weiter.

---

## Teil 4 – Testen

### 4.1 Automatische Tests

```bash
uv run pytest
```

✅ **Kontrolle:** Die letzte Zeile lautet **`241 passed`**. Der Lauf dauert etwa eine Minute.

### 4.2 Eigenes Konto anlegen

```bash
uv run chronik create-user -u benjamin -n Benjamin
```

Das Passwort wird zweimal abgefragt (mindestens 8 Zeichen, beim Tippen unsichtbar).

✅ **Kontrolle:** `Benutzer 'benjamin' angelegt.`

### 4.3 Server starten

```bash
uv run chronik serve
```

✅ **Kontrolle:** Es erscheint `Uvicorn running on http://0.0.0.0:8000`. Das Fenster bleibt jetzt „beschäftigt“, das ist richtig so. Der Server läuft, solange das Fenster offen ist.

### 4.4 Im Browser am PC prüfen

Öffne am PC im Browser: **http://localhost:8000/api/v1/health**

✅ **Kontrolle:** Es erscheint `{"status":"ok"}`.

### 4.5 Vom Handy prüfen

Zuerst die Adresse des PCs herausfinden. In der **PowerShell**:

```powershell
ipconfig
```

Such beim Abschnitt „Drahtlos-LAN-Adapter WLAN“ bzw. „Ethernet-Adapter“ die Zeile **IPv4-Adresse**, z. B. `192.168.178.42`.

Das Handy muss **im selben WLAN** sein, nicht im Gäste-WLAN. Öffne im Handy-Browser:
**http://192.168.178.42:8000/api/v1/health** (mit deiner Adresse).

✅ **Kontrolle:** `{"status":"ok"}` erscheint auch auf dem Handy. Damit stimmt die Verbindung.

**Tipp:** Reservier im Router (z. B. FRITZ!Box: Heimnetz → Netzwerk → Gerät bearbeiten → „Immer die gleiche IPv4-Adresse zuweisen“) eine feste Adresse für den PC. Dann ändert sie sich nicht mehr.

### 4.6 Server beenden

Im Ubuntu-Fenster, in dem der Server läuft: **Strg+C**.

---

## Teil 5 – Transkriptions-Probelauf

Der Probelauf testet Transkription und Sprechertrennung mit einer echten Aufnahme, z. B. einer Podcast-Folge.
Er läuft ohne App und ohne Datenbank.

### 5.1 NVIDIA-Treiber aktualisieren (Windows)

Die KI-Pakete sind für einen neueren Treiber gebaut, als du gerade hast (560). Aktualisiere am besten über die
**NVIDIA App** (bzw. GeForce Experience) oder auf nvidia.com → Treiber → RTX 3060 Ti. Danach den PC neu starten.

✅ **Kontrolle:** In Ubuntu zeigt `nvidia-smi` oben eine **Driver Version ab 570** und **CUDA Version ab 12.8**.

### 5.2 Neue Version einspielen und KI-Pakete installieren

Neue Zip-Datei herunterladen, dann in Ubuntu (Namen ggf. anpassen):

```bash
conda deactivate
cp /mnt/c/Users/Benjamin/Downloads/session-chronik-server.zip ~/
cd ~ && unzip -o session-chronik-server.zip && cd session-chronik-server
uv sync --extra ki
```

`--extra ki` installiert WhisperX, pyannote und PyTorch. Das sind **etwa 5 GB** und dauert je nach Internet
5–15 Minuten.

Wichtig: Ab jetzt immer `uv sync --extra ki` verwenden. Ein einfaches `uv sync` würde die KI-Pakete wieder entfernen.

✅ **Kontrolle:**

```bash
uv run python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

Die Ausgabe lautet `True NVIDIA GeForce RTX 3060 Ti`.

### 5.3 Zugang zum Sprechermodell (Hugging Face)

Das Modell für die Sprechertrennung ist kostenlos, aber man muss einmal den Bedingungen zustimmen.

1. Auf **https://huggingface.co** anmelden (Konto anlegen, falls noch nicht geschehen, E-Mail bestätigen).
2. **https://huggingface.co/pyannote/speaker-diarization-community-1** öffnen. Das kurze Formular ausfüllen
   (bei Firma/Universität z. B. „privat“ bzw. deinen Verein) und **Agree and access repository** klicken.
3. Oben rechts auf dein Profilbild → **Settings** → **Access Tokens** → **Create new token**.
   Token type: **Read**, Name: `session-chronik` → **Create token**. Den angezeigten Schlüssel (beginnt mit `hf_`)
   kopieren. Er wird nur einmal angezeigt.
4. In Ubuntu, im Ordner `~/session-chronik-server` (ersetze `hf_DEIN_SCHLUESSEL` durch deinen Schlüssel):

```bash
echo "HF_TOKEN=hf_DEIN_SCHLUESSEL" >> .env
```

✅ **Kontrolle:** `grep HF_TOKEN .env` zeigt genau **eine** Zeile mit deinem Schlüssel.

Der Schlüssel ist wie ein Passwort. Gib ihn nicht weiter und schick ihn auch mir nicht.

> **Nur für den Probelauf nötig.** Im Betrieb trägst du den Schlüssel einmal in der Verwaltung ein
> (**Transkription → Sprechermodell**). Der Server lädt das Modell dann selbst und gibt es an alle Worker weiter.
> Worker brauchen dann keinen eigenen Schlüssel, und die Zeile `HF_TOKEN` kann aus ihrer `.env` wieder raus.

### 5.4 Erster kurzer Lauf (10 Minuten Audio)

Leg die Podcast-Folge unter Windows in **Downloads** ab. Steht ein Leerzeichen im Dateinamen, setz den Pfad
in Anführungszeichen:

```bash
uv run chronik probelauf "/mnt/c/Users/Benjamin/Downloads/folge.mp3" --dauer 10
```

Beim ersten Mal lädt der Befehl noch die Modelle herunter (etwa 3,5 GB). Danach geht es schneller.

✅ **Kontrolle:** Am Ende steht `Fertig. Ergebnisse in: …` und darunter ein `explorer.exe …`-Befehl.
Kopier diesen Befehl und führ ihn aus, dann öffnet sich der Ordner im Windows-Explorer.

### 5.5 Voller Lauf und Tisch-Simulation

Die ganze Folge verarbeiten. Kennst du die Zahl der Sprecher, gib sie mit `--sprecher` an:

```bash
uv run chronik probelauf "/mnt/c/Users/Benjamin/Downloads/folge.mp3" --sprecher 5 --namen "Gareth,Borbarad"
```

Die Namen hinter `--namen` sind nur ein Beispiel. Trag dort Eigennamen aus der Kampagne ein, dann schreibt
Whisper sie eher richtig.

Denselben Lauf noch einmal mit simuliertem Handy am Spieltisch (Mono, Hall, Rauschen):

```bash
uv run chronik probelauf "/mnt/c/Users/Benjamin/Downloads/folge.mp3" --sprecher 5 --tisch
```

Im Ergebnisordner liegt dann zusätzlich `tisch-hoerprobe.mp3`. Hör kurz rein, ob das realistisch klingt.

**Schick mir danach die beiden `bericht.md`-Dateien.** Aus dem Transkript reichen ein, zwei Stellen, an denen
etwas auffällig falsch ist.

Weitere Einstellungen zeigt `uv run chronik probelauf --help`.

---

## Teil 6 – Aufnahmen verarbeiten (Worker)

Ab Schritt 2 nimmt der Server Aufnahmen aus der App an und stellt sie in eine Warteschlange. Abgearbeitet werden
sie von einem **Worker**, einem eigenen Programm, das Aufträge beim Server abholt. Heute läuft beides auf
deinem PC. Später kann der Worker auf einem anderen Computer mit Grafikkarte laufen, ohne dass sich am
Server etwas ändert.

**Noch läuft der Worker im Testmodus:** Er verarbeitet das Audio wirklich (zusammenfügen, Hörproben
schneiden), schreibt aber Platzhaltertext statt einer Transkription. Die echte Erkennung kommt mit Schritt 3.

### 6.1 Zugangsschlüssel anlegen (einmalig)

Im Ubuntu-Fenster, Server **nicht** gestartet:

```bash
cd ~/session-chronik-server && uv run chronik worker-token create --name heim-pc
```

✅ **Kontrolle:** Es erscheint eine lange Zeile, die mit `wk.` beginnt. Sie wird **nur dieses eine Mal** angezeigt.

Trag sie in die `.env` ein (die Zeichenkette nach `=` durch deine ersetzen, ohne Leerzeichen):

```bash
echo "WORKER_TOKEN=wk.hier-deine-zeile" >> .env
```

Schlüssel verloren? `uv run chronik worker-token revoke --name heim-pc`, dann einen neuen mit anderem Namen anlegen.

### 6.2 Server und Worker starten

**Fenster 1** (wie bisher):

```bash
cd ~/session-chronik-server && uv run chronik serve
```

**Fenster 2:** Im Windows-Startmenü ein zweites Mal „Ubuntu“ öffnen, dann:

```bash
cd ~/session-chronik-server && uv run chronik worker --testmodus
```

✅ **Kontrolle:** `Worker bereit, warte auf Aufträge …`

### 6.3 Aus der App testen

1. In der App eine Session anlegen, Anwesende mit Einwilligung eintragen, eine kurze Aufnahme machen (1–2 Minuten reichen) und hochladen.
2. Im Fenster 2 erscheint nach ein paar Sekunden `Auftrag … übernommen` und dann `Auftrag … fertig`.
3. In der App steht die Session danach auf **„Stimmen zuordnen“**, mit so vielen Stimmen, wie Leute anwesend waren, jeweils mit einer kurzen Hörprobe. Der Text lautet noch „Platzhalter (Testmodus) …“.
4. Stimmen zuordnen und bestätigen. Nach wenigen Sekunden steht die Session auf **„Prüfen“**: Recap und Bibel-Vorschläge liegen vor (auch noch Platzhalter). Die Zusammenfassung erledigt der Server selbst, dafür braucht es kein zweites Fenster.
5. Recap bearbeiten, Vorschläge annehmen oder verwerfen, **veröffentlichen**. Danach sehen Spieler den Recap, und die angenommenen Vorschläge stehen in der Bibel.

Ohne laufenden Worker bleibt die Session in der Warteschlange, und die App zeigt den Hinweis, dass gerade
keine Transkription verfügbar ist. Sobald du ihn startest, wird sie abgearbeitet.

**Zum Ausprobieren ohne Aufnahme:** `uv run chronik demo-data` legt eine Demo-Kampagne an (Konten `sl` und `spieler`,
Passwort `chronik-demo`). Kapitel 2 wartet dort schon auf die Prüfung, mit vier Vorschlägen: neu, aufdecken, ergänzen und
ein vermuteter Scherz. Gibt es die Demo-Konten schon aus einem früheren Test, meldet der Befehl das nur und ändert nichts.

**Datenschutz:** Nach der Verarbeitung löscht der Server die Aufnahme sofort. Schlägt die Verarbeitung fehl,
bleibt sie für einen neuen Versuch höchstens 7 Tage liegen. Nie abgeschlossene Uploads werden nach 7 Tagen verworfen.
Die Hörproben werden gelöscht, sobald du die Stimmen bestätigst.

---

## Teil 7 – Echte Transkription

Ab Schritt 3 transkribiert der Worker wirklich (WhisperX mit Whisper large-v3 und Sprechertrennung). Voraussetzung
ist der Probelauf aus Teil 5: aktueller NVIDIA-Treiber und `uv sync --extra ki`. Das Sprechermodell kommt vom Server
(Verwaltung → Transkription → Sprechermodell); ohne das braucht der Worker `HF_TOKEN` in der `.env`.

### 7.1 Selbsttest ohne App

Der Selbsttest nimmt denselben Weg wie ein echter Auftrag, nur ohne Server. Nimm eine Aufnahme, die du kennst, z. B. die
Podcast-Folge vom Probelauf (Pfad und Namen anpassen):

```bash
cd ~/session-chronik-server && uv run chronik worker --selbsttest "/mnt/c/Users/Benjamin/Downloads/folge.mp3" --sprecher 4 --namen "Jemma Reed,Litha Flamel,Tubo"
```

✅ **Kontrolle:** Zuerst stehen Grafikkarte und Modell da, dann läuft die Prozentanzeige hoch (beim ersten Mal lädt er
vorher die Modelle, das dauert einige Minuten). Am Ende stehen Tempo, Grafikspeicher-Spitze, die Stimmen mit
`Abdruck: ja` und die ersten Zeilen mit richtig geschriebenen Namen. **Schick mir die Ausgabe.**

### 7.2 Worker mit echter Transkription starten

Wie in 6.2, nur **ohne** `--testmodus`. Fenster 1: `uv run chronik serve`, Fenster 2:

```bash
cd ~/session-chronik-server && uv run chronik worker
```

Dann aus der App wie in 6.3: aufnehmen, hochladen, Stimmen zuordnen. Diesmal steht dort der echte Text.

**Tipp – Vorstellungsrunde:** Stellt sich am Anfang jede Person kurz vor („Ich bin Lilio und spiele Jemma Reed“,
„Ich leite heute“), schlägt der Server die Zuordnung der Stimmen schon vor. Du musst sie nur noch bestätigen. Recap und
Vorschläge sind weiter Platzhalter, denn das Sprachmodell kommt mit Schritt 5.

**Tipps:**

- Solange der Worker arbeitet, ist die Grafikkarte voll ausgelastet. Spiele oder Videos laufen dann schlecht.
- Wenn du den PC anders brauchst: Fenster 2 mit Strg+C beenden. Aufträge warten in der Warteschlange, bis du ihn wieder startest.
- Eine Aufnahme von 4 Stunden dauert etwa 15–30 Minuten.

### 7.3 Modelle bleiben in fester Fassung

Der Worker lädt die Modelle nur einmal und fragt danach nicht mehr bei Hugging Face nach neueren Fassungen.
Welche Fassung gilt, merkt er sich in `data/modelle.json`. Eine neue Fassung kommt nur mit einem Server-Update, das
vorher geprüft wurde. So bleiben auch die Stimmabdrücke vergleichbar.

Anzeigen (und fehlende Modelle gleich laden):

```bash
cd ~/session-chronik-server && uv run chronik modelle --laden
```

Die Fassungen stehen auch in der Verwaltung unter **Transkription** in der Spalte „Gerät“.

---

## Teil 8 – Weboberfläche (Verwaltung)

Im Browser verwaltest du den Server, ohne Befehle zu tippen: Konten, Worker (auch den eigenen starten),
Warteschlange, Angaben für die App und die Sicherung. Inhalte der Kampagnen (Recaps, Bibel, Transkripte) zeigt sie nicht.

### 8.1 Dein Konto freischalten (einmalig)

Server beenden (Strg+C), dann:

```bash
cd ~/session-chronik-server && uv run chronik admin -u benjamin
```

✅ **Kontrolle:** `'benjamin' ist jetzt Verwalter von „…“.`

**Bei einer Neuinstallation** (leere Datenbank) gibt es kein Einrichtungskonto mehr. Der Server schreibt beim ersten
Start einen **Einrichtungscode** ins Protokoll:

```
  Ersteinrichtung: im Browser öffnen
    <Adresse dieses Servers>/verwaltung/einrichtung?code=K7Q2-M9XA
```

Diesen Link öffnen (am PC also `http://localhost:8000/verwaltung/einrichtung?code=…`), den eigenen Verwalter anlegen,
danach führt ein **Assistent** durch die Einrichtung:

1. Verein
2. Betriebsart: Nur Cloud, Lokaler Server oder beides
3. Zusammenfassung
4. Datenschutzhinweis (mit Vorlage)
5. Sicherung
6. Erste Spielleitung

Jeder Schritt lässt sich überspringen, offene Punkte zeigt die Übersicht. Ohne den Code kann niemand den Server
übernehmen. Code verloren? `uv run chronik einrichtungscode` zeigt ihn wieder an.

### 8.2 Öffnen

Server starten (`uv run chronik serve`), dann im Browser am PC: **http://localhost:8000/verwaltung**, vom Handy
**http://192.168.178.42:8000/verwaltung** (deine Adresse aus 4.5). Mit deinem normalen Konto anmelden.

### 8.3 Lokalen Worker aus der Oberfläche starten

Unter **Transkription → Lokaler Worker**: „Echte Transkription“ wählen, **„Beim Start des Servers automatisch
mitstarten“** anhaken, **Starten**. Ab jetzt brauchst du nur noch **ein** Ubuntu-Fenster: `uv run chronik serve`
startet den Worker mit, Strg+C beendet beide.

Läuft noch ein Worker in einem zweiten Fenster (Teil 6/7), beende ihn dort, sonst arbeiten zwei auf derselben
Grafikkarte. Das Protokoll des lokalen Workers steht auf derselben Seite unter „Protokoll“.

### 8.4 Was du dort noch findest

- **Übersicht:** Warteschlange, Fehlschläge, Speicher, Sicherung herunterladen.
- **Transkription:** lokaler Worker, weitere Worker per **Kopplungscode** anbinden, **Sprechermodell** einmal auf den
  Server laden (mit Hugging-Face-Zugang, der auf dem Server bleibt), Worker **pausieren** (z. B. während
  du spielst) oder sperren, externe Transkription einstellen.
- **Zusammenfassung:** wer Recap und Vorschläge schreibt (Cloud-API, lokales Modell, Testmodus), siehe Teil 11.
- **Warteschlange:** alle Aufträge mit Zustand und Fehlermeldung, „Erneut versuchen“.
- **Konten:** anlegen, Passwort neu setzen (meldet überall ab), Verwalter-Recht vergeben.
- **Einstellungen:** Name, Betreiber, Kontakt und Datenschutz-Link, wie die App sie anzeigt, ob sich Leute mit
  Einladungslink selbst ein Konto anlegen dürfen (Standard: ja), dazu die App-Versionen
  (Mindestversion, neueste Version, Download-Link). Die Werte aus der `.env` gelten nur, bis du hier speicherst.
- **Sprache:** oben rechts Deutsch oder English.

---

## Teil 9 – Stimmprofile

Wer möchte, legt in der App ein Stimmprofil an: 20–30 Sekunden vorlesen, mit ausdrücklicher Einwilligung. Der
Worker berechnet daraus einen Stimmabdruck, eine Zahlenreihe. Die Aufnahme wird sofort gelöscht. Danach
erkennt der Server die Person in Tischaufnahmen wieder, auch ohne Vorstellungsrunde.

### 9.1 Wiedererkennung einstellen (einmalig, hilft sehr)

Ab wann „dieselbe Person“ gilt, ist noch geschätzt. Mit echten Stimmen stelle ich es genau ein. Nimm mit dem Handy
drei kurze Sprachnachrichten auf (je 20–30 Sekunden vorlesen): **zweimal du** (gern an verschiedenen Tagen oder Orten)
und **einmal eine andere Person**. Kopier sie nach Downloads, dann (Namen anpassen):

```bash
cd ~/session-chronik-server && uv run chronik stimmen-vergleich /mnt/c/Users/Benjamin/Downloads/ich1.m4a /mnt/c/Users/Benjamin/Downloads/ich2.m4a /mnt/c/Users/Benjamin/Downloads/andere.m4a
```

✅ **Kontrolle:** Drei Zeilen mit Ähnlichkeiten. „ich1 ↔ ich2“ sollte deutlich höher liegen als die Paare mit der
anderen Person. **Schick mir die Ausgabe.** Die Aufnahmen werden dabei nicht gespeichert.

---

## Teil 10 – Externe Transkription (optional)

Ersatz für Zeiten, in denen kein eigener Worker läuft: Mistral (Voxtral) transkribiert dann die Aufnahme,
für etwa 0,3 Cent pro Minute. **Standardmäßig aus.** Sie greift nur, wenn alles zusammenkommt:

- Du hast sie in der `.env` freigegeben.
- Die SL hat sie für die Kampagne erlaubt. In der App heißt der Schalter „Externe Transkription erlauben“.
- Eine Session wartet länger als 24 Stunden.
- In dieser Zeit war kein Worker erreichbar.

**Vorher klären:** Das Audio verlässt dann den Verein. Dafür brauchst du einen Vertrag zur Auftragsverarbeitung mit
Mistral (bei Mistral online abschließbar) und einen Absatz im Datenschutzhinweis.

### 10.1 Qualität ausprobieren

API-Schlüssel bei Mistral anlegen (console.mistral.ai). In der Verwaltung unter **Transkription → Externe
Transkription** eintragen, Anbieter vorerst auf **Aus** lassen und speichern. Dann 10 Minuten der Probefolge schicken
(kostet etwa 3 Cent) und mit dem Probelauf vergleichen:

```bash
uv run chronik extern-probe "/mnt/c/Users/Benjamin/Downloads/folge-lang.mp3" --dauer 10 --namen "Jemma Reed,Litha Flamel,Tubo"
```

**Schick mir die Ausgabe**, dann entscheiden wir, ob sich die Freigabe lohnt.

### 10.2 Freigeben

Unter **Transkription → Externe Transkription** den Anbieter auf **Mistral (Voxtral)** stellen, Wartezeit wählen,
**Speichern**. Das wirkt sofort, ein Neustart ist nicht nötig. Der Schlüssel steht danach in der Datenbank (auch in
Sicherungen) und wird nicht wieder angezeigt.

---

## Teil 11 – Zusammenfassung mit Sprachmodell

Nach der Stimmzuordnung schreibt ein Sprachmodell den Recap und die Bibel-Vorschläge. Bisher war das ein Platzhalter.
Wer das übernimmt, stellst du in der Verwaltung unter **Zusammenfassung** ein:

- **Cloud-API:** Mistral Large 3 im Internet (EU). Das kostet etwa 6 Cent pro 4-Stunden-Session.
- **Lokales Modell:** Ollama auf deinem PC. Dabei verlässt nichts das Haus. Ein Recap dauert etwa 10–20 Minuten,
  und kleine Modelle schreiben schwächer.

### 11.1 Über die Cloud-API (Mistral)

1. Öffne **Verwaltung → Zusammenfassung** und wähle bei „Wer fasst zusammen?“ den Eintrag **Cloud-API**.
2. Stelle den Anbieter auf **Mistral** und das Modell auf `mistral-large-latest`.
3. Beim API-Schlüssel hast du zwei Möglichkeiten:
   - Hast du schon einen Schlüssel für die externe Transkription eingetragen, lass das Feld leer. Dann wird dieser
     Schlüssel genommen.
   - Sonst legst du bei console.mistral.ai einen Schlüssel an und trägst ihn hier ein.
4. Klicke auf **Speichern**. Das wirkt sofort.

Das Transkript geht dann an Mistral. Dafür braucht es denselben Vertrag zur Auftragsverarbeitung und denselben
Datenschutz-Absatz wie bei der externen Transkription.

### 11.2 Lokales Modell (Ollama)

Installiere Ollama in Ubuntu (einmalig):

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama --version
```

Meldet `ollama --version`, dass kein Server läuft, startest du ihn in einem eigenen Ubuntu-Fenster mit
`ollama serve` und lässt das Fenster offen.

Starte danach den Worker neu (**Transkription → Lokaler Worker → Beenden → Starten**). Er erkennt Ollama von selbst.
Unter **Zusammenfassung** steht er dann bei „Worker mit Sprachmodell“. Dort wählst du
**Lokales Modell** und speicherst.

Das Modell (`ministral-3:8b`, etwa 6 GB) lädt der Worker beim ersten Auftrag selbst. Willst du es vorher laden:

```bash
ollama pull ministral-3:8b
```

Nach jeder Zusammenfassung gibt der Worker die Grafikkarte sofort wieder frei, damit die nächste Transkription
genug Speicher hat.

### 11.3 Qualität vergleichen

Ohne etwas zu speichern, kannst du für eine vorhandene Session einen Recap schreiben lassen:

```bash
cd ~/session-chronik-server
uv run chronik recap-probe              # listet Sessions mit Transkript (ID vorne)
uv run chronik recap-probe <ID>         # über die Cloud-API
uv run chronik recap-probe <ID> --lokal # mit Ollama auf diesem Computer
```

**Schick mir die Ausgaben.** Daran sehen wir, ob das kleine Modell reicht und ob ich die Anweisungen nachschärfen muss.

### 11.4 SL-Unterlagen ausprobieren

Dasselbe Sprachmodell wertet auch Unterlagen aus: Abenteuer-PDFs, eigene Notizen, Spielerhandouts. Ohne etwas zu
speichern:

```bash
uv run chronik unterlage-probe "/mnt/c/Users/Benjamin/Downloads/abenteuer.pdf" --art gm
uv run chronik unterlage-probe "/mnt/c/Users/Benjamin/Downloads/handout.pdf" --art handout --lokal
```

Mit `--kampagne <ID>` nimmt es die Bibel dieser Kampagne dazu und schlägt dann Ergänzungen statt Doppelter vor.
Kampagnen-IDs zeigt `uv run chronik recap-probe`. **Schick mir die Ausgaben.**

---

## Teil 12 – Sicherung und Wiederherstellen

Der Server sichert jede Nacht selbst: Datenbank, Bilder und SL-Unterlagen in eine ZIP-Datei unter
`data/sicherungen/`. Die letzten 14 Tage bleiben erhalten. In der Verwaltung (Übersicht → Sicherung) kannst du:

- sofort sichern
- jede Sicherung herunterladen
- einen zusätzlichen Ordner angeben, z. B. eine eingebundene Storage Box, in den jede Sicherung kopiert wird

Von der Kommandozeile: `uv run chronik sicherung`.

**Wiederherstellen:** Server beenden, dann:

```bash
cd ~/session-chronik-server && uv run chronik wiederherstellen data/sicherungen/taleward-sicherung-….zip
```

Der jetzige Stand wird vorher selbst gesichert (Datei mit `-vorher` im Namen). Danach den Server wieder starten.

## Teil 13 – Weitere Worker koppeln

**Am einfachsten mit der Worker-App** für Windows oder Linux ([worker-app/README.md](worker-app/README.md)). Sie
braucht weder WSL noch Kommandozeile: installieren, Adresse und Kopplungscode eingeben, fertig. Den Rest dieses
Teils brauchst du nur ohne App.

Soll ein lokaler Server mit Grafikkarte transkribieren, musst du keinen Schlüssel mehr von Hand kopieren:

1. In der Verwaltung **Transkription → Weiteren Worker anbinden → Kopplungscode erzeugen** wählen. Der Code gilt
   15 Minuten.
2. Auf dem lokalen Server (nach Teil 1–3 und 5.2) den angezeigten Befehl ausführen:

   ```bash
   uv run chronik worker --koppeln ABCD-EFGH --server https://taleward.meinverein.de
   ```

Der Worker trägt Schlüssel und Adresse selbst in seine `.env` ein. Beim nächsten Mal reicht `uv run chronik worker`.
Das Sprechermodell lädt er vom Server (**Transkription → Sprechermodell**), geprüft und in genau der Fassung, die
auch alle anderen Worker nutzen. Ein Hugging-Face-Konto braucht er dafür nicht.

## Teil 14 – Benachrichtigungen, Passwort vergessen, Übergabe

### 14.1 Benachrichtigungen

Damit du Probleme merkst, ohne in die Verwaltung zu schauen: **Einstellungen → Benachrichtigungen**.

- **ntfy (am einfachsten):**
  1. Die App „ntfy“ aufs Handy laden.
  2. Ein Thema abonnieren, das niemand errät, z. B. `traumjaeger-taleward-7k2q`.
  3. In der Verwaltung `https://ntfy.sh/traumjaeger-taleward-7k2q` eintragen.
- **E-Mail:** Mailserver, Port, Benutzer und Passwort deines E-Mail-Anbieters (die Angaben wie in einem
  Mailprogramm), dazu den Empfänger.
- Mit **„Speichern und Testnachricht senden“** prüfst du, ob alles ankommt.

Gemeldet wird:

- Aufträge warten länger als 6 Stunden (einstellbar), und kein Worker ist verbunden.
- Aufträge sind endgültig fehlgeschlagen.
- 80 % bzw. 100 % des Kostenlimits sind erreicht.
- Eine Sicherung ist fehlgeschlagen oder älter als 48 Stunden.
- Es sind weniger als 2 GB Speicher frei.

Inhalte aus Kampagnen stehen nie in den Meldungen.

### 14.2 Passwort vergessen

**Konten → „Passwort-Link erstellen“** zeigt einen Link und einen QR-Code. Der Link gilt einmal und 24 Stunden. Die
Person öffnet ihn und wählt selbst ein neues Passwort. Danach ist sie auf allen Geräten abgemeldet.

### 14.3 Auftragsverarbeitung und Übergabe

Unter **Einstellungen → „Auftragsverarbeitung und Übergabe der Verwaltung“** findest du zwei Dinge:

- Die Verträge, die der Verein braucht (Hosting, Mistral), mit Links. Dort vermerkst du das Datum des Abschlusses.
- Eine Liste für die Übergabe an eine neue Verwaltung.

## Teil 15 – Anmeldung: „Passwort vergessen“ und Google, Discord, Microsoft, Apple

Voraussetzung für beides: Der Server ist unter einer eigenen Domain mit HTTPS erreichbar. Diese Adresse trägst du unter
**Einstellungen → Öffentliche Adresse des Servers** ein, z. B. `https://taleward.meinverein.de`.

### 15.1 „Passwort vergessen“ per E-Mail

Unter **Einstellungen → Benachrichtigungen → E-Mail-Versand** trägst du einen Mailserver ein (die Angaben wie in einem
Mailprogramm). Danach zeigt die App „Passwort vergessen?“:

- Mitglieder können dann freiwillig eine E-Mail-Adresse hinterlegen und bestätigen.
- Den Link zum Zurücksetzen bekommen nur sie selbst.
- „Empfänger der Benachrichtigungen“ darf leer bleiben.

### 15.2 Anmeldedienste

Unter **Anmeldung** gibt es für jeden Dienst eine kurze Anleitung und die **Rückleitungsadresse**. Diese Adresse trägst
du beim Dienst ein; Client-ID und Schlüssel des Dienstes trägst du bei Taleward ein.

- **Google:** Google Cloud Console, OAuth-Client vom Typ „Webanwendung“.
- **Discord:** Discord Developer Portal, eine Application, dort unter OAuth2 die Redirects.
- **Microsoft:** Microsoft Entra, eine App-Registrierung für „alle Organisationen und persönliche Konten“.
- **Apple:** Das braucht ein Apple-Entwicklerkonto und lohnt sich erst mit einer iPhone-Fassung der App.

Die App zeigt nur die Dienste, die hier eingerichtet sind. Jeder eingeschaltete Dienst gehört in den
Datenschutzhinweis; die Vorlage im Assistenten ergänzt ihn automatisch.

---

## Teil 16 – Updates für App, Worker und Server

Der Server fragt einmal am Tag bei GitHub nach neuen Fassungen (Verwaltung → **Updates**):

- **App und Worker:** Er lädt die Dateien (Android-APK, Windows-Installer) selbst herunter, prüft sie und bietet sie
  unter seiner eigenen Adresse an. Handys und Worker fragen nur diesen Server, nie GitHub. Standard ist
  „Automatisch freigeben“. Mit „Erst nach meiner Freigabe“ testest du eine neue Fassung zuerst und gibst sie dann frei.
- **Worker-App:** Sie installiert neue Fassungen selbst, sobald sie nichts zu tun hat (abschaltbar in ihren
  Einstellungen).
- **Server:** Eine neue Server-Fassung zeigt die Seite mit dem passenden Befehl an. Wenn Benachrichtigungen
  eingerichtet sind, kommt zusätzlich eine Nachricht.
- **Web-App:** Hat ein App-Release die Web-Fassung (`taleward-web-….zip`) dabei, liefert der Server sie unter
  `https://<server>/app/` selbst aus. Die Einladungsseite bekommt dann „Im Browser öffnen“. Ohne eigene Web-App zeigt
  dieser Knopf auf die zentrale Web-App auf taleward.org, sofern sie unter Einstellungen → App-Versionen erlaubt ist.
- Ohne Internetzugang oder zum Abschalten: `UPDATE_CHECK=false` in der `.env`.

## Alltag: Server später wieder starten

Ubuntu öffnen, dann:

```bash
cd ~/session-chronik-server && uv run chronik serve
```

Ist in der Verwaltung „automatisch mitstarten“ eingestellt (8.3), läuft der Worker damit schon mit. Sonst im
zweiten Ubuntu-Fenster `uv run chronik worker` starten.

## Update auf eine neue Version

**Vorher den Server beenden (Strg+C) und die Datenbank sichern:**

```bash
cd ~/session-chronik-server && cp data/chronik.db "data/chronik-sicherung-$(date +%Y%m%d-%H%M).db"
```

Dann die neue Zip-Datei herunterladen und in Ubuntu einspielen (Namen ggf. anpassen):

```bash
cp /mnt/c/Users/Benjamin/Downloads/session-chronik-server.zip ~/
cd ~ && unzip -o session-chronik-server.zip && cd session-chronik-server && uv sync --extra ki
```

Deine `.env` und die Datenbank im Ordner `data/` bleiben dabei erhalten.

---

## Wenn etwas nicht klappt

| Problem | Lösung |
|---|---|
| `wsl --install` meldet Fehler **0x80370102** oder „Virtualisierung nicht aktiviert“ | Im BIOS/UEFI die Virtualisierung einschalten (Intel: „VT-x“/„Intel Virtualization Technology“, AMD: „SVM Mode“). Danach nochmal. |
| `wsl -l -v` zeigt VERSION **1** | In der PowerShell: `wsl --set-version Ubuntu-24.04 2` |
| `New-NetFirewallHyperVRule` ist „nicht erkannt“ | In der PowerShell `wsl --update`, dann das Fenster neu öffnen und nochmal. |
| `uv: command not found` | `source $HOME/.local/bin/env` eingeben oder Ubuntu schließen und neu öffnen. |
| `nvidia-smi: command not found` oder keine Tabelle | Unter Windows den aktuellen NVIDIA-Treiber installieren (GeForce Experience/NVIDIA App oder nvidia.com), PC neu starten. In Ubuntu **nichts** installieren. |
| `cp: cannot stat …zip` | Pfad oder Name stimmt nicht. Mit `ls /mnt/c/Users/Benjamin/Downloads/` nachsehen, wie die Datei genau heißt (z. B. `session-chronik-server (1).zip`). |
| `uv sync` bricht mit Netzwerkfehler ab | Einfach nochmal ausführen. |
| Probelauf: „PyTorch sieht keine Grafikkarte“ oder „driver … too old“ | NVIDIA-Treiber unter Windows aktualisieren (5.1), PC neu starten. |
| Probelauf: `libcudnn…` oder `libcublas…` nicht gefunden | `uv sync --extra ki` nochmal ausführen. Hilft das nicht, die ganze Meldung an mich schicken. |
| Probelauf: „Hugging Face verweigert den Zugriff“ | 5.3 prüfen: Bedingungen akzeptiert? Schlüssel vollständig kopiert? |
| Probelauf: `CUDA out of memory` | Mit `--batch 4` nochmal. Hilft das nicht: `--modell large-v3-turbo`. Andere Programme mit Grafiklast (Spiele, Browser mit Videos) schließen. |
| Probelauf: `No such file` bei der Audiodatei | Pfad in Anführungszeichen setzen; Namen prüfen mit `ls /mnt/c/Users/Benjamin/Downloads/`. |
| Verwaltung: „kein Verwalter-Recht“ | Teil 8.1 ausführen. |
| Verwaltung: eigener Worker „beendet mit Fehler“ | „Protokoll“ aufklappen. Meist liegt das Sprechermodell noch nicht auf dem Server (Transkription → Sprechermodell) oder es fehlt `uv sync --extra ki`. |
| Worker: „Sprechermodell liegt noch nicht auf dem Server“ oder „KI-Pakete fehlen“ | In der Verwaltung unter Transkription den Hugging-Face-Zugang eintragen und „Jetzt auf den Server laden“; bzw. Teil 5.2 wiederholen. |
| Session schlägt fehl mit „Grafikspeicher … voll“ | Andere Programme mit Grafiklast schließen. Kommt es öfter vor: in der `.env` die Zeile `WHISPER_BATCH=4` ergänzen und den Worker neu starten. |
| Session schlägt fehl mit „keinen Zugang zum Sprechermodell“ | Teil 5.3 prüfen, Worker neu starten, in der App „Erneut versuchen“. |
| Worker: „Kein Worker-Token“ | 6.1 ausführen und die Zeile in die `.env` eintragen. |
| Worker: „lehnt den Worker-Token ab (401)“ | Zeile in der `.env` vollständig? `uv run chronik worker-token list` zeigt, ob der Schlüssel noch `aktiv` ist. Sonst neu anlegen (6.1). |
| Worker: „Server nicht erreichbar“ | Läuft der Server in Fenster 1? Der Worker versucht es selbst weiter. |
| Session hängt auf „In der Warteschlange“ | Läuft der Worker (Fenster 2)? |
| `pytest` zeigt Fehler (`failed`) | Die komplette Ausgabe an mich schicken. |
| Server startet nicht, Meldung zu „Geheimnis für Anmeldungen“ | Der Ordner `data/` ist nicht beschreibbar. Rechte prüfen oder `JWT_SECRET` in der `.env` setzen. |
| `address already in use` | Der Server läuft schon in einem anderen Ubuntu-Fenster. Dort mit Strg+C beenden. |
| Health-Test am PC geht, **am Handy nicht** | Nacheinander prüfen: Handy im selben WLAN (kein Gäste-WLAN)? Netzwerkprofil „Privat“ (1.4)? Beide Firewall-Regeln gesetzt? `.wslconfig` richtig benannt und danach `wsl --shutdown` ausgeführt (1.3)? Richtige IPv4-Adresse aus `ipconfig`? |
| Health-Test geht **auch am PC nicht** | Läuft der Server (4.3)? Steht dort eine Fehlermeldung? Diese an mich schicken. |
