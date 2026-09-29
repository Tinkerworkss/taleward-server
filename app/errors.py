"""Einheitliches Fehlerformat {code, message}. Alle Nutzertexte auf Deutsch und Englisch an einer Stelle.

Die Sprache kommt aus dem Header Accept-Language (de | en), Standard und Rückfall: de.
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Schlüssel → (Deutsch, Englisch). Platzhalter in {geschweiften Klammern}.
MESSAGES: dict[str, tuple[str, str]] = {
    # Anmeldung
    "not_authenticated": ("Bitte melde dich an.", "Please sign in."),
    "token_invalid": ("Deine Anmeldung ist abgelaufen. Bitte melde dich neu an.",
                      "Your session has expired. Please sign in again."),
    "invalid_credentials": ("Benutzername oder Passwort ist falsch.", "Wrong username or password."),
    "registration_closed": ("Auf diesem Server können sich keine neuen Konten selbst registrieren. "
                            "Bitte wende dich an die Betreiber.",
                            "New accounts cannot be registered on this server. Please contact the operators."),
    "consent_required": ("Bitte bestätige den Datenschutzhinweis und dein Alter.",
                         "Please accept the privacy notice and confirm your age."),
    "weak_password": ("Das Passwort braucht mindestens 8 Zeichen und darf nicht leicht zu erraten sein.",
                      "The password needs at least 8 characters and must not be easy to guess."),
    "username_taken": ("Diesen Benutzernamen gibt es schon. Bitte wähle einen anderen.",
                       "This username is already taken. Please choose another one."),
    "username_invalid": ("Der Benutzername darf 3 bis 64 Zeichen haben: Buchstaben, Ziffern, Punkt, Minus, Unterstrich.",
                         "The username may have 3 to 64 characters: letters, digits, dot, hyphen, underscore."),
    "too_many_requests": ("Zu viele Versuche. Bitte warte ein paar Minuten.", "Too many attempts. Please wait a few minutes."),
    "wrong_password": ("Das Passwort ist falsch.", "The password is wrong."),
    # Anmeldung 0.4.0
    "invalid_email": ("Das sieht nicht nach einer E-Mail-Adresse aus.", "This doesn't look like an e-mail address."),
    "email_taken": ("Diese E-Mail-Adresse gehört schon zu einem anderen Konto auf diesem Server.",
                    "This e-mail address already belongs to another account on this server."),
    "mail_unavailable": ("Dieser Server kann gerade keine E-Mails verschicken. Bitte wende dich an die Verwaltung.",
                         "This server can't send e-mails right now. Please contact the administration."),
    "ticket_invalid": ("Die Anmeldung ist abgelaufen oder wurde schon benutzt. Bitte noch einmal versuchen.",
                       "The sign-in has expired or was already used. Please try again."),
    "provider_unknown": ("Diese Anmeldung ist auf diesem Server nicht eingerichtet.",
                         "This sign-in method is not set up on this server."),
    "provider_linked_elsewhere": ("Dieses Konto beim Anmeldedienst ist schon mit einem anderen Taleward-Konto verbunden.",
                                  "This sign-in account is already connected to another Taleward account."),
    "last_login_method": ("Das ist deine einzige Anmeldung. Lege zuerst ein Passwort fest oder verbinde einen anderen "
                          "Dienst.", "This is your only way to sign in. Set a password or connect another service "
                                     "first."),
    "registration_token_invalid": ("Die Anmeldung ist abgelaufen. Bitte noch einmal mit dem Dienst anmelden.",
                                   "The sign-in has expired. Please sign in with the service again."),
    "oidc_failed": ("Die Anmeldung beim Dienst hat nicht geklappt. Bitte noch einmal versuchen.",
                    "Signing in with the service didn't work. Please try again."),
    "confirmation_mismatch": ("Bitte zur Bestätigung deinen Benutzernamen genau eintippen.",
                              "Please type your username exactly to confirm."),
    "last_gm_campaigns": ("Du leitest diese Kampagnen als einzige Spielleitung: {titel}. Ernenne dort zuerst eine andere "
                          "Spielleitung, dann kannst du dein Konto löschen.",
                          "You are the only game master of these campaigns: {titel}. Promote someone else there first, "
                          "then you can delete your account."),
    # SL-Unterlagen
    "unsupported_document": ("Bitte eine PDF-, Word- (.docx), .txt- oder .md-Datei hochladen.",
                             "Please upload a PDF, Word (.docx), .txt or .md file."),
    "unsupported_document.password": ("Die PDF ist mit einem Passwort geschützt. Bitte ohne Passwort speichern.",
                                      "The PDF is password-protected. Please save it without a password."),
    "document_too_large": ("Die Unterlage ist zu groß (höchstens 50 MB).", "The document is too large (at most 50 MB)."),
    "invalid_state.document": ("Neu starten geht nur bei einer fehlgeschlagenen Auswertung.",
                               "Only a failed evaluation can be restarted."),
    "invalid_state.document_no_text": ("Die Unterlage enthält keinen auslesbaren Text – ein Neustart hilft hier nicht.",
                                       "The document contains no readable text – restarting will not help."),
    "invalid_state.document_review": ("Die Vorschläge lassen sich nur prüfen und übernehmen, solange die Unterlage auf "
                                      "Prüfung wartet.",
                                      "Proposals can only be reviewed and applied while the document is awaiting review."),
    "not_found.document": ("Die Unterlage wurde nicht gefunden.", "Document not found."),
    "doc.no_text": ("Die Unterlage enthält keinen auslesbaren Text (eingescannt?). Eine Texterkennung gibt es noch nicht.",
                    "The document contains no readable text (scanned?). Text recognition is not available yet."),
    "doc.scanned_pages": ("{n} von {gesamt} Seiten enthalten keinen Text (eingescannt?) und wurden übersprungen – eine "
                          "Texterkennung gibt es noch nicht.",
                          "{n} of {gesamt} pages contain no text (scanned?) and were skipped – text recognition is not "
                          "available yet."),
    "doc.truncated": ("Die Unterlage ist sehr lang. Ausgewertet wurde bis vor Seite bzw. Abschnitt {seite}.",
                      "The document is very long. It was evaluated up to page or section {seite}."),
    "doc.failed": ("Die Auswertung ist fehlgeschlagen: {detail}", "The evaluation failed: {detail}"),
    "doc.waiting_llm": ("Die Auswertung wartet auf ein Sprachmodell. Der Betreiber stellt es in der Verwaltung unter "
                        "„Zusammenfassung“ ein.",
                        "The evaluation is waiting for a language model. The operator sets it up in the administration "
                        "under “Summary”."),
    "doc.waiting_worker": ("Die Auswertung wartet auf einen Worker mit Sprachmodell.",
                           "The evaluation is waiting for a worker with a language model."),
    # Kommentare
    "comment_too_long": ("Der Kommentar ist zu lang (höchstens 4000 Zeichen).",
                         "The comment is too long (at most 4000 characters)."),
    "comment_empty": ("Der Kommentar ist leer.", "The comment is empty."),
    "recipient_unknown": ("Die Person, an die du schreibst, gehört nicht (mehr) zur Kampagne.",
                          "The person you are writing to is not (or no longer) part of the campaign."),
    "forbidden.private_to_gm": ("Privat kannst du nur an die Spielleitung schreiben.",
                                "You can only write privately to the game master."),
    "forbidden.own_comment": ("Du kannst nur deine eigenen Kommentare ändern.", "You can only edit your own comments."),
    "forbidden.delete_comment": ("Du kannst nur deine eigenen Kommentare löschen.",
                                 "You can only delete your own comments."),
    # Terminabstimmung
    "date_poll_open": ("Es läuft schon eine Terminabstimmung.", "A date poll is already running."),
    "date_poll_closed": ("Die Terminabstimmung ist schon beendet.", "The date poll has already ended."),
    "date_in_past": ("Der Termin liegt in der Vergangenheit.", "The date is in the past."),
    "option_exists": ("Diesen Termin gibt es schon in der Abstimmung.", "This date is already in the poll."),
    "not_found.option": ("Der Termin wurde nicht gefunden.", "Date not found."),
    "not_found.date_poll": ("Die Terminabstimmung wurde nicht gefunden.", "Date poll not found."),
    "not_found.comment": ("Der Kommentar wurde nicht gefunden.", "Comment not found."),
    "forbidden.option": ("Nur wer den Termin vorgeschlagen hat oder die Spielleitung kann ihn entfernen.",
                         "Only whoever proposed the date or the game master can remove it."),
    # Bilder
    "unsupported_image": ("Bitte ein Bild als JPEG, PNG oder WebP senden.", "Please send a JPEG, PNG or WebP image."),
    "image_too_large": ("Das Bild ist zu groß (höchstens {mb} MB).", "The image is too large (at most {mb} MB)."),
    "invalid_crop": ("Der Bildausschnitt liegt nicht vollständig im Bild.", "The crop area is not fully inside the image."),
    "forbidden.portrait": ("Du kannst nur dein eigenes Charakterbild hochladen.", "You can only upload your own character image."),
    # Berechtigung
    "forbidden": ("Das darf nur die Spielleitung.", "Only the game master can do this."),
    "forbidden.own_character": ("Du kannst nur deinen eigenen Charakter ändern.", "You can only edit your own character."),
    "forbidden.role": ("Nur die Spielleitung kann Rollen ändern.", "Only the game master can change roles."),
    "forbidden.character_self": ("Kurzbeschreibung und Hintergrund eines Charakters kann nur die Person selbst ändern.",
                                 "Only the player themselves can edit a character's summary and backstory."),
    "forbidden.generic": ("Keine Berechtigung.", "Not allowed."),
    # Nicht gefunden
    "not_found": ("Nicht gefunden.", "Not found."),
    "not_found.campaign": ("Die Kampagne wurde nicht gefunden.", "Campaign not found."),
    "not_found.session": ("Die Session wurde nicht gefunden.", "Session not found."),
    "not_found.entry": ("Der Eintrag wurde nicht gefunden.", "Entry not found."),
    "not_found.member": ("Das Mitglied wurde nicht gefunden.", "Member not found."),
    "not_found.recap": ("Für diese Session gibt es noch keinen Recap.", "There is no recap for this session yet."),
    "not_found.image": ("Es gibt kein Bild.", "There is no image."),
    "no_date_poll": ("Es gibt keine Terminabstimmung.", "There is no date poll."),
    "invite_invalid": ("Dieser Einladungscode ist ungültig oder abgelaufen. Bitte frag deine Spielleitung nach einem neuen.",
                       "This invite code is invalid or has expired. Please ask your game master for a new one."),
    # Kampagne / Mitglieder
    "last_gm": ("Die Kampagne braucht mindestens eine Spielleitung. Ernenne zuerst eine andere Person.",
                "The campaign needs at least one game master. Promote someone else first."),
    "campaign_archived": ("Die Kampagne ist abgeschlossen. Nimm sie zuerst wieder auf.",
                          "The campaign is closed. Reopen it first."),
    "session_published": ("Veröffentlichte Kapitel lassen sich nicht verwerfen.",
                          "Published chapters cannot be discarded."),
    "confirmation_mismatch.title": ("Bitte zur Bestätigung den Titel der Kampagne genau eintippen.",
                                    "Please type the campaign title exactly to confirm."),
    "organization_invalid": ("Du gehörst dieser Organisation nicht an.", "You are not a member of this organization."),
    "organization_required": ("Du gehörst mehreren Organisationen an. Bitte wähle eine aus.",
                              "You belong to several organizations. Please choose one."),
    # Sessions / Einwilligung
    "attendees_empty": ("Bitte gib mindestens eine anwesende Person an.", "Please add at least one attendee."),
    "attendee_duplicate": ("Eine Person ist doppelt als anwesend eingetragen.", "A person is listed twice as attendee."),
    "attendee_unknown": ("Eine der anwesenden Personen gehört nicht zur Kampagne.",
                         "One of the attendees is not a member of this campaign."),
    "invalid_attendee": ("Jede anwesende Person braucht entweder ein Mitglied oder einen Gastnamen – nicht beides.",
                         "Each attendee needs either a member or a guest name – not both."),
    "invalid_attendee.guest_app": ("Gäste ohne Konto können nur vor Ort zustimmen.",
                                   "Guests without an account can only consent on site."),
    "consent_missing": ("{name} hat der Aufnahme in der App noch nicht zugestimmt. Bitte vor Ort zustimmen lassen.",
                        "{name} has not agreed to recordings in the app yet. Please ask for consent on site."),
    "consent_missing.upload": ("Nicht alle Anwesenden haben der Aufnahme zugestimmt. Ohne Zustimmung aller ist kein Upload möglich.",
                               "Not all attendees have agreed to the recording. Uploading requires everyone's consent."),
    "consent_revoked": ("{name} hat die Zustimmung zu Aufnahmen inzwischen widerrufen. Ein Upload ist so nicht möglich.",
                        "{name} has withdrawn consent to recordings. The upload is not possible."),
    "attendees_locked": ("Anwesende und Einwilligungen lassen sich nach dem Start des Uploads nicht mehr ändern.",
                         "Attendees and consent cannot be changed after the upload has started."),
    "invalid_state": ("Nur fehlgeschlagene Verarbeitungen können neu gestartet werden.",
                      "Only failed processing can be restarted."),
    "already_published": ("Diese Session ist bereits veröffentlicht.", "This session has already been published."),
    "not_ready": ("Die Session kann erst veröffentlicht werden, wenn Recap und Vorschläge vorliegen.",
                  "The session can only be published once the recap and proposals are ready."),
    # Upload / Verarbeitung
    "upload_in_progress": ("Für diese Session läuft bereits ein anderer Upload.", "Another upload is already running for this session."),
    "invalid_state.upload": ("Für diese Session wurde bereits Audio hochgeladen.", "Audio has already been uploaded for this session."),
    "upload_closed": ("Dieser Upload ist bereits abgeschlossen oder wurde verworfen.", "This upload has already been completed or discarded."),
    "chunks_missing": ("Es fehlen noch {count} Teile. Bitte den Upload fortsetzen.", "{count} parts are still missing. Please resume the upload."),
    "chunk_size_mismatch": ("Teil {index} hat die falsche Größe ({got} statt {expected} Bytes).",
                            "Part {index} has the wrong size ({got} instead of {expected} bytes)."),
    "chunk_index_invalid": ("Diesen Teil gibt es nicht (Nummer {index}).", "This part does not exist (number {index})."),
    "chunk_checksum_mismatch": ("Teil {index} ist beim Übertragen beschädigt worden. Bitte erneut senden.",
                                "Part {index} was damaged in transit. Please send it again."),
    "files_empty": ("Bitte mindestens eine Audiodatei angeben.", "Please add at least one audio file."),
    "unsupported_audio": ("„{name}“ ist keine unterstützte Audiodatei.", "“{name}” is not a supported audio file."),
    "file_too_large": ("„{name}“ ist zu groß.", "“{name}” is too large."),
    "track_member_unexpected": ("Bei einer Tischaufnahme gehört keine Person zu einer einzelnen Spur.",
                                "A table recording has no per-track members."),
    "track_member_invalid": ("Jede Discord-Spur braucht eine anwesende Person der Kampagne – und jede Person nur eine Spur.",
                             "Each Discord track needs a different attendee of this campaign."),
    "audio_gone": ("Das Audio ist schon gelöscht. Bitte die Aufnahme neu hochladen.",
                   "The audio has already been deleted. Please upload the recording again."),
    "audio_deleted": ("Die Hörprobe ist bereits gelöscht.", "The voice sample has already been deleted."),
    "lease_lost": ("Dieser Auftrag ist nicht (mehr) diesem Worker zugeteilt.", "This job is no longer assigned to this worker."),
    "app_outdated": ("Diese App ist zu alt. Bitte aktualisiere auf Taleward {version} oder neuer.",
                     "This app is too old. Please update to Taleward {version} or newer."),
    "setup_account_only": ("Das Konto „admin“ dient nur der Ersteinrichtung. Bitte im Browser unter /verwaltung einen eigenen Verwalter anlegen.",
                           "The “admin” account is only for the initial setup. Please create your own administrator at /verwaltung in a browser."),
    "voice_consent_missing": ("Für ein Stimmprofil brauchen wir deine ausdrückliche Einwilligung.",
                              "A voice profile requires your explicit consent."),
    "voice_audio_missing": ("Die Aufnahme fehlt oder ist leer.", "The recording is missing or empty."),
    "voice_too_large": ("Die Aufnahme ist zu groß. 20–30 Sekunden reichen.", "The recording is too large. 20–30 seconds are enough."),
    "voice_unsupported": ("Dieses Dateiformat wird nicht unterstützt.", "This file format is not supported."),
    "voice_too_short": ("Die Aufnahme ist mit {sekunden} Sekunden zu kurz. Bitte etwa 20–30 Sekunden vorlesen.",
                        "The recording is too short ({sekunden} seconds). Please read aloud for about 20–30 seconds."),
    "voice_processing": ("Dein Stimmprofil wird gerade berechnet. Bitte kurz warten.",
                         "Your voice profile is being processed. Please wait a moment."),
    # Status-Hinweise (ProcessingStatus.message)
    "status.no_worker": ("Zurzeit ist keine Transkription verfügbar. Die Aufnahme wird verarbeitet, sobald ein Worker bereitsteht.",
                         "Transcription is not available right now. The recording will be processed as soon as a worker is ready."),
    "status.worker_paused": ("Der Worker ist gerade pausiert. Die Aufnahme wird verarbeitet, sobald er fortgesetzt wird.",
                             "The worker is paused right now. The recording will be processed as soon as it resumes."),
    "status.external_planned": ("Zurzeit ist keine Transkription verfügbar. Steht bis {zeit} Uhr kein Worker bereit, übernimmt {anbieter} die Transkription (für diese Kampagne erlaubt).",
                                "Transcription is not available right now. If no worker is ready by {zeit}, {anbieter} will transcribe the recording (allowed for this campaign)."),
    "status.failed_transcription": ("Die Transkription ist fehlgeschlagen: {detail}", "Transcription failed: {detail}"),
    "status.failed_generic": ("Die Verarbeitung ist fehlgeschlagen. Bitte „Erneut versuchen“.",
                              "Processing failed. Please try again."),
    "status.failed_summary": ("Die Zusammenfassung ist fehlgeschlagen: {detail}", "Summarizing failed: {detail}"),
    "model_not_on_server": ("Dieses Modell liegt nicht auf dem Server.", "This model is not stored on the server."),
    "pairing_code_invalid": ("Der Kopplungscode ist ungültig oder abgelaufen. Bitte in der Verwaltung einen neuen "
                             "erzeugen.", "The pairing code is invalid or has expired. Please create a new one in the "
                             "administration."),
    "status.cloud_summary_not_allowed": (
        "Die Zusammenfassung läuft auf diesem Server über {anbieter}. Die SL hat das für diese Kampagne noch nicht "
        "erlaubt (Kampagne und Welt bearbeiten → „Zusammenfassung über {anbieter} erlauben“).",
        "Summaries on this server run via {anbieter}. The GM has not allowed this for this campaign yet (Edit campaign "
        "and world → “Allow summary via {anbieter}”)."),
    "status.cost_limit": ("Das monatliche Kostenlimit für Cloud-Dienste ist erreicht. Die Verarbeitung wartet bis zum "
                          "nächsten Monat oder bis der Betreiber das Limit erhöht.",
                          "The monthly cost limit for cloud services has been reached. Processing waits until next "
                          "month or until the operator raises the limit."),
    "status.no_llm_worker": ("Zurzeit ist kein Sprachmodell verfügbar. Die Zusammenfassung startet, sobald ein Worker mit Sprachmodell bereitsteht.",
                             "No language model is available right now. Summarizing will start as soon as a worker with a language model is ready."),
    "status.no_summarizer": ("Die Zusammenfassung ist auf diesem Server gerade abgeschaltet. Die Session wird verarbeitet, sobald sie wieder läuft.",
                             "Summarizing is currently switched off on this server. The session will be processed once it is running again."),
    # Stimmen, Recap, Vorschläge
    "invalid_state.speakers": ("Stimmen können nur zugeordnet werden, solange die Session auf „Stimmen zuordnen“ steht.",
                               "Voices can only be assigned while the session is waiting for voice assignment."),
    "invalid_state.review": ("Recap und Vorschläge lassen sich nur bearbeiten, solange die Session auf Prüfung wartet.",
                             "The recap and proposals can only be edited while the session is waiting for review."),
    "speaker_unknown": ("Eine der Stimmen gehört nicht zu dieser Session.", "One of the voices does not belong to this session."),
    "speaker_duplicate": ("Eine Stimme wurde mehrfach angegeben.", "A voice was listed more than once."),
    "member_unknown": ("Eine der zugeordneten Personen gehört nicht zur Kampagne.", "One of the assigned members is not part of this campaign."),
    "validation_error.recap_text": ("Der Recap darf nicht leer sein.", "The recap must not be empty."),
    "validation_error.open_threads": ("Zu viele oder zu lange offene Fäden.", "Too many or too long open threads."),
    # Bibel
    "public_text_required": ("Für die Freigabe braucht der Eintrag einen öffentlichen Text.",
                             "An entry needs a public text before it can be shared with players."),
    "hidden_member_unknown": ("Eine der Personen, vor denen der Eintrag verborgen sein soll, gehört nicht zur Kampagne.",
                              "One of the members to hide the entry from is not part of this campaign."),
    "holder_unknown": ("Die Person, die den Gegenstand hat, gehört nicht zur Kampagne.",
                       "The item holder is not a member of this campaign."),
    # Allgemein
    "feature_unavailable": ("Diese Funktion ist in dieser Serverversion noch nicht verfügbar.",
                            "This feature is not available in this server version yet."),
    "validation_error": ("Die Eingabe ist ungültig.", "The input is invalid."),
    "validation_error.json": ("Die Anfrage enthält kein gültiges JSON.", "The request does not contain valid JSON."),
    "validation_error.missing": ("Pflichtfeld fehlt: {field}.", "Required field missing: {field}."),
    "validation_error.missing_any": ("Es fehlen Angaben.", "Some information is missing."),
    "validation_error.value": ("Ungültiger Wert für {field}.", "Invalid value for {field}."),
    "validation_error.datetime": ("Ungültige Zeitangabe für {field} (erwartet ISO 8601).",
                                  "Invalid date/time for {field} (ISO 8601 expected)."),
    "validation_error.too_long": ("{field} ist zu lang.", "{field} is too long."),
    "validation_error.empty": ("{field} darf nicht leer sein.", "{field} must not be empty."),
    "validation_error.title": ("Bitte gib einen Titel an.", "Please enter a title."),
    "validation_error.type_name": ("Bitte gib Typ und Namen des Eintrags an.", "Please enter the type and name of the entry."),
    "validation_error.type": ("Bitte gib einen Typ an.", "Please choose a type."),
    "validation_error.name": ("Bitte gib einen Namen an.", "Please enter a name."),
    "validation_error.visibility": ("Bitte gib die Sichtbarkeit an.", "Please choose the visibility."),
    "validation_error.month": ("Der Monat muss im Format JJJJ-MM angegeben werden.", "The month must be given as YYYY-MM."),
    "method_not_allowed": ("Diese Aktion wird hier nicht unterstützt.", "This action is not supported here."),
    "payload_too_large": ("Die Datei ist zu groß.", "The file is too large."),
    "internal_error": ("Auf dem Server ist ein unerwarteter Fehler aufgetreten.", "An unexpected server error occurred."),
    "error": ("Es ist ein Fehler aufgetreten.", "An error occurred."),
}


class ApiError(Exception):
    """Fehler mit HTTP-Status, maschinenlesbarem Code und übersetzbarer Meldung.

    `key` wählt den Meldungstext (Standard: code), `params` füllt Platzhalter.
    """

    def __init__(self, status: int, code: str, key: str | None = None, **params):
        self.status = status
        self.code = code
        self.key = key or code
        self.params = params

    def message(self, lang: str) -> str:
        de, en = MESSAGES.get(self.key, MESSAGES["error"])
        text = en if lang == "en" else de
        try:
            return text.format(**self.params)
        except (KeyError, IndexError):
            return text


def sprache(request: Request) -> str:
    """de oder en aus Accept-Language (erste passende Angabe gewinnt)."""
    header = request.headers.get("accept-language", "")
    for teil in header.split(","):
        tag = teil.split(";")[0].strip().lower()
        if tag.startswith("en"):
            return "en"
        if tag.startswith("de"):
            return "de"
    return "de"


# Kurzformen
def not_found(resource: str | None = None) -> ApiError:
    return ApiError(404, "not_found", f"not_found.{resource}" if resource else "not_found")


def forbidden(key: str = "forbidden") -> ApiError:
    return ApiError(403, "forbidden", key)


def conflict(code: str, key: str | None = None, **params) -> ApiError:
    return ApiError(409, code, key, **params)


def bad_request(code: str, key: str | None = None, **params) -> ApiError:
    return ApiError(400, code, key, **params)


def feature_unavailable() -> ApiError:
    return ApiError(409, "feature_unavailable")


UNAUTHENTICATED = ApiError(401, "not_authenticated")
TOKEN_INVALID = ApiError(401, "token_invalid")
BAD_CREDENTIALS = ApiError(401, "invalid_credentials")


def _body(err: ApiError, lang: str) -> dict:
    return {"code": err.code, "message": err.message(lang)}


def _field_name(loc: tuple) -> str:
    return ".".join(str(p) for p in loc if p not in ("body", "query", "path", "header"))


def _validation_error(exc: RequestValidationError) -> ApiError:
    errors = exc.errors()
    if not errors:
        return ApiError(400, "validation_error")
    err = errors[0]
    field = _field_name(tuple(err.get("loc", ())))
    etype = err.get("type", "")
    if etype == "json_invalid":
        return ApiError(400, "validation_error", "validation_error.json")
    if etype == "missing":
        return ApiError(400, "validation_error", "validation_error.missing" if field else "validation_error.missing_any",
                        field=field)
    if etype.startswith("datetime"):
        return ApiError(400, "validation_error", "validation_error.datetime", field=field)
    if etype == "string_too_short":
        return ApiError(400, "validation_error", "validation_error.empty", field=field)
    if etype == "string_too_long":
        return ApiError(400, "validation_error", "validation_error.too_long", field=field)
    if field:
        return ApiError(400, "validation_error", "validation_error.value", field=field)
    return ApiError(400, "validation_error")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(req: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=_body(exc, sprache(req)))

    @app.exception_handler(RequestValidationError)
    async def _validation(req: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content=_body(_validation_error(exc), sprache(req)))

    @app.exception_handler(StarletteHTTPException)
    async def _http(req: Request, exc: StarletteHTTPException):
        mapping = {
            401: ApiError(401, "not_authenticated"),
            403: ApiError(403, "forbidden", "forbidden.generic"),
            404: ApiError(404, "not_found"),
            405: ApiError(405, "method_not_allowed"),
            413: ApiError(413, "payload_too_large"),
        }
        err = mapping.get(exc.status_code, ApiError(exc.status_code, "error"))
        return JSONResponse(status_code=exc.status_code, content=_body(err, sprache(req)), headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unexpected(req: Request, _exc: Exception):
        return JSONResponse(status_code=500, content=_body(ApiError(500, "internal_error"), sprache(req)))
