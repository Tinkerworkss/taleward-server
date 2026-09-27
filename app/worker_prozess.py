"""Worker: holt Aufträge von der Zentrale, verarbeitet sie lokal, liefert Ergebnisse zurück.

- Keine Datenbank, nichts bleibt liegen: Arbeitsordner wird bei jedem Start und nach jedem Auftrag geleert.
- Verbindung nur nach außen (Long-Poll), kein offener Port nötig.
- Lebenszeichen während der Arbeit verlängern die Lease; geht sie verloren, bricht der Worker ab.

Aufruf: uv run chronik worker --attrappe      (Schritt 2: Platzhaltertext statt Transkription)
"""
from __future__ import annotations

import base64
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from app import audio
from app.sprachmodell import SprachmodellFehler

log = logging.getLogger("worker")

Fortschritt = Callable[[float], None]


class Abgebrochen(Exception):
    """Lease verloren oder Worker wird beendet."""


def pruefe_adresse(url: str, unsicher: bool) -> None:
    teile = urlparse(url)
    if teile.scheme == "https" or unsicher:
        return
    if teile.scheme == "http" and teile.hostname in ("localhost", "127.0.0.1", "::1"):
        return
    raise SystemExit(
        f"Unverschlüsselte Verbindung zu {url} abgelehnt. Nur HTTPS oder localhost – "
        "für Tests im eigenen Heimnetz: --unsicher"
    )


# ---------------------------------------------------------------- Verarbeitung: Attrappe
def verarbeite_attrappe(auftrag: dict, dateien: list[Path], arbeit: Path, fortschritt: Fortschritt) -> dict:
    """Dekodiert und fügt echt zusammen, schneidet echte Hörproben – aber ohne KI (Platzhaltertext)."""
    t0 = time.monotonic()
    if auftrag.get("type") == "voice_enroll":
        return _stimme_attrappe(dateien[0], arbeit, t0)
    sitzung = auftrag["session"]
    discord = sitzung["source"] == "discord"
    segmente, sprecher = [], []
    if discord:
        spuren = []
        for f, pfad in zip(auftrag["files"], dateien):
            wav = arbeit / f"{f['fileId']}.wav"
            audio.zusammenfuegen([pfad], wav)
            spuren.append((f, wav))
        gesamt = max(audio.dauer(w) for _, w in spuren)
        for nr, (f, wav) in enumerate(spuren):
            d = audio.dauer(wav)
            t, k = 0.0, 1
            while t < d:
                segmente.append({"start": t, "end": min(t + 25, d), "speaker": f["fileId"],
                                 "text": f"Platzhalter (Testmodus): Spur {nr + 1}, Abschnitt {k}."})
                t, k = t + 30, k + 1
            probe = audio.hoerprobe(wav, 0, min(5.0, d), arbeit / f"probe-{nr}.ogg")
            sprecher.append({"label": f["fileId"], "speakingSeconds": d * 25 / 30,
                             "sampleText": f"Platzhalter (Testmodus): Spur {nr + 1}.",
                             "sampleOggBase64": base64.b64encode(probe).decode(),
                             "trackMemberId": f["trackMemberId"]})
            fortschritt(0.5 + 0.5 * (nr + 1) / len(spuren))
    else:
        wav = arbeit / "gesamt.wav"
        audio.zusammenfuegen(dateien, wav)
        fortschritt(0.5)
        gesamt = audio.dauer(wav)
        n = max(1, min(8, int(sitzung.get("expectedSpeakers") or 2)))
        t, k = 0.0, 0
        redezeit = [0.0] * n
        erste: dict[int, float] = {}
        while t < gesamt:
            nr = k % n
            ende = min(t + 18, gesamt)
            segmente.append({"start": t, "end": ende, "speaker": f"SPEAKER_{nr:02d}",
                             "text": f"Platzhalter (Testmodus): Stimme {nr + 1}, Abschnitt {k + 1}."})
            redezeit[nr] += ende - t
            erste.setdefault(nr, t)
            t, k = t + 20, k + 1
        for nr in range(n):
            if nr not in erste:
                continue
            probe = audio.hoerprobe(wav, erste[nr], 5.0, arbeit / f"probe-{nr}.ogg")
            sprecher.append({"label": f"SPEAKER_{nr:02d}", "speakingSeconds": redezeit[nr],
                             "sampleText": f"Platzhalter (Testmodus): Stimme {nr + 1}.",
                             "sampleOggBase64": base64.b64encode(probe).decode()})
        fortschritt(1.0)
    return {"audioSeconds": gesamt, "computeSeconds": time.monotonic() - t0, "model": "attrappe",
            "segments": segmente, "speakers": sprecher}


def _stimme_attrappe(datei: Path, arbeit: Path, t0: float) -> dict:
    """Stimmprofil ohne KI: echte Dauer, Abdruck aus der Prüfsumme (gleiche Datei → gleicher Abdruck)."""
    import hashlib

    wav = arbeit / "stimme.wav"
    audio.zusammenfuegen([datei], wav)
    d = audio.dauer(wav)
    h = hashlib.sha256(datei.read_bytes()).digest()
    return {"embedding": [(b - 127.5) / 127.5 for b in h[:32]], "speechSeconds": d, "audioSeconds": d,
            "computeSeconds": time.monotonic() - t0, "model": "attrappe"}


# ---------------------------------------------------------------- Sprachmodell (Ollama)
def lokales_sprachmodell(url: str, client: httpx.Client | None = None):
    """Läuft Ollama auf diesem Worker? Dann (Funktion für Zusammenfassungs-Aufträge, Angabe für die Zentrale).
    Sonst (None, None) – der Worker übernimmt dann nur Transkriptionen."""
    from app.sprachmodell import Ablauf, OllamaKlient

    probe = OllamaKlient(url, "", client=client)
    version = probe.version()
    if version is None:
        return None, None

    def zusammenfassen(auftrag: dict, fortschritt: Fortschritt) -> dict:
        if auftrag.get("type") == "document":
            return unterlage(auftrag, fortschritt)
        z = auftrag["summarize"]
        t0 = time.monotonic()
        klient = OllamaKlient(url, z["model"], int(z.get("context") or 12288), client=client)
        kennung = klient.bereitstellen(lambda text: log.info(text))
        fortschritt(0.05)
        # Stückgröße so, dass Anweisung, Stück und Antwort in den Kontext passen
        ablauf = Ablauf(klient, max_transkript_tokens=max(2000, klient.kontext - 5000),
                        stueck_tokens=max(1500, (klient.kontext - 4000) // 2))
        try:
            d = ablauf.ausfuehren(z["recap"], z["proposals"], fortschritt)
        finally:
            klient.entladen()
        d["model"] = f"ollama/{klient.modell}@{kennung}"
        d["computeSeconds"] = round(time.monotonic() - t0, 1)
        return d

    def unterlage(auftrag: dict, fortschritt: Fortschritt) -> dict:
        from app.sprachmodell import DokumentAblauf

        z = auftrag["document"]
        t0 = time.monotonic()
        klient = OllamaKlient(url, z["model"], int(z.get("context") or 12288), client=client)
        kennung = klient.bereitstellen(lambda text: log.info(text))
        fortschritt(0.05)
        # Anweisung, Bibel (Namen) und Antwort brauchen Platz – das Stück bekommt etwa die Hälfte des Kontexts
        ablauf = DokumentAblauf(klient, stueck_tokens=max(1500, (klient.kontext - 5000) // 2))
        try:
            d = ablauf.ausfuehren(z["input"], fortschritt)
        finally:
            klient.entladen()
        d["model"] = f"ollama/{klient.modell}@{kennung}"
        d["computeSeconds"] = round(time.monotonic() - t0, 1)
        return d

    return zusammenfassen, f"Ollama {version}"


# ---------------------------------------------------------------- Worker
class WorkerProzess:
    def __init__(self, server: str, token: str, arbeitsordner: Path,
                 verarbeite: Callable[[dict, list[Path], Path, Fortschritt], dict],
                 client: httpx.Client | None = None, claim_wait: int | None = None,
                 capabilities: tuple[str, ...] = ("asr",), info: dict | None = None,
                 zusammenfassen: Callable[[dict, Fortschritt], dict] | None = None):
        self.client = client or httpx.Client(base_url=server, timeout=httpx.Timeout(90.0))
        self.client.headers["Authorization"] = f"Bearer {token}"
        self.arbeit = arbeitsordner
        self.verarbeite = verarbeite
        self.claim_wait = claim_wait
        self.capabilities = list(capabilities)
        self.info = info or {}
        self.zusammenfassen = zusammenfassen  # Sprachmodell (Ollama) – nur, wenn auf diesem Worker vorhanden
        if zusammenfassen is not None and "llm" not in self.capabilities:
            self.capabilities.append("llm")
        self._stop = threading.Event()

    # -- Arbeitsordner
    def aufraeumen(self) -> None:
        shutil.rmtree(self.arbeit, ignore_errors=True)
        self.arbeit.mkdir(parents=True, exist_ok=True)

    def beenden(self) -> None:
        self._stop.set()

    # -- Protokoll
    def abholen(self) -> dict | None:
        body = {"capabilities": self.capabilities, "info": self.info}
        if self.claim_wait is not None:
            body["waitSeconds"] = self.claim_wait
        r = self.client.post("/worker/v1/jobs/claim", json=body)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def herunterladen(self, auftrag: dict) -> list[Path]:
        dateien = []
        for f in sorted(auftrag["files"], key=lambda x: x["position"]):
            ziel = self.arbeit / f"{f['position']:04d}-{f['fileId']}"
            with open(ziel, "wb") as out:
                for c in f["chunks"]:
                    if self._stop.is_set():
                        raise Abgebrochen()
                    r = self.client.get(c["url"])
                    if r.status_code == 409:
                        raise Abgebrochen()
                    r.raise_for_status()
                    if len(r.content) != c["sizeBytes"]:
                        raise audio.AudioFehler("download_incomplete", f"Teil {c['index']} unvollständig", True)
                    out.write(r.content)
            dateien.append(ziel)
        return dateien

    def einen_auftrag(self) -> bool:
        """Holt höchstens einen Auftrag und bearbeitet ihn. True, wenn einer bearbeitet wurde."""
        auftrag = self.abholen()
        if auftrag is None:
            return False
        job = auftrag["jobId"]
        log.info("Auftrag %s übernommen (%d Datei(en))", job, len(auftrag["files"]))
        self.aufraeumen()
        stand = {"p": 0.0, "verloren": False}
        herz_stop = threading.Event()

        def herzschlag():
            takt = max(5, auftrag["leaseSeconds"] // 3)
            while not herz_stop.wait(takt):
                try:
                    r = self.client.post(f"/worker/v1/jobs/{job}/progress", json={"progress": stand["p"]})
                    if r.status_code == 409:
                        stand["verloren"] = True
                        return
                except httpx.HTTPError:
                    pass  # Netz kurz weg – nächster Versuch beim nächsten Takt

        def fortschritt(p: float) -> None:
            stand["p"] = max(stand["p"], min(1.0, p))
            if stand["verloren"] or self._stop.is_set():
                raise Abgebrochen()

        herz = threading.Thread(target=herzschlag, daemon=True)
        herz.start()
        try:
            if auftrag.get("type") in ("summarize", "document"):
                if self.zusammenfassen is None:
                    raise audio.AudioFehler("worker_setup", "Auf diesem Worker läuft kein Sprachmodell.", True)
                ergebnis = self.zusammenfassen(auftrag, fortschritt)
                ziel = "summary-result" if auftrag["type"] == "summarize" else "document-result"
            else:
                dateien = self.herunterladen(auftrag)
                fortschritt(0.1)
                ergebnis = self.verarbeite(auftrag, dateien, self.arbeit, fortschritt)
                ziel = "voice-result" if auftrag.get("type") == "voice_enroll" else "result"
            r = self.client.post(f"/worker/v1/jobs/{job}/{ziel}", json=ergebnis)
            if r.status_code == 409:
                log.warning("Ergebnis zu %s verworfen: Auftrag inzwischen woanders", job)
            else:
                r.raise_for_status()
                log.info("Auftrag %s fertig", job)
        except Abgebrochen:
            if not stand["verloren"]:
                self._melde_fehler(job, "worker_stopped", "Der Worker wurde beendet.", True)
            log.warning("Auftrag %s abgebrochen", job)
        except KeyboardInterrupt:
            self._melde_fehler(job, "worker_stopped", "Der Worker wurde beendet.", True)
            raise
        except audio.AudioFehler as e:
            self._melde_fehler(job, e.code, str(e), e.retryable)
        except SprachmodellFehler as e:
            self._melde_fehler(job, "summary_error", str(e), e.erneut)
        except Exception as e:  # unerwartet: lieber noch einmal versuchen lassen
            log.exception("Fehler bei Auftrag %s", job)
            self._melde_fehler(job, "worker_error", f"{type(e).__name__}: {e}"[:500], True)
        finally:
            herz_stop.set()
            self.aufraeumen()
        return True

    def _melde_fehler(self, job: str, code: str, message: str, retryable: bool) -> None:
        try:
            self.client.post(f"/worker/v1/jobs/{job}/fail",
                             json={"code": code, "message": message, "retryable": retryable})
        except httpx.HTTPError:
            pass  # Zentrale holt den Auftrag nach Lease-Ablauf ohnehin zurück

    def laufen(self) -> None:
        self.aufraeumen()
        log.info("Worker bereit, warte auf Aufträge …")
        pause = 1.0
        while not self._stop.is_set():
            try:
                self.einen_auftrag()
                pause = 1.0
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    raise SystemExit("Der Server lehnt den Worker-Token ab (401). Token prüfen: chronik worker-token list")
                log.warning("Server antwortet mit %s – neuer Versuch in %.0f s", e.response.status_code, pause)
                self._stop.wait(pause)
                pause = min(pause * 2, 60)
            except httpx.HTTPError as e:
                log.warning("Server nicht erreichbar (%s) – neuer Versuch in %.0f s", type(e).__name__, pause)
                self._stop.wait(pause)
                pause = min(pause * 2, 60)
        self.aufraeumen()
