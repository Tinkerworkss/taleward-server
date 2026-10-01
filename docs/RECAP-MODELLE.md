# Recap-Modelle

Welches Sprachmodell ein Worker für Recaps und Vorschläge nimmt, wählt er selbst, solange in der Verwaltung unter
„Zusammenfassung → Lokales Modell“ `auto` steht (Standard ab Server 0.4.36). Die Werte stehen in `app/recapmodell.py`.

| Nutzbarer Grafikspeicher | Modell | höchster Kontext |
|---|---|---|
| ab 15 GB | `gemma4:12b` | 32.768 |
| 12 bis 15 GB | `gemma4:12b` | 24.576 |
| 6 bis 12 GB | `gemma4:e4b` | 20.480 |
| unter 6 GB | `gemma4:e4b` | 12.288 |
| keine messbare Karte | `gemma4:e4b` | 20.480 |

„Nutzbar“ heißt: der Grafikspeicher der Karte, höchstens die Grenze, die in der Worker-App eingestellt ist.

## Kontext nach Länge der Session

Der Kontext richtet sich nach dem Auftrag: geschätzte Tokens von Transkript und Bibel plus 5.000 für Anweisung und
Antwort, gerundet auf 2.048, mindestens 12.288, höchstens der Wert der Stufe. Passt das Transkript ganz hinein, schreibt
das Modell den Recap direkt daraus. Sonst verdichtet es zuerst zu Szenennotizen. Das kostet merklich Qualität.

Ein fest eingetragenes Modell bekommt ebenfalls einen Kontext nach Länge, höchstens die „Höchste Kontextgröße“ aus der
Verwaltung.

## Messung (01.10.2026)

Eine Session mit 47 Minuten Spiel (~14.000 Tokens Transkript), bewertet an 20 Prüfpunkten einer Referenz:

| Karte | Modell | Kontext | Dauer | Prüfpunkte |
|---|---|---|---|---|
| 8 GB | ministral-3:8b (bisher) | 12.288 | 7,5 min | ≈ 6 |
| 8 GB | gemma4:e4b | 20.480 | 54 s | ≈ 14,5 |
| 8 GB | gemma4:12b | 20.480 | 4,2 min (weicht in den Arbeitsspeicher aus) | ≈ 14 |
| 16 GB | gemma4:e4b | 20.480 | 69 s | ≈ 12 |
| 16 GB | gemma4:12b | 32.768 | 82 s | ≈ 15,5 |

Zwischen zwei Läufen desselben Modells schwankt das Ergebnis um einige Punkte. Für einen Wechsel des Standards zählen
deshalb mehrere Läufe (`chronik modellvergleich`).

## Ollama

Die gemma4-Modelle brauchen Ollama ab 0.35.0. Ein älteres Ollama meldet der Worker mit einer klaren Fehlermeldung,
statt den Auftrag still scheitern zu lassen.

## Harte Regeln für Vorschläge

Unabhängig vom Modell verwirft der Server (`sprachmodell.pruefen`):

- neue Einträge für Spielercharaktere und Änderungen an Einträgen, die einen Spielercharakter meinen
- Sätze über den Spieltisch statt über die Spielwelt („in dieser Session“, „die SL hat …“, „Spieler“), in Detail und
  gmNotes. Bleibt vom Detail nichts übrig, fällt der Vorschlag weg.
