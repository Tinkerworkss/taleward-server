# Entwicklungsnotizen

Entscheidungen und Stände der einzelnen Schritte, in der Reihenfolge der Entwicklung.

## Entscheidungen in Schritt 1

- **Für Spieler unsichtbar heißt 404:** Das gilt für geheime Einträge, unveröffentlichte Sessions (auch Status und Recap) und für Kampagnen, in denen man nicht Mitglied ist. 403 gibt es nur, wenn die Sache sichtbar, die Aktion aber der SL vorbehalten ist.
- **Neue Bibeleinträge ohne Angabe von `visibility`** sind `gm_only`. So wird im Zweifel lieber zu wenig verraten.
- **Erwähnungen in Kapiteln:** `mentions` sowie `first/lastSessionNumber` zählen für Spieler nur veröffentlichte Kapitel.
- **Nur die SL** legt Sessions an und ändert sie. Einwilligungen sind ab Upload-Start gesperrt.
- **`consentAt`** setzt der Server selbst, wenn die App keinen Zeitpunkt mitschickt.
- **Die letzte SL** kann sich nicht selbst zum Spieler machen.
- **`pendingReviewCount`** zählt Sessions in `awaiting_speakers` und `awaiting_review`. Für Spieler ist der Wert immer 0.
- **Suche:** Alle Suchwörter müssen in Name oder Zusammenfassung vorkommen. Groß-/Kleinschreibung und Umlaute werden korrekt behandelt.
- **Später HTTPS:** `serve` wertet Proxy-Header aus (`FORWARDED_ALLOW_IPS`). Stellt die App auf `androidScheme: 'https'` um, muss `https://localhost` in `CORS_ORIGINS` eingetragen werden.

## Entscheidungen in Schritt 1b

- **Tokens sind an diesen Server gebunden** (`aud` = Server-ID, liegt in der Datenbank). Nach dem Update auf 1b
  müssen sich alle einmal neu anmelden.
- **Konto und Anmeldeart sind getrennt** (Tabelle `auth_methods`). Heute gibt es nur Passwörter, später können
  OpenID Connect und Passkeys dazukommen, ohne dass Konten umgebaut werden müssen.
- **Organisation:** Beim Update wird eine Organisation angelegt (Name aus `ORGANIZATION_NAME`). Alle Konten und
  Kampagnen gehören ihr an. `chronik create-user --admin` trägt jemanden als Verwalter ein.
- **Einwilligung:** `consentSource: app` übernimmt nur die eigene, stehende Zustimmung des Mitglieds. Fehlt sie,
  kommt 409 `consent_missing`. Gäste können nur vor Ort zustimmen. Zustimmung, Widerruf und Zustimmungen vor Ort
  landen mit Zeitpunkt in `consent_log`. Das Protokoll bleibt als Nachweis erhalten.
- **Vor dem Upload** wird noch einmal geprüft, ob eine App-Zustimmung inzwischen widerrufen wurde (409 `consent_revoked`).
- **Charakter-Hintergrund und `gmNotes`** werden für Unbefugte weggelassen, nicht auf `null` gesetzt. Die Suche der
  Spieler durchsucht `gmNotes` nicht.
- **Ungelesen:** Recaps und Bibel werden gezählt, Kommentare kommen mit 1c. Für die SL ist alles 0, weil sie die
  Inhalte selbst erstellt.
- **Fehlermeldungen** sind auf Deutsch und Englisch hinterlegt (`Accept-Language`) und stehen gesammelt in `app/errors.py`.
- **Noch nicht fertig** (Antwort 409 `feature_unavailable`, leere Liste oder 404): Upload, Kommentare schreiben,
  Terminabstimmung, Titel- und Charakterbilder, SL-Unterlagen, Stimmprofil anlegen, Datenexport, Konto löschen.
  Registrierung: 403 `registration_closed`, Konten legt der Betreiber per `chronik create-user` an.

## Nachgezogen auf Schnittstelle 0.3.7

- `GET /info` meldet `apiVersion` 0.3.7 und `externalTranscription: null`, denn eine externe Transkription gibt es noch nicht.
- `Campaign.allowExternalTranscription` (Standard aus, nur SL) wird gespeichert, wirkt aber erst mit der externen Transkription.
- `Session.transcriptionEngine` bleibt `null`, bis Schritt 3 transkribiert.
- **„Wer weiß was“** (`Entry.hiddenFromMemberIds`): Vor diesen Mitgliedern ist ein öffentlicher Eintrag vollständig
  verborgen, also in der Liste, bei der Suche, im Einzelabruf (404) und bei den Ungelesen-Zählern. Das Feld selbst sieht nur die SL.
- Einen Eintrag per PATCH öffentlich zu machen geht nur mit öffentlichem Text (sonst 400).
- `reveal`-Vorschläge und `Proposal.hiddenFromMemberIds` kommen mit den Vorschlägen (Schritt 5), der Bildausschnitt fürs
  Charakterbild mit den Bildern (1c).

## Entscheidungen in Schritt 2a

- **Zentrale und Worker:** Der Server rechnet selbst nichts. Worker melden sich mit einem eigenen
  Schlüssel an (`wk.…`, nur als Prüfsumme gespeichert, einzeln widerrufbar) und holen Aufträge per Long-Poll ab.
  Sie brauchen keinen offenen Port. Außerhalb von localhost verbinden sie sich nur per HTTPS (`--unsicher` für Heimnetz-Tests).
- **Leases:** Ein Auftrag gehört 180 s lang einem Worker. Lebenszeichen verlängern das. Bleiben sie aus, holt
  die Wartung den Auftrag zurück, und ein anderer Worker übernimmt. Ein verspätetes Ergebnis wird abgelehnt.
  Vorübergehende Fehler werden bis zu dreimal versucht, danach steht die Session auf `failed` mit Begründung.
  „Erneut versuchen“ geht, solange das Audio noch da ist, sonst 409 `audio_gone`.
- **Upload:** Teile zu 5 MiB, optional mit `X-Chunk-SHA256`. Ein erneuter Start mit denselben Dateien setzt den
  offenen Upload fort. Beim Abschluss wird die Einwilligung noch einmal geprüft. Nur die SL lädt hoch, Spieler bekommen 404.
- **Audio löschen:** sofort nach erfolgreicher Transkription (`audioDeletedAt`). Bei Fehlschlag spätestens nach
  7 Tagen, nie abgeschlossene Uploads ebenfalls nach 7 Tagen. Hörproben (höchstens einige Sekunden je Stimme)
  bleiben bis zur Bestätigung der Stimmen (2b).
- **Statushinweise** werden als Schlüssel gespeichert und erst beim Abruf übersetzt (de/en). „Kein Worker verbunden“
  erscheint, solange sich seit 2 Minuten kein Worker gemeldet hat.
- **Namenshilfe für die Erkennung:** Anzeige- und Charakternamen, Namen aus der Bibel (zuletzt erwähnte zuerst),
  Systemname und eine Begriffsliste je System (DSA, D&D, Pathfinder, Cthulhu, Shadowrun, Splittermond), gekappt auf 80
  Begriffe. SL-Notizen und Charakter-Hintergründe fließen nie ein.
- **Discord:** Jede Spur gehört einem Mitglied. Die Stimmen sind damit schon zugeordnet, und die Session geht direkt
  weiter zur Zusammenfassung (ab 2b).
- **Verbrauch** (`/usage`) wird aus einem Protokoll je Verarbeitung berechnet (Audiominuten, Rechenzeit, Worker).

## Entscheidungen in Schritt 2b

- **Zusammenfassen macht die Zentrale selbst** (Plan „Zentrale und Worker“): Ein Arbeitsprozess im Server holt
  Aufträge vom Typ `summarize` aus derselben Warteschlange (Lease, 3 Versuche, „Erneut versuchen“ setzt bei der
  Zusammenfassung an). `SUMMARIZER=attrappe` (Standard) oder `aus`. Ab Schritt 5 kommen hier die Sprachmodelle dazu.
- **Was das Sprachmodell sieht, legt genau eine Funktion fest** (`eingabe_bauen`): Transkript mit Charakternamen,
  Anwesende, Kampagne und Welt-Hintergrund, öffentliche Bibeleinträge. Geheime Einträge nur mit Typ und Namen (für
  „aufdecken“). Nie: SL-Notizen, `gmNotes`, Texte geheimer Einträge, Charakter-Hintergründe. Ein Test prüft das.
- **Aufdecken (`reveal`):** Den geheimen Teil des Vorschlags füllt der Server aus dem bisherigen Eintrag, nicht das
  Sprachmodell. Beim Veröffentlichen wird der Vorschlagstext zum öffentlichen Text, der Rest bleibt in `gmNotes`.
- **Ergänzen (`update`):** Der Text wird angehängt. Ist der Vorschlag geheim, der Eintrag aber öffentlich, landet das
  Neue nur in `gmNotes`, damit nichts durchrutscht.
- **Stimmen bestätigen:** Nicht genannte Stimmen behalten den Vorschlag, `memberId: null` heißt Gast bzw. ignorieren.
  Danach werden Hörproben und Stimmabdrücke gelöscht (Pflichtregel). Bei Discord passiert das sofort, weil die Spuren
  die Zuordnung schon festlegen.
- **Vorschläge sind für Spieler immer 404**, auch bei veröffentlichten Sessions (vorher 403), weil schon ihre Existenz
  etwas verraten kann.
- **Veröffentlichen:** Angenommene Vorschläge werden übernommen, offene gelten als verworfen, jeder betroffene Eintrag
  bekommt eine Erwähnung im Kapitel. Hat die Session keinen Titel, übernimmt sie den des Recaps. Danach sind Recap und
  Vorschläge nicht mehr änderbar (409 `already_published`).
- **Hörprobe schon gelöscht:** 410 ohne Inhalt, wie in der YAML 0.3.7 (Wunsch für 0.3.8: Fehlerformat wie überall).

## Entscheidungen in Schritt 3

- **Ablauf im Worker:** Abschnitte zusammenfügen → Whisper (nur `hotwords` als Namenshilfe, Echo-Filter) →
  wortgenaue Zeiten → pyannote mit Stimmabdrücken. Die Modelle werden je Auftrag geladen und danach freigegeben. Das
  kostet etwas Zeit, gibt die Grafikkarte zwischen den Aufträgen aber für anderes frei.
- **Sprecherzahl:** Anwesende ±1 als Grenzen für pyannote. Gruppen unter 5 s Redezeit werden keine eigene Stimme,
  ihre Zeilen bleiben ohne Stimme im Transkript (meist Fehlzuordnungen).
- **Whisper-Erfindungen** bei Musik oder Stille („Musik Musik“, „Untertitel im Auftrag des ZDF“) werden entfernt,
  am Tisch nur, wenn die Sprechertrennung dort keine Stimme gefunden hat.
- **Hörprobe:** ein Abschnitt möglichst nahe an 6 s (höchstens 8 s), dazu sein Text als `sampleText`.
- **Discord:** jede Spur einzeln, ohne Sprechertrennung und ohne Stimmabdruck. Die Zentrale führt die Zeilen nach
  Zeit zusammen.
- **Fehler:** „Grafikspeicher voll“ wird automatisch erneut versucht. Fehlender Zugang zum Sprechermodell oder fehlende
  CUDA-Bibliotheken brechen mit klarer Meldung ab. Der Worker prüft Treiber, Pakete und `HF_TOKEN` schon beim Start.
- **Einstellungen** (`.env` des Workers): `WHISPER_MODEL` (large-v3), `WHISPER_COMPUTE_TYPE` (int8_float16),
  `WHISPER_BATCH` (8).
- **Geprüft** mit einem Test-Motor an Stelle von WhisperX (Tests ohne Grafikkarte). Die Aufrufe an WhisperX 3.8.6
  sind gegen dessen Quellcode geprüft. Der erste Lauf mit Grafikkarte ist der Selbsttest (INSTALLATION.md Teil 7.1).

## Entscheidungen zur Weboberfläche (Verwaltung)

- **Zugang** nur für Verwalter einer Organisation (`chronik admin -u …` oder in der Oberfläche vergeben). Eigene
  Sitzung als Cookie (HttpOnly, SameSite=Strict, 12 h), unabhängig vom App-Token. Jedes Formular trägt ein
  CSRF-Merkmal. Nach 5 Fehlversuchen in 5 Minuten ist die Anmeldung kurz gesperrt. Strenge Sicherheits-Kopfzeilen
  (CSP ohne Skripte, kein Einbetten).
- **Einrichtungskonto `admin`/`admin`:** Das legt der Server nur bei leerer Datenbank an (abschaltbar mit
  `CREATE_SETUP_ACCOUNT=false`). Es kann ausschließlich die Seite „Ersteinrichtung“ öffnen, dort wird der eigene
  Verwalter angelegt, danach löscht es sich selbst. In der App ist es gesperrt (401 `setup_account_only`).
- **Keine Inhalte:** Die Verwaltung zeigt Kampagnentitel, Kapitelnummern und Zustände, aber keine Recaps, Bibel,
  Transkripte oder Notizen. Ausnahme ist die Sicherung, eine vollständige Kopie der Datenbank. Der Hinweis steht daneben.
- **Lokaler Worker:** wird als eigener Prozess auf demselben Server gestartet (echt oder Attrappe) und spricht
  wie jeder andere über das Worker-Protokoll. Bei jedem Start bekommt er einen frischen Schlüssel, gespeichert wird
  nur die Prüfsumme. Er startet auf Wunsch mit dem Server und endet mit ihm (Strg+C beendet beide). Das Protokoll
  steht in `data/logs/worker.log` und in der Oberfläche.
- **Pausieren:** Ein pausierter Worker bleibt verbunden, bekommt aber keine Aufträge.
- **Server-Angaben** (Name, Betreiber, Kontakt, Datenschutz-Link, Mindestalter) liegen in der Datenbank und
  überschreiben die `.env`. `/info` und die Einladungsseite nutzen sie.
- **Wartung** läuft jetzt sofort beim Start: Aufträge eines abgestürzten Workers werden nach einem Neustart
  gleich neu eingereiht. Fertige Aufträge behalten den Namen des Workers.
- **Logo:** Das Chronistensiegel aus dem Markenentwurf (25.09.2026), vektorisiert aus der Rastergrafik:
  `app/verwaltung/static/marke/` enthält `signet.svg` (Tinte/Siegel), `signet-hell.svg` (für dunkle Flächen),
  Favicon und App-Symbole. Es erscheint in der Kopfzeile, auf der Anmeldung, auf der Einladungsseite (`/einladung/…`)
  und als Browser-Symbol. Sobald die offizielle Vektorzeichnung vorliegt, werden nur diese Dateien ersetzt.
- **Schriften:** Es werden keine Schriften von fremden Servern geladen (DSGVO). Die Alegreya-Dateien kommen ins
  Paket, sobald sie vorliegen. Bis dahin gelten die Ersatzschriften aus dem Markenhandbuch.

## Entscheidungen in Schritt 4 (Vorstellungsrunde)

- **Nach der Transkription** sucht die Zentrale in den ersten 20 Minuten nach Vorstellungen: „ich bin / ich heiße /
  mein Name ist …“ (Name oder Figur), „ich spiele / mein Charakter heißt …“ (Figur) und „ich leite / ich bin die SL“
  (nur wenn genau eine SL anwesend ist). Englisch geht auch („I'm …, I play …, I'm your GM“). Aufeinanderfolgende
  Zeilen einer Stimme werden verbunden.
- **Namensvergleich** mit Anzeige- und Benutzernamen sowie Charakternamen der Anwesenden mit Konto, samt Vor- und
  Nachnamen. Er verzeiht Whisper-Schreibweisen („Gemma“ statt „Jemma“), gibt dafür aber weniger Sicherheit. Gäste und
  Nicht-Anwesende kommen nie in Frage.
- **Verteilung:** Hinweise werden je Stimme und Person verrechnet. Widersprüche (dieselbe Stimme nennt zwei Personen)
  senken die Sicherheit. Jede Person wird höchstens einer Stimme vorgeschlagen, erst ab Sicherheit 0,5.
  `Speaker.source` = `intro_round`, `confidence` = Sicherheit. Die SL bestätigt immer, „Bestätigen“ ohne Änderung
  übernimmt die Vorschläge.
- **Stimmprofile (Schritt 6)** liefern später Hinweise mit `voice_match` in dieselbe Verteilung. Eine Stimme, die
  in Vorstellung und Profil übereinstimmt, ist dann besonders sicher.
- Bei Discord-Aufnahmen entfällt das, denn die Spuren legen die Zuordnung fest.

## Entscheidungen in Schritt 6 (Stimmprofile)

- **Den Abdruck berechnet der Worker** (Auftrag `voice_enroll`), mit demselben Modell wie die Sprechertrennung
  (pyannote community-1, eine Person). Nur so passt er zu den Stimmgruppen der Sessions. Die Zentrale braucht dafür
  keine KI-Pakete. Der Worker bekommt nur die Aufnahme, ohne Namen, und gibt nur den Abdruck zurück.
- **Nie Audio:** Die Aufnahme liegt nur bis zur Berechnung auf der Platte (`data/voice/`). Sie wird danach sofort
  gelöscht, bei Fehlschlag ebenfalls, und die Wartung räumt spätestens nach einem Tag auf. Gespeichert werden
  Basis-Abdruck, Summe der gelernten Abdrücke, Zähler und der Zeitpunkt der Einwilligung.
- **Prüfungen:** Einwilligung Pflicht (400), höchstens 20 MB (413), mindestens 10 s Aufnahme (400, falls `ffprobe`
  vorhanden) und mindestens 12 s erkannte Sprache (sonst `failed` mit Begründung). Während der Berechnung ist kein
  zweites Anlegen möglich (409). Ein neues Profil ersetzt das alte samt allem Gelernten.
- **Wiedererkennen:** Nur Profile von Anwesenden der Session werden verglichen (Kosinus-Ähnlichkeit). Ähnlichkeit
  0,35 → 0 %, ab 0,75 → 95 % Sicherheit, Vorschlag ab 50 %. Das kommt als `voice_match`-Hinweis in dieselbe Verteilung
  wie die Vorstellungsrunde. Die Schwellen sind geschätzt und werden mit `chronik stimmen-vergleich` eingestellt.
- **Lernen** („aus Sessions lernen“): nach der Bestätigung durch die SL, vor dem Löschen der Abdrücke. Je Person und
  Session einmal, nur aus der Stimme mit der meisten Redezeit (ab 60 s) und nur, wenn sie zum Profil passt
  (Ähnlichkeit ab 0,45, Schutz vor falscher Zuordnung). Die eigene Aufnahme zählt wie drei Sessions. Nie aus Discord.
- **Löschen** entfernt Profil, Gelerntes, offene Aufträge und eine noch nicht verarbeitete Aufnahme. Ein Ergebnis,
  das danach noch eintrifft, wird verworfen.

## Schnittstelle 0.3.8 und Abgleich mit dem App-Stand (27.09.2026)

- 410 bei gelöschter Hörprobe jetzt mit Fehlerkörper `audio_deleted`, `apiVersion` 0.3.8.
- Die Recap-Eingabe behandelt teilweise verborgene Einträge („Wer weiß was“) wie gm_only: nur Typ und Name.
- `Proposal.hiddenFromMemberIds` wirkt auch bei `update`. Die Liste wird mit der vorhandenen vereinigt, damit
  niemand versehentlich wieder alles sieht.
- Verwaltung im hellen Thema „Pergament“ (Werte aus dem App-Stand, Punkt 23).

## Entscheidungen zur externen Transkription

- **Mistral Voxtral** (`voxtral-mini-latest`, Voxtral Mini Transcribe 2). Deutsch, Sprechertrennung, Zeitstempel je
  Abschnitt, bis zu 100 Begriffe Namenshilfe (`context_bias`), bis 3 h je Anfrage. Das Anfrageformat ist gegen das
  offizielle Mistral-SDK (2.10) geprüft; die Tests nutzen einen nachgebauten Anbieter.
- **Hohe Schwelle:** Freigabe in der `.env` samt Schlüssel, Erlaubnis der SL je Kampagne, Wartezeit ab 24 h, und in
  der ganzen Zeit war kein (nicht pausierter) Worker erreichbar. Vorher zeigt der Status an, ab wann extern
  transkribiert würde. `/info` meldet `externalTranscription: "mistral"` nur mit Schlüssel.
- **Gleicher Weg wie beim Worker:** Namens-Echo- und Halluzinationsfilter, Hörproben (schneidet die Zentrale
  selbst, braucht dafür ffmpeg), Vorstellungsrunde, Audio löschen, `transcriptionEngine: external`, Verbrauch mit
  Kostenschätzung (`EXTERNAL_COST_CENTS_PER_MINUTE`, Standard 0,3).
- **Grenzen:** keine Stimmabdrücke, also keine Wiedererkennung über Stimmprofile und nichts gelernt. Aufnahmen über
  2,5 h werden geteilt, die Stimmgruppen gelten dann je Teil. Fehler: 401/403 → abgebrochen mit Hinweis auf den
  Schlüssel; 429/5xx → neuer Versuch (höchstens 3).
- Die Sprache wird nicht mitgeschickt, weil ältere Voxtral-Versionen Sprache und Zeitstempel nicht kombinieren
  konnten. Die automatische Erkennung reicht.

## Weboberfläche, App-Wünsche und Schnittstelle 0.3.9 (27.09.2026)

- **Offizielle Logos** aus `taleward-ci/logos` in `app/verwaltung/static/marke/` (Zeichen, Wortmarke, gestapelt, App-Icon).
  `taleward-lockup-stacked-inverse.svg` ist abgeleitet (nur Farben wie in `…-inverse.svg`, ohne C2PA-Angaben).
  Favicon, Homescreen-Symbol und `og-image.png` sind aus dem App-Icon erzeugt.
- **Schriften** Alegreya und Alegreya Sans lokal (`static/fonts`, OFL, aus @fontsource wie in der App).
- **Tab „Transkription“** statt „Worker“: lokaler Worker, alle Worker, Schlüssel für weitere
  Worker und die externe Transkription auf einer Seite. `/verwaltung/worker` leitet weiter.
- **Externe Transkription in der Oberfläche:** Anbieter, API-Schlüssel (nur schreibbar, zeigt die letzten 4 Zeichen)
  und Wartezeit. Werte in `server_meta` (`extern.*`) haben Vorrang vor der `.env`. Das wirkt ohne Neustart, weil der
  Hintergrundprozess bei jedem Durchlauf prüft.
- **Zwei Sprachen** (`app/verwaltung/i18n.py`): Der deutsche Text ist der Schlüssel, dazu ein Wörterbuch je Sprache.
  Die Auswahl steht oben, sonst gilt `Accept-Language`, sonst Deutsch. `tests/test_verwaltung2.py` prüft, dass jeder
  Text übersetzt ist und die Platzhalter stimmen. Neue Sprache: Eintrag in `SPRACHEN` plus Wörterbuch.
- **App-Versionen (0.3.9):** Mindestversion, neueste Version, Download-Link und „Was ist neu“ unter Einstellungen.
  `/info` liefert sie. Ist die App älter als die Mindestversion oder schickt sie kein `X-Taleward-App`, antworten alle
  API-Anfragen außer `/info` und `/health` mit **426 `app_outdated`**, auch mit CORS-Kopfzeilen. Der Header ist in
  CORS erlaubt. Der Versionsvergleich rechnet je Stelle (0.10.0 > 0.9.9).
- **Einladungsseite** `/einladung/<CODE>`: Code und „gültig bis“, „In Taleward öffnen“ (`taleward://einladung?url=…`),
  „App herunterladen“ (Link aus den Einstellungen, bei APK mit Android-Hinweis), Link zum Kopieren ohne Skript,
  drei Schritte, Vorschau im Messenger (`og:*`). Kein Kampagnentitel. Ein abgelaufener oder unbekannter Code liefert
  404 mit freundlichem Hinweis.
- **Behoben:** Im Tab-Titel der Einstellungen stand HTML (beim Einfügen einer Karte war auch das Ende des Titelblocks
  getroffen). Ein Test prüft jetzt alle Titel.

## Feste Modellfassungen (27.09.2026)

- `app/modelle.py`: Whisper, Sprechermodell und (für Sprachen ohne torchaudio-Modell) das Ausrichtungsmodell werden
  in fester Fassung geladen. Vorrang hat `FASSUNGEN` im Code, dann `data/modelle.json` des Workers, sonst die schon
  vorhandene bzw. aktuelle Fassung, die dann gemerkt wird. Geladen wird aus dem lokalen Ordner, also ohne Nachfrage
  bei Hugging Face. VAD und das deutsche/englische Ausrichtungsmodell hängen an den Paketversionen.
- Die Fassungen werden beim Start des Workers geprüft und bei Bedarf geladen, nicht erst im ersten Auftrag.
  Sie erscheinen in der Verwaltung und in `chronik modelle`.
- Stimmabdrücke tragen `Modell@Fassung` (Stimmprofil und `speakers.embedding_model`, Migration `c9d5f7a1b3e4`).
  Verglichen wird nur bei gleicher Fassung. Bei einer neuen Fassung wird ein Profil mit „aus Sessions lernen“ nach der
  bestätigten Zuordnung aus dieser Session neu angelernt. Ohne diese Einstellung wird es verworfen (Status `failed`
  mit Bitte um neue Stimmprobe).
- pyannote 4 schickt ohne Abschalten Nutzungsdaten (Audiodauer, Personenzahl) an pyannote.ai. Der Worker setzt
  `PYANNOTE_METRICS_ENABLED=false`.

## Schritt 5: Recap und Vorschläge mit Sprachmodell (27.09.2026)

- **Auswahl** in der Verwaltung (Tab „Zusammenfassung“, `server_meta llm.*` vor der `.env`): aus, Attrappe,
  lokales Modell (Ollama im Worker) oder API (OpenAI-kompatibel; voreingestellt Mistral,
  `mistral-large-latest`). Ohne eigenen Schlüssel nutzt die API bei Mistral den Schlüssel der externen
  Transkription, bei anderen Anbietern nie.
- **`app/sprachmodell.py`** (ohne Datenbank, läuft in Zentrale und Worker):
  - Klienten: `OpenAIKlient` (chat/completions, `json_object`) und `OllamaKlient` (natives `/api/chat` mit `num_ctx`,
    `format: json`; lädt fehlende Modelle selbst, gibt die Grafikkarte danach frei).
  - Ablauf: Übersteigt das Transkript den Kontext, wird es erst in Stücken zu Szenennotizen verdichtet, bei Bedarf
    in mehreren Runden. Danach folgen zwei getrennte Aufrufe, zuerst der Recap, dann die Vorschläge.
  - Vom Bibel-Inhalt geht nur mit, was in der Session vorkommt. Von allen anderen Einträgen gehen nur die Namen mit.
  - `pruefen` übersetzt die Antwort in Vorschläge nach Schnittstelle und verwirft Ungültiges.
- **Spoilerschutz:**
  - Der Recap-Aufruf bekommt nur `recap_eingabe`: öffentliche, niemandem verborgene Einträge ohne gmNotes und keine
    Namen geheimer Einträge.
  - Der Vorschlags-Aufruf bekommt `vorschlag_eingabe`: die ganze Bibel inkl. gmNotes, wie in der Schnittstelle
    festgelegt.
  - Der Notizen-Schritt sieht nur das Transkript.
  - SL-Notizen der Session und Charakter-Hintergründe kommen in keiner Eingabe vor.
  - Übernimmt ein öffentlicher Vorschlagstext Formulierungen aus geheimen Texten, wird er als gm_only mit Hinweis
    gespeichert.
- **Worker:** Er erkennt Ollama (`WORKER_LLM_URL`) beim Start und meldet `info.llm`. Er bekommt
  `summarize`-Aufträge nur, wenn „Lokales Modell“ eingestellt ist. Das Ergebnis liefert er an
  `POST /worker/v1/jobs/{id}/summary-result`, dort prüft die Zentrale es noch einmal. Neue Schlüssel und bestehende
  Worker haben die Fähigkeit `asr,llm` (Migration `d2e8a4c6f0b1`).
- **Status:** Neuer Hinweis `status.no_llm_worker`, wenn „Lokales Modell“ eingestellt ist, aber keiner mit
  Sprachmodell verbunden ist.
- **Verbrauch:** Tokens ein/aus, Kosten aus der Preistabelle (Mistral) oder den Preisen aus der Verwaltung.
- **`chronik recap-probe <ID> [--lokal] [--modell …]`:** Zeigt Recap und Vorschläge für eine vorhandene Session an,
  ohne etwas zu speichern. Zum Vergleichen.
- **Schnittstelle unverändert (0.3.9).**

## Begriffe in Oberfläche und Anleitung (27.09.2026)

Benjamin hat „Worker“ als Begriff gewählt. In Oberfläche, Statusmeldungen, CLI-Ausgaben und Anleitung steht jetzt:

| alt | neu |
|---|---|
| Rechenknecht | Worker |
| Eigener Rechenknecht / Dieser Rechner | Lokaler Worker (auf diesem Server) |
| Rechenknecht auf einem anderen Rechner | Weiteren Worker anbinden |
| Eigener Rechner (Zusammenfassung) | Lokales Modell (Ollama auf einem Worker) |
| Vereins-PC / Rechner mit Grafikkarte | Lokaler Server |
| API | Cloud-API |
| Attrappe | Testmodus (`chronik worker --testmodus`; `--attrappe` geht weiter) |
| Zentrale | Server |
| Externe Transkription (Ersatz) | Externe Transkription (Ausweichlösung) |
| „kein Rechner … verbunden“ (App) | „Zurzeit ist keine Transkription / kein Sprachmodell verfügbar …“ |

Seit dem 27.09.2026 heißt es auch im Code so: `app/worker_prozess.py` (Klasse `WorkerProzess`),
`app/verwaltung/lokaler_worker.py`, Seiten unter `/verwaltung/worker`, Protokoll `data/logs/worker.log`, Arbeitsordner
`data/worker`. Nur `ZENTRALE` und `verarbeite_attrappe` heißen intern noch so.

## Paket 1 vor dem Vereinseinsatz: Konto und Bilder (27.09.2026)

- **Registrierung** (`app/konto.py`):
  - Standard ist „nur mit Einladungscode“ (`registration: invite_only` in `/info`). Umstellen in der Verwaltung
    unter Einstellungen → Neue Konten.
  - Benutzername: 3–64 Zeichen aus Buchstaben, Ziffern, `.`, `-`, `_`, wird klein gespeichert.
  - Passwort: mindestens 8 Zeichen, nicht offensichtlich und nicht gleich dem Namen.
  - Zustimmung zum Datenschutzhinweis und Altersbestätigung stehen mit Zeitpunkt am Konto.
  - Höchstens 10 Versuche je Adresse in 15 Minuten (429 `too_many_requests`).
- **Export** `GET /me/export` (Format `taleward-export/1`): Konto, Anmeldearten, Organisationen, Mitgliedschaften
  mit Charakterangaben, besuchte Kapitel mit Zustimmung, Zustimmungsprotokoll, Kommentare, Stimmprofil-Status (ohne
  Abdruck), Bilder als Links.
- **Konto löschen** `DELETE /me`:
  - 401 `wrong_password`.
  - 409 `last_gm_campaigns` nur, wenn in der Kampagne noch andere sind. Kampagnen ohne andere Mitglieder werden
    samt Dateien mitgelöscht.
  - Das Mitglied bleibt als „Gelöschtes Konto“ stehen (`members.user_id = NULL`, `deleted_at`, Migration
    `e4a7c1d9b2f5`). Der Charaktername bleibt, Bild, Hintergrund, Zustimmung (Widerruf im Protokoll), Lesemarker
    und Stimmprofil werden entfernt.
  - Gelöschte Konten können nicht mehr als anwesend eingetragen, umbenannt oder befördert werden. Sie zählen nicht
    als Mitglied und nicht als Spielleitung.
- **Bilder** (`app/bilder.py`, neue Abhängigkeit Pillow):
  - Jedes Bild wird neu kodiert: EXIF-Drehung anwenden, Metadaten entfernen, JPEG bzw. WebP bei echter Transparenz.
  - Titelbild höchstens 5 MB und 1600 px. Charakterbild höchstens 3 MB: `full` bis 1024 px, `thumb` 256×256 aus
    `cropX/cropY/cropSize` (sonst mittig, außerhalb 400 `invalid_crop`).
  - Gespeichert unter `data/bilder/`. Ein mitgeliefertes Motiv (`coverPreset`) löscht das eigene Titelbild.
- **Migrationen:** Sie laufen jetzt ausdrücklich ohne Fremdschlüsselprüfung. Sonst würde das Neuaufbauen einer
  Tabelle in SQLite abhängige Zeilen löschen.

## Paket 2 vor dem Vereinseinsatz: Kommentare, Ungelesen, Terminabstimmung (27.09.2026)

- **Kommentare** (`app/routers/miteinander.py`, Tabellen `comments`, Migration `f5b8d2e0c3a6`):
  - Öffentlich oder privat. Private sehen nur Absender und Empfänger, auch eine zweite SL nicht.
  - Spieler schreiben privat nur an eine SL (sonst 403).
  - Fremde private Nachrichten und Kommentare unveröffentlichter Kapitel gibt es für Unbefugte nicht (404).
  - Bearbeiten nur eigene (`editedAt`). Löschen eigene, die SL alle, die sie sieht.
  - Leerer Text gibt 400 `comment_empty`, über 4000 Zeichen 413 `comment_too_long`.
  - Kommentare fließen nie ins Sprachmodell (Test).
- **Ungelesen:**
  - `unreadComments` je Kapitel (Liste und Einzelabruf) und `unread.comments` je Kampagne, auch für die SL.
  - Gezählt werden Kommentare anderer, die man sehen darf, seit dem Marker des Kapitels. Fehlt dieser, gilt der
    Chronik-Marker bzw. der Beitritt.
- **Terminabstimmung** (`date_polls`, `date_options`, `date_votes`):
  - Höchstens eine offene je Kampagne (409 `date_poll_open`).
  - Vorschlagen dürfen alle Mitglieder (400 `date_in_past`, 409 `option_exists`), der Vorschlag zählt automatisch als
    „yes“.
  - Einen Termin entfernen darf, wer ihn vorgeschlagen hat, oder die SL.
  - Abstimmen mit `yes`/`maybe`/`no`.
  - Festlegen setzt `nextSessionAt`, Abbrechen setzt den Zustand `cancelled`. `GET` liefert die offene, sonst die
    zuletzt beendete Abstimmung.
  - `datePollNeedsMyVote` ist gesetzt, wenn es einen Termin ohne meine Antwort gibt.
- **Gelöschte Konten:** Ihre Kommentare bleiben stehen, Stimmen werden gelöscht, sie sind keine Empfänger mehr.
  Der Export enthält jetzt Kommentare und Stimmen (`dateVotes`).
- **Hinweis zur YAML:** `VoteAnswer: [yes, maybe, no]` lesen YAML-1.1-Leser als Wahrheitswerte. Gemeint sind Texte,
  der Vertragstest korrigiert das. Wunsch: in der YAML quoten.

## SL-Unterlagen (27.09.2026)

Damit ist die Schnittstelle 0.3.9 vollständig umgesetzt (`app/routers/spaeter.py` ist weg).

- **Hochladen** (`app/unterlagen.py`, Router `app/routers/unterlagen.py`, Tabelle `documents`, Migration
  `a6c9e3f1d4b7`, neue Abhängigkeit pypdf):
  - Erlaubt sind PDF, .docx, .txt und .md bis 50 MB. Geprüft werden Endung und Dateikopf.
  - Der Text wird sofort ausgelesen: PDF je Seite, Word mit Seitenumbrüchen, Text in Abschnitten.
  - Seiten ohne Text (eingescannt) werden übersprungen und in `message` genannt. Ganz ohne Text bleibt der Zustand
    `failed`.
  - Ausgewertet werden höchstens etwa 1,5 Mio. Zeichen.
  - Datei und Text liegen in `data/unterlagen/<id>/`.
- **Auswertung:** Auftrag `document` mit Fähigkeit `llm`, dieselbe Einstellung wie die Zusammenfassung (Cloud-API
  bzw. Testmodus in der Zentrale, lokales Modell im Worker über `document-result`).
  - `sprachmodell.DokumentAblauf`: Stücke mit `[Seite N]` und Bibel-Kontext (Namen, Inhalt samt gmNotes nur für
    erwähnte Einträge).
  - Vorschläge mehrerer Stücke werden zusammengeführt. Ein `create` mit dem Namen eines vorhandenen Eintrags wird zum
    `update`.
  - Welt-Hintergrund, bei mehreren Stücken mit einem Aufruf zum Zusammenfassen.
- **Trennung SL-/Spielerwissen:** Der Server setzt sie beim Speichern durch, unabhängig vom Modell.
  - `handout`: öffentlich, ohne gmNotes.
  - `gm`: alles gm_only.
  - `mixed`: gm_only, `publicSuggested` nur mit Begründung.
  - Ein Update eines öffentlichen Eintrags aus gm/mixed landet komplett in gmNotes.
  - Formulierungen aus markierten Abschnitten (`[SL]`, `[GM]`, `Geheim:`, `Secret:`) im öffentlichen Teil werden
    in gmNotes verschoben.
- **Prüfen und Übernehmen:** Geprüft wird per `PATCH /proposals/{id}` wie bei Sessions. `apply` übernimmt
  angenommene Vorschläge, verwirft offene und hängt auf Wunsch den Welt-Hintergrund an. `retry` geht nur bei
  `failed`. Löschen entfernt Datei, Text, Vorschläge und Aufträge, übernommene Einträge bleiben.
- **Verwaltung:** Die Warteschlange zeigt „SL-Unterlage“ mit Titel.
- **`chronik unterlage-probe <datei> --art gm|mixed|handout [--kampagne ID] [--lokal] [--modell …]`:** zeigt die
  Vorschläge, ohne zu speichern.

## Einrichtungsassistent, Sicherung, Kostenlimit, Worker koppeln (27.09.2026)

Aus dem Usability-Check , Stufen 1 und 2.

- **Einrichtungscode statt admin/admin** (`app/einrichtung.py`):
  - Ohne Verwalter erzeugt der Server beim Start einen Code (`server_meta einrichtung.code`) und schreibt den Link
    ins Protokoll. `chronik einrichtungscode [--neu]` zeigt ihn an.
  - Falsche Codes werden wie falsche Passwörter gebremst.
  - Ältere Installationen mit einem noch vorhandenen admin/admin funktionieren weiter, neue bekommen keins mehr.
- **Assistent** (`app/verwaltung/assistent.py`, `templates/assistent.html`):
  - Schritte: Verein → Betriebsart (`cloud` | `lokal` | `beides`, `server_meta betrieb.art`) → Zusammenfassung →
    Datenschutz → Sicherung → Spielleitung.
  - Mistral- und Hugging-Face-Schlüssel werden beim Speichern geprüft (`app/schluessel.py`). Ist der Anbieter nicht
    erreichbar, wird trotzdem gespeichert, mit Hinweis.
  - Die Übersicht zeigt eine Checkliste aus dem tatsächlichen Zustand (`einrichtung.stand`), bis alles erledigt oder
    ausgeblendet ist.
- **Betriebsart „Nur Cloud“:** externe Transkription ohne Wartezeit (`extern.after_hours = 0`, jetzt erlaubt).
  Neue Kampagnen haben dann `allowExternalTranscription = true`. Der Prozess prüft jetzt jede Minute statt alle
  5 Minuten.
- **Kostenlimit** (`app/kosten.py`): Monatssumme der Cloud-Kosten aus `usage_log` (engine external). Ist
  `kosten.limit_cent` erreicht, bleiben Cloud-Transkription, Cloud-Zusammenfassung und Unterlagen liegen, mit
  Statushinweis `status.cost_limit`. Anzeige und Einstellung in der Verwaltung.
- **Datenschutz-Vorlage** (`app/datenschutz.py`): aus den Einstellungen befüllt. Veröffentlichen erst, wenn keine
  `[…]`-Platzhalter mehr drin sind. Öffentlich unter `/datenschutz`, `privacyPolicyUrl` zeigt darauf. Keine
  Rechtsberatung, das steht auch im Assistenten.
- **Sicherung** (`app/sicherung.py`):
  - ZIP mit `chronik.db` (SQLite-Backup-API), `bilder/`, `unterlagen/` und `manifest.json`.
  - Automatisch aus der Wartung, wenn die letzte älter als 24 Stunden ist. Aufbewahrung in Tagen, die neuesten
    drei bleiben immer. Optional ein zusätzlicher Ordner.
  - `chronik sicherung`, `chronik wiederherstellen <zip>` (vorher wird der jetzige Stand gesichert, danach
    migriert).
- **Worker koppeln** (`app/koppeln.py`): `POST /worker/v1/pair` tauscht einen 15-Minuten-Code gegen einen Schlüssel
  (Versuche je Adresse begrenzt). `chronik worker --koppeln CODE --server URL` schreibt ihn in die `.env`.
- **Hugging Face zentral:** `server_meta hf.token`, Worker holen ihn über `GET /worker/v1/config`, wenn ihre `.env`
  keinen hat. So laden sie auch neue Modellfassungen nach Updates.
- **Geheimnis automatisch:** Fehlt `JWT_SECRET`, erzeugt der Server `data/geheimnis.txt` (0600).
- **Migrationen:** keine neuen Tabellen, alles in `server_meta`.

## Laufender Betrieb: Benachrichtigungen, QR-Code, Passwort-Link, Übergabe (27.09.2026)

- **`app/benachrichtigung.py`:** ntfy (JSON-Veröffentlichung, optional Bearer-Token) und E-Mail (smtplib, STARTTLS,
  SSL oder ohne).
  - Die Wartung ruft `pruefen(db)` bei jedem Durchlauf auf.
  - Arten:
    - `worker_fehlt`: Aufträge `queued`/`local` älter als N Stunden, und kein Worker mit passender Fähigkeit ist
      online. Gilt nicht bei „Nur Cloud“.
    - `fehlschlag`: seit dem letzten Stand; nur Auftragsart und Fehlercode, nie Inhalte.
    - `kosten_80` / `kosten_100`: je einmal pro Monat.
    - `sicherung`: Fehler aus der Wartung oder älter als 48 h.
    - `speicher`: weniger als 2 GB frei.
  - Dauerzustände werden höchstens alle 24 h wiederholt.
  - Einstellungen liegen in `server_meta` `melden.*`; Token und Passwort sind nur schreibbar.
  - Die Checkliste hat den Punkt „Benachrichtigungen“.
- **QR-Code auf der Einladungsseite:** `segno` erzeugt eine data:-SVG ohne Skript (CSP bleibt). Immer dunkel auf
  hellem Grund, damit Kameras ihn auch im dunklen Thema lesen.
- **Passwort-Link (`app/passwortlink.py`):**
  - Die Verwaltung erzeugt ihn unter Konten. Er ist einmalig und 24 h gültig und erscheint als Link und QR-Code.
  - Gespeichert wird nur der SHA-256-Wert in `server_meta` `pwlink.<userId>`. Ein neuer Link ersetzt den alten.
  - `/passwort/<token>` ist eine öffentliche Seite mit Sicherheitskopfzeilen. Nach dem Setzen steigt
    `token_version`, das Konto ist also überall abgemeldet.
  - „Passwort selbst setzen“ nutzt dieselbe Funktion.
- **`/verwaltung/uebergabe`:**
  - Verträge zur Auftragsverarbeitung (Hosting, Mistral), mit Links und dem Datum des Abschlusses (`av.*`).
  - Checkliste „Verwaltung übergeben“.
  - Was hinterlegt ist, nur als ja/nein.
- **Vorschlag an die App (nicht umgesetzt):** Passwort-Link durch die SL.

## Sprechermodell über den Server (27.09.2026)

- **`app/modellablage.py`:** Der Server holt `pyannote/speaker-diarization-community-1` einmal in fester Fassung.
  - Quellen:
    - Der Taleward-Spiegel (`MODEL_MIRROR_URL`), aber nur, wenn die SHA-256 seiner `manifest.json` in `SPIEGEL`
      steht.
    - Sonst Hugging Face mit dem Zugang aus der Verwaltung, per httpx: API `revision`/`tree`, `resolve`, und die
      LFS-Prüfsummen werden verglichen.
  - Ablage in `data/modellablage/<repo>/<fassung>/` mit `manifest.json` und `NOTICE.txt` (Namensnennung nach
    CC-BY-4.0). Die gemerkte Fassung steht in `server_meta` `modell.<repo>`.
  - Die Wartung holt das Modell automatisch, sobald es gebraucht wird (Betriebsart lokal/beides oder ein Worker
    existiert). Nach einem Fehler erst wieder nach einer Stunde, oder sofort über „Jetzt auf den Server laden“.
- **Worker-Protokoll:**
  - `GET /worker/v1/models?repo=…` liefert das Verzeichnis, `GET /worker/v1/models/file?repo&fassung&pfad` die Datei.
    Nur Dateien aus dem Verzeichnis werden ausgeliefert.
  - `/worker/v1/config` meldet `models`. Liegt das Modell auf dem Server, ist `hfToken` `null`: Der Zugang bleibt
    auf dem Server.
- **Worker (`modelle.vom_server`, `ServerQuelle`):**
  - Er lädt in `data/modelle/<repo>/<fassung>/` und prüft jede Datei per SHA-256. Erst ein vollständiger Ordner
    zählt.
  - Eine Fassung, die im Code festgelegt ist und nicht zum Server passt, führt zu einer klaren Meldung.
  - `HF_TOKEN` braucht er nur noch als Ausweichweg.
- **CLI:** `chronik modell-holen`; `chronik modell-spiegel <ordner>` erstellt den Spiegel für das Taleward-Projekt und
  gibt die Zeile für `SPIEGEL` aus.
- **Passwort-Link durch die SL:** bewusst nicht umgesetzt.

## Schnittstelle 0.3.10 und 0.4.0 (27.09.2026)

- **0.3.10:**
  - `Campaign.allowCloudSummary`:
    - Die Zentrale nimmt Zusammenfassungs- und Unterlagen-Aufträge über die Cloud-API nur für Kampagnen mit Erlaubnis
      (`queue.claim(darf=cloud_erlaubt)`). Die anderen warten mit `status.cloud_summary_not_allowed`.
    - Neue Kampagnen in „Nur Cloud“ bekommen die Erlaubnis gleich. Die Migration setzt sie für bestehende Kampagnen
      nur in dieser Betriebsart.
  - `ServerInfo.externalTranscriptionMode` (`primary` bei „Nur Cloud“, sonst `fallback`) und `cloudSummary`.
  - `Member.deletedAt`.
- **0.4.0 Anmeldung:**
  - `app/mail.py` versendet über den Mailserver der Benachrichtigungen.
  - `app/einmal.py` verwaltet Einmal-Werte (SHA-256, Ablauf, Tabelle `einmal_tokens`).
  - `app/anmeldedienste.py` für Google, Microsoft und Apple über OIDC (id_token wird mit JWKS geprüft, dazu Aussteller,
    Empfänger, Nonce; Apple-Secret als ES256-JWT) und Discord über OAuth2 plus Profil.
  - `app/routers/anmeldung.py` enthält die API und die Webseiten `/auth/oidc/{dienst}/callback` (GET, bei Apple POST)
    und `/konto/email/<token>`. Die Bestätigung braucht einen Klick.
  - E-Mails vom Dienst gelten nur, wenn er sie als bestätigt meldet. Bei Microsoft gilt das nur für private Konten.
  - Eine schon vergebene E-Mail wird nie automatisch verbunden (`email_in_use`).
  - „Passwort vergessen“:
    - Die Antwort ist immer 202.
    - Die Mail geht im Hintergrund raus, höchstens 3 pro Konto und Stunde.
    - Der Link gilt 1 Stunde (Seite `/passwort/<token>`).
  - `PUT /me/password`:
    - Andere Geräte sind danach abgemeldet.
    - Das eigene Token bleibt gültig über `jti` im JWT und `users.token_ausnahme`.
  - **Verwaltung:**
    - Neuer Tab „Anmeldung“ mit Anleitungen und Rückleitungsadressen; Schlüssel sind nur schreibbar.
    - „Öffentliche Adresse des Servers“ unter Einstellungen.
    - Anmeldearten in der Kontenliste.
  - **Datenschutz-Vorlage:** Absätze für die E-Mail und je eingeschaltetem Dienst.
  - Migration `b7d0f4a2e8c9`. Neue Abhängigkeit: `pyjwt[crypto]`.
