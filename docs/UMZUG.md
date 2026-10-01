# Umzugsdatei `taleward-kampagne/1`

Ab Server 0.4.8 (Schnittstelle 0.4.8). Die Spielleitung packt eine Kampagne als ZIP-Datei und legt sie auf einem
anderen Taleward-Server wieder an. Die App reicht die Datei nur weiter und liest sie nicht. Code: `app/umzug.py`.

## Aufbau der ZIP-Datei

```
kampagne.json
bilder/cover.jpg              Titelbild (falls eigenes Bild)
bilder/p1-full.jpg            Porträt eines Platzes (nur mit Zustimmung), .jpg oder .webp
bilder/p1-thumb.jpg           Ausschnitt dazu
unterlagen/d1/datei.pdf       Unterlage der SL bzw. Charakterbogen (nur mit Zustimmung)
```

Andere Pfade lehnt der Import ab (`import_unsafe`), ebenso `..`, absolute Pfade, verschlüsselte Einträge und eine
entpackte Gesamtgröße über dem Dreifachen der erlaubten Dateigröße.

## `kampagne.json`

Alle Zeiten in UTC im Format ISO 8601. Querverweise innerhalb der Datei laufen über Schlüssel (`p1`, `s1`, `e1`,
`d1`), nie über IDs eines Servers; beim Import entstehen neue IDs.

| Feld | Inhalt |
|---|---|
| `format` | immer `taleward-kampagne/1` |
| `apiVersion` | Schnittstelle des packenden Servers, z. B. `0.4.8`. Ist sie neuer als die des Zielservers: `import_version` |
| `exportedAt` | Zeitpunkt |
| `source` | `name`, `url` (öffentliche Adresse, falls eingestellt), `campaignId` (alte ID) – nur zur Information |
| `campaign` | `title`, `description`, `worldInfo`, `language`, `system`, `systemName`, `coverPreset`, `coverImage` (Pfad in der ZIP), `hotwords` (Namenshilfe der SL: `extra`, `entfernt`, `ignoriert`), `nextSessionAt`, `createdAt` |
| `seats[]` | Plätze, siehe unten |
| `sessions[]` | nur veröffentlichte Kapitel, siehe unten |
| `entries[]` | die ganze Bibel inklusive `gm_only`, siehe unten |
| `documents[]` | `key`, `title`, `fileName`, `kind` (`handout`, `gm`, `mixed`, `character_sheet`), `file` (Pfad in der ZIP), `uploadedBySeat`, `createdAt` |

**`seats[]`** – je Mitglied ein Platz:

- immer: `key`, `role`, `characterName`, `characterId` (bei Ehemaligen `null`), `former`, `leftAt` (bei Ehemaligen),
  `consented`
- nur mit Zustimmung (`consented: true`): `character` mit `version`, `status`, `nickname`, `summary`, `backstory`,
  `system`, dazu `portrait` und `portraitThumb` (Pfade in der ZIP)

**`sessions[]`**: `key`, `number`, `title`, `playedAt`, `publishedAt`, `attendees[]` (`{seat}` oder `{guest}`),
`recap` (`title`, `text`, `openThreads`, `editedAt` – ohne Prüfteil), `gmNote`, `comments[]` (`author`, `recipient`
oder `null`, `text`, `createdAt`, `editedAt`). Öffentliche Kommentare nur von Plätzen mit Zustimmung, private nur,
wenn beide zugestimmt haben.

**`entries[]`**: `key`, `type`, `name`, `summary`, `gmNotes`, `status`, `holderSeat`, `visibility`,
`hiddenFromSeats[]`, `pcCharacterId`, `origin` (mitgebrachte Welt: `characterId`, `entryId`, `version`, `seat` – nur
mit Zustimmung der Urheberin), `createdAt`, `updatedAt`, `publicChangedAt`, `mentions[]` (`session`, `note`). Bei
`pc`-Einträgen steht die Kurzbeschreibung nur mit Zustimmung des Platzes in der Datei.

## Nie in der Datei

Anzeige- und Benutzernamen, E-Mail-Adressen, Konten, Einwilligungen und das Zustimmungsprotokoll, Stimmprofile,
Transkripte, Hörproben, Audio, der Prüfteil des Recaps, Vorschläge (offen oder entschieden), Terminabstimmungen,
Lesemarker, Nutzung und Kosten, Hinweise an die SL, unveröffentlichte Kapitel.

## Import

- Neue Kampagne mit neuen IDs, aktiv. Die importierende Person wird einzige SL.
- Alle Plätze werden offene Plätze (`openSeat`), Ehemalige kommen mit `leftAt`. Kapitelnummern bleiben.
- Anwesenheiten bleiben, aber ohne Einwilligung – Einwilligungen beginnen auf dem neuen Server bei null.
- Bilder werden neu kodiert, Unterlagen wie beim Hochladen geprüft und ihr Text neu ausgelesen. Unbrauchbare Bilder
  oder Unterlagen werden ausgelassen, der Rest kommt trotzdem.
- Freigaben für externe Transkription und Cloud-Zusammenfassung gelten nur für einen Server und kommen nicht mit.
- Fehler beim Import (`ImportStatus.message`): `import_format`, `import_too_large`, `import_unsafe`, `import_version`.
