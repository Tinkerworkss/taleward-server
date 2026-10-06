# Ledger v2 / 0.5.0 – experimenteller Mini-Benchmark

Basis: `aae38c72c0e22f52dc814be2b99e9b4b1722f3ce` (0.4.58).
Der neue Core steht in `app/ledger_v2.py`. Er wird ausschließlich durch den neuen
Mini-Benchmark aufgerufen. Recap, offene Fäden, Bibel und der bestehende
`modellvergleich --nur-ledger` verwenden weiterhin ihren bisherigen Ablauf.

**Status: Prototyp, kein gemessener E4B-Qualitätserfolg.** In der Entwicklungsumgebung
ist kein Ollama erreichbar. Automatisierte Tests verwenden ausdrücklich nachgebildete
Modellantworten. Sie belegen Softwareverhalten, nicht den Recall eines Sprachmodells.

## Ein kurzer Lauf auf dem Rechner mit E4B

Ollama bzw. den lokalen Sprachmodell-Dienst der Worker-App starten. Das Modell muss
bereits installiert sein. Weder WhisperX noch Audio, Serverlogin oder HF-Token sind nötig.
Im Checkout:

```powershell
uv run taleward-ledger-mini --modell gemma4:e4b --kontext 24576 --hardware "RTX 3060 Ti 8GB / CUDA"
```

Ohne Checkout, für den veröffentlichten Testbranch:

```powershell
uvx --python 3.11 --from "https://github.com/Tinkerworkss/taleward-server/archive/refs/heads/test/0.4.48-recap-plan-relations.zip" taleward-ledger-mini --modell gemma4:e4b --kontext 24576 --hardware "RTX 3060 Ti 8GB / CUDA"
```

Für einen reproduzierbaren Vergleich die Branch-URL durch
`https://github.com/Tinkerworkss/taleward-server/archive/<Commit-SHA>.zip` ersetzen.
Optional: `--ollama http://127.0.0.1:11435` oder `--ziel C:\Taleward-Test`.
Der Standardlauf verarbeitet **acht Originalfenster und zwölf synthetische Varianten**,
insgesamt 260 Quellsegmente. Es gibt keinen Vollsession-Schalter.

```powershell
uv run taleward-ledger-mini --inspect
```

`--inspect` prüft nur den Corpus und setzt niemals das Modell-Gate auf bestanden.
Exitcode: `0` bei bestandenem Mini-Gate (bzw. gültigem `--inspect`), `1` bei gemessenem
verfehltem Gate, `2` bei Start-/Umgebungsfehlern.

## Was sich an der Architektur ändert

1. **Originalsegmente:** eigene stabile IDs innerhalb einer Source-Revision; keine
   Zusammenlegung aller unmittelbar aufeinanderfolgenden SL-Segmente. Die Originaltexte,
   Uhrzeiten und auch falsche Sprecherlabels bleiben erhalten.
2. **Lokale Fenster:** Turn-Grenzen, kleine Überlappung und begrenzte Erweiterung über
   Frage-Antwort-Grenzen. Das ist eine Heuristik, keine vollständige Diskursanalyse.
3. **Pass A:** Evidenzstellen plus kurze vorläufige Lesart. Keine finale Ontologie,
   keine Relevanzbewertung für Recap/Bibel/offene Fäden. Kandidatenüberlauf wird sichtbar.
4. **Pass B:** bis zu sechs Kandidaten pro begrenztem Auftrag, jeder mit eigenem kleinen
   Originalfenster. Explizite Rollen, Zustandsänderungen, Epistemik und Modalität.
5. **Validierung:** gültige Quellen, wörtliche Mention-Belege, erforderliche Rollen,
   keine Tischrollen als Weltfiguren, konsistente Übergaberichtung in `possession`.
6. **Eskalation:** nur beanstandete vorhandene Kandidaten; höchstens zwei zusätzliche
   lokale Aufrufe insgesamt. Unaufgelöste Aussagen bleiben unter `unresolved`, außerhalb
   des akzeptierten Ledgers. Kein flächendeckender Review, kein neuer Faktenpass.
7. **Ledger:** stabile Event-/Semantic-Fingerprints, exakte Duplikate auf denselben
   Quellen entfernen, Quellen und Entscheidungen auditierbar halten. Ein übergebener
   vorheriger Snapshot wird tief kopiert, seine Events bleiben erhalten.

`mentionId` verweist auf die konkrete Äußerung. Die benannte Entity-Zuordnung bleibt
eine nachvollziehbare lokale Modellentscheidung; es gibt noch keinen globalen
Entity-Merger. Keine destruktive Zusammenführung von Figuren.

`stateHistory` enthält jede Zustandsbehauptung mit Source-Zeit, optionaler Ereigniszeit,
Epistemik und Modalität. Abweichende Werte ergeben zunächst einen offenen Konflikthinweis.
Dieser Prototyp unterscheidet noch nicht zuverlässig Weltänderung, Retcon und Korrektur
und behauptet keinen automatisch kanonischen aktuellen Zustand.

## Wo die Übergabe präzisiert wurde

- **Strukturkonsistenz ist keine Wahrheit:** Ein vollständig invertierter Transfer kann
  Rollen und Zustandsänderung konsistent falsch abbilden. Ein eigener Regressionstest
  zeigt diese Grenze ausdrücklich. Die semantische Prüfung bleibt Aufgabe des Goldtests
  und gegebenenfalls einer Sichtprüfung der Originalquelle.
- **Strengerer v2-Scorer:** Der alte Harness akzeptiert bei Rettung/Behandlung die
  Beteiligten im Zeitfenster ohne entsprechenden Handlungstyp und nutzt teils symmetrische
  Teilworttreffer. v2 prüft Predicate, Rollen, Epistemik/Modalität, konkrete Beleggruppen
  und exakte Aliasse für Namen und Statuswerte. `not dead` trifft nicht auf `dead`.
- **Gold ist versioniert:** Ein Schwur darf das Zustandsfeld `goal` oder `obligation`
  verwenden. Die Gefallensschuld wird beim Schuldner und mit dem richtigen Gläubiger
  geprüft. Eine belegte Zuordnung Frau → Eigenname ist zulässig. Diese Änderungen stehen
  im Audit der jeweiligen Goldfakten, nicht in den Produktionsregeln.
- **Unsicherheit ist kein technischer Abbruch:** Vollständig bearbeitete, aber ungelöste
  Kandidaten bleiben sichtbar und können zu verfehltem Recall führen. Sie werden nicht
  als wahre Fakten gespeichert. Transportfehler, abgeschnittene Antworten, Budgetabbruch
  oder Kandidatenüberlauf kennzeichnen den Lauf dagegen als unvollständig.

Der unveränderte v1-Goldstandard läuft zusätzlich über einen rein deterministischen
Legacy-Adapter (`legacy-harness.json`). Relevanzwerte werden dabei nicht erfunden.
v1- und v2-Scores sind wegen der geänderten Prüfsemantik **nicht direkt austauschbar**.

## Corpus und Messung

`app/ledger_fixtures/mini_v2.json` enthält ausschließlich Benchmarkmaterial:

- unveränderte Originalsegmente aus `Nostria-0.4.58-E4B.zip / transkript.txt`;
- zehn positive Goldfakten und dieselben fünf verbotenen Behauptungen;
- drei synthetische Varianten je Fehlerklasse: Authority, Coreference, Transfer,
  wechselnde NPCs unter gleichem SL-Label (15 zusätzliche positive Fakten);
- Fingerprint der vollständigen Quellrevision, SHA-256 der Originaldatei, Fingerprints
  der Fenster und Auditbegründungen. Der gemeldete manuelle Videobefund aus der Übergabe
  ist als solcher vermerkt; er wurde hier nicht erneut manuell geprüft.

Goldfakten, Erwartungen, Fallnamen und Bewertungsregeln gelangen nie in die Modellprompts.
Es gibt keine namens- oder sessionspezifische Produktionslogik.
Die auswählbaren Originalfenster sind gezielte Fehlerproben. Sie messen noch nicht,
ob ein automatischer Fensterplaner über eine ganze Session dieselbe Abdeckung erreicht.

`bericht.json` trennt:

- **evidenceLocated:** Ein Kandidat zitiert die erforderlichen Quellgruppen. Das ist
  eine Abdeckungsmessung, keine Aussage über die Richtigkeit seines Freitexts.
- **factsMatched:** Ein einzelnes akzeptiertes Event erfüllt die vollständige Relation
  und zitiert die entsprechenden Originalbelege.
- **compiledGivenEvidence:** Erfolgreiche Fakten bei vorhandener Kandidatenevidenz.
- **Non-Claims:** Verbotene Relationen unter den akzeptierten Events.

Jeder Fall erhält eine `case-NN.json` mit Quellen, Kandidaten, Entscheidungen,
abgelehnten/ungeklärten Aussagen, Rohantworten, Fingerprints und Verbrauch. Der Bericht
wird nach jedem Fall fortgeschrieben; unterbrochene Läufe verlieren ihre bisherigen Daten
nicht. Modell-Digest, Parser-/Corpus-Fingerprint, Ollama-Version, Kontext, Seed,
Temperatur und Hardwarelabel stehen im Bericht. Vollständige Ein-/Ausgangstoken und
Dauer sind je Aufruf im Fallexport enthalten.

## Gate und Leistungsbudget

Ein späterer Volltest ist erst zulässig, wenn der **echte** Modelllauf alle Fälle
bearbeitet hat, mindestens **8/10 reale Fakten** liefert und **0/5 Non-Claim-Verstöße**
aufweist. Zusätzlich: Quellenintegrität, keine Tischrollen, keine exakten Duplikate,
keine technischen Abbrüche, keine synthetischen Non-Claim-Verstöße und höchstens
**50 Modellaufrufe**. Synthetischer positiver Recall wird getrennt ausgewiesen.

Dieses Gate startet keinen Volltest automatisch und ist kein Produktionsfreigabesiegel.
Insbesondere misst ein selektiver Gold-Corpus keine Gesamtpräzision: zusätzliche,
nicht abgedeckte Halluzinationen müssen anhand der Fallexporte geprüft werden.

Der Ollama-Benchmark macht pro gezähltem Aufruf genau einen Generierungsversuch.
Keine versteckten Reparaturversuche, kein automatischer Modelldownload. Das vollständige
50-Aufruf-Limit gilt über alle 20 Fälle einschließlich lokaler Eskalationen.
Ein Mini-Lauf ist kein Laufzeitnachweis für die vollständige Nostria-Session. Sobald die
automatische Fensterbildung oder Compiler-Stapel das Budget sprengen, endet ein Lauf
ausdrücklich unvollständig, statt unbemerkt Fakten wegzulassen.

## Entwicklungsprüfung

Verifikation dieser Iteration: Der Gesamtlauf erreichte zunächst 492 bestandene
Tests und zwei Fehler. Die im Basisstand fehlende Laufzeitkante für `jsonschema`
in `uv.lock` wurde ergänzt; der KI-Abhängigkeitsexport stimmt danach bytegenau
mit `engine-requirements.txt` überein. Der andere Fehler betraf ausschließlich
das fehlende SOCKS-Paket der lokalen Testumgebung und wurde dort behoben.
Beide fehlgeschlagenen Prüfungen wurden erfolgreich nachgeprüft. Die neuen v2-
und bestehenden Harness-Tests wurden nach den letzten Änderungen erneut ausgeführt.
Das Wheel enthält CLI und Corpus. Ein realer E4B-Lauf steht weiterhin aus.

```bash
uv run pytest tests/test_ledger_v2.py tests/test_ledger_harness.py -q
uv run pytest -q
```

Kein neuer Workflow, kein Release-Tag, kein Merge nach `main`. Die Iteration wird als
ein atomarer Commit auf dem vorhandenen Testbranch bereitgestellt.
