# Systembegriffe für die Transkription

Die Dateien in diesem Ordner haben drei getrennte Aufgaben:

- **Stufe A:** kleine, priorisierte Liste für Whisper-Hotwords.
- **Stufe B:** großes Wörterbuch für bekannte Schreibweisen und Korrekturvorschläge. Stufe-B-Begriffe werden nur dann als Hotword hochgestuft, wenn sie bereits im Kampagnenkontext vorkommen.
- **Verhörer:** systembezogene Korrekturhinweise. Sie werden nicht blind automatisch ersetzt.
- **Erkennungsnamen:** Aliasnamen, mit denen bei `system=other` der freie Systemname einer Liste zugeordnet werden kann.

Format:

```text
# Erkennungsnamen: shadowrun, sr6, sechste welt
# Stufe A
Chummer
Nuyen
# Stufe B
Dunkelzahn
# Verhörer
Tschummer => Chummer
```

Alte Dateien ohne Abschnittsüberschriften bleiben kompatibel und werden vollständig als Stufe A gelesen.

## Priorität der aktiven Whisper-Hotwords

Der Server baut pro Kampagne eine begrenzte Liste (maximal 80 Begriffe / ca. 700 Zeichen):

1. manuell korrigierte oder ergänzte Begriffe,
2. wichtige Kampagnen-/Charakternamen,
3. im Kampagnenkontext gefundene Stufe-B-Begriffe,
4. Stufe A,
5. übrige Kampagnennamen.

Ein von der SL explizit entfernter Begriff wird auch dann nicht über Stufe A/B wieder eingefügt.

## Neue Systeme

Neue Dateien können serverseitig bereits über `system=other` + freien `systemName` erkannt werden, wenn passende Erkennungsnamen angegeben sind. Die App muss das System nicht zwingend schon als eigene Auswahl anbieten.
