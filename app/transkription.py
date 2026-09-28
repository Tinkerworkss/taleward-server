"""Echte Transkription im Worker: WhisperX (Whisper large-v3) + pyannote (community-1).

Aufbau:
- `WhisperXMotor` kapselt alle Aufrufe der KI-Bibliotheken (nur mit `uv sync --extra ki`, braucht eine Grafikkarte).
- `verarbeiter(motor)` liefert die Verarbeitungsfunktion für den Worker. Sie ist von der Bibliothek unabhängig
  und wird in den Tests mit einem Test-Motor geprüft.

Erkenntnisse aus dem Probelauf (Benjamins PC, RTX 3060 Ti 8 GB):
- Namenshilfe nur als `hotwords`, nie als `initial_prompt` (wurde sonst wörtlich ins Transkript übernommen).
  Zusätzlich filtert `namens_echo_entfernen` Abschnitte, die nur die Namensliste nachplappern.
- Modelle nacheinander laden und sofort wieder freigeben (Spitze ~6,6 GB inkl. Windows).
- Stimmen mit sehr wenig Redezeit sind meist Fehlzuordnungen → unter MIN_REDEZEIT keine eigene Stimme.
"""
from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Callable, Protocol

from app import audio
from app.audio import AudioFehler
from app.worker_prozess import Abgebrochen
from app.probelauf import VramMesser, cuda_bibliotheken_vorladen, gpu_freigeben, namens_echo_entfernen

# pyannote schickt sonst Nutzungsdaten (Audiodauer, Personenzahl) an pyannote.ai – hier nie
os.environ["PYANNOTE_METRICS_ENABLED"] = "false"

Fortschritt = Callable[[float], None]

MIN_REDEZEIT = 5.0  # Sekunden; kleinere Cluster werden keiner Stimme zugeordnet
PROBE_ZIEL = 6.0  # gewünschte Länge einer Hörprobe
PROBE_MAX = 8.0
PROBE_TEXT_MAX = 300


class Motor(Protocol):
    modell: str

    def audio_laden(self, wav: Path): ...
    def transkribieren(self, audio, sprache: str, hotwords: list[str], fortschritt: Fortschritt) -> list[dict]: ...
    def ausrichten(self, segmente: list[dict], audio, sprache: str, fortschritt: Fortschritt) -> list[dict]: ...
    def sprecher_trennen(self, audio, segmente: list[dict], min_n: int | None, max_n: int | None,
                         fortschritt: Fortschritt) -> tuple[list[dict], dict[str, list[float]] | None]: ...
    def stimmabdruck(self, audio, fortschritt: Fortschritt) -> tuple[list[float], float]: ...


def _bereich(fortschritt: Fortschritt, von: float, bis: float) -> Callable[[float], None]:
    """Fortschritt einer Phase (0–100 von WhisperX) auf einen Abschnitt 0–1 des ganzen Auftrags abbilden."""
    return lambda pct: fortschritt(von + (bis - von) * max(0.0, min(100.0, pct)) / 100.0)


_GERAEUSCH_WOERTER = {"musik", "applaus", "lachen", "gelächter", "music", "applause", "laughter"}
_BEKANNTE_HALLUZINATIONEN = ("untertitel", "fürs zuschauen", "für's zuschauen", "thanks for watching",
                             "subtitles by", "amara.org")


def ist_halluzination(text: str) -> bool:
    """Typische Whisper-Erfindungen bei Musik oder Stille („Musik Musik“, „Untertitel im Auftrag des ZDF“)."""
    klein = text.casefold().strip()
    woerter = re.findall(r"\w+", klein)
    if woerter and all(w in _GERAEUSCH_WOERTER for w in woerter):
        return True
    return any(h in klein for h in _BEKANNTE_HALLUZINATIONEN)


def halluzinationen_entfernen(segmente: list[dict], nur_ohne_sprecher: bool = True) -> list[dict]:
    """Bei der Tischaufnahme nur Zeilen, in denen die Sprechertrennung keine Stimme gefunden hat – echte Sätze
    bleiben so immer erhalten. Bei Discord-Spuren (ohne Sprechertrennung) nach denselben engen Mustern."""
    return [s for s in segmente
            if not (ist_halluzination(s.get("text") or "") and (not nur_ohne_sprecher or not s.get("speaker")))]


def _bereinigen(segmente: list[dict], hotwords: list[str]) -> list[dict]:
    segmente, _ = namens_echo_entfernen(segmente, hotwords)
    return [s for s in segmente if (s.get("text") or "").strip() and s.get("end", 0) > s.get("start", 0)]


def _probe_segment(segs: list[dict]) -> dict:
    """Ein gut hörbarer Abschnitt: möglichst nahe an 6 s, mindestens 2 s, sonst der längste."""
    brauchbar = [s for s in segs if s["end"] - s["start"] >= 2.0]
    if brauchbar:
        return min(brauchbar, key=lambda s: (abs((s["end"] - s["start"]) - PROBE_ZIEL), -len(s["text"])))
    return max(segs, key=lambda s: s["end"] - s["start"])


def sprecher_auswerten(segmente: list[dict], wav: Path, arbeit: Path, embeddings: dict | None = None,
                       track_member_id: str | None = None) -> tuple[list[dict], list[dict]]:
    """Segmente → (Segmente für die Zentrale, Stimmen mit Redezeit, Hörprobe, Beispieltext, Abdruck)."""
    nach_sprecher: dict[str, list[dict]] = {}
    for s in segmente:
        label = s.get("speaker")
        if label and label != "UNBEKANNT":
            nach_sprecher.setdefault(label, []).append(s)
    zu_klein = {lab for lab, segs in nach_sprecher.items()
                if sum(s["end"] - s["start"] for s in segs) < MIN_REDEZEIT and track_member_id is None}
    stimmen = []
    for nr, (label, segs) in enumerate(sorted(nach_sprecher.items())):
        if label in zu_klein:
            continue
        probe = _probe_segment(segs)
        dauer = min(PROBE_MAX, probe["end"] - probe["start"])
        ogg = audio.hoerprobe(wav, probe["start"], dauer, arbeit / f"probe-{nr}.ogg")
        eintrag = {
            "label": label,
            "speakingSeconds": round(sum(s["end"] - s["start"] for s in segs), 1),
            "sampleText": probe["text"].strip()[:PROBE_TEXT_MAX],
            "sampleOggBase64": base64.b64encode(ogg).decode(),
        }
        if embeddings and label in embeddings and track_member_id is None:
            eintrag["embedding"] = [float(x) for x in embeddings[label]]
        if track_member_id:
            eintrag["trackMemberId"] = track_member_id
        stimmen.append(eintrag)
    aus = [{"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2),
            "speaker": s.get("speaker") if s.get("speaker") not in (None, "UNBEKANNT", *zu_klein) else None,
            "text": s["text"].strip()} for s in segmente]
    return aus, stimmen


def verarbeiter(motor: Motor):
    """Verarbeitungsfunktion für den Worker (gleiche Form wie `verarbeite_attrappe`)."""

    def verarbeite(auftrag: dict, dateien: list[Path], arbeit: Path, fortschritt: Fortschritt) -> dict:
        t0 = time.monotonic()
        if auftrag.get("type") == "voice_enroll":
            try:
                return _stimmabdruck(motor, dateien[0], arbeit, fortschritt, t0)
            except (AudioFehler, Abgebrochen):
                raise
            except Exception as e:
                raise _uebersetzen(e) from e
            finally:
                gpu_freigeben()
        sitzung = auftrag["session"]
        sprache = sitzung.get("language") or "de"
        hotwords = [h for h in sitzung.get("hotwords") or [] if h.strip()]
        with VramMesser() as vram:
            try:
                if sitzung["source"] == "discord":
                    ergebnis = _discord(motor, auftrag, dateien, arbeit, fortschritt, sprache, hotwords)
                else:
                    ergebnis = _tisch(motor, sitzung, dateien, arbeit, fortschritt, sprache, hotwords)
            except (AudioFehler, Abgebrochen):
                raise
            except Exception as e:  # Speicher voll, Modell nicht ladbar … → verständliche Meldung
                raise _uebersetzen(e) from e
            finally:
                gpu_freigeben()
        ergebnis.update(computeSeconds=round(time.monotonic() - t0, 1), model=f"whisperx/{motor.modell}",
                        peakVramMb=vram.max_mb or None)
        return ergebnis

    return verarbeite


def _stimmabdruck(motor, datei: Path, arbeit: Path, fortschritt: Fortschritt, t0: float) -> dict:
    """Stimmprofil: ein Abdruck aus einer Aufnahme mit genau einer Person – mit demselben Modell wie die
    Sprechertrennung, damit er zu den Stimmgruppen der Sessions passt. Kein Text, kein Audio zurück."""
    wav = arbeit / "stimme.wav"
    audio.zusammenfuegen([datei], wav)
    gesamt = audio.dauer(wav)
    fortschritt(0.3)
    embedding, sprechzeit = motor.stimmabdruck(motor.audio_laden(wav), _bereich(fortschritt, 0.3, 0.95))
    fortschritt(1.0)
    return {"embedding": [float(x) for x in embedding], "speechSeconds": round(sprechzeit, 1),
            "audioSeconds": round(gesamt, 1), "computeSeconds": round(time.monotonic() - t0, 1),
            "model": getattr(motor, "abdruck_modell", motor.modell)}


def _tisch(motor, sitzung, dateien, arbeit, fortschritt, sprache, hotwords) -> dict:
    wav = arbeit / "gesamt.wav"
    audio.zusammenfuegen(dateien, wav)
    gesamt = audio.dauer(wav)
    fortschritt(0.15)
    daten = motor.audio_laden(wav)
    segs = motor.transkribieren(daten, sprache, hotwords, _bereich(fortschritt, 0.15, 0.55))
    segs = _bereinigen(segs, hotwords)
    segs = motor.ausrichten(segs, daten, sprache, _bereich(fortschritt, 0.55, 0.70))
    n = int(sitzung.get("expectedSpeakers") or 0)
    # Anwesende ±1: jemand kann kaum reden, oder eine Stimme wird in zwei Gruppen geteilt
    segs, embeddings = motor.sprecher_trennen(daten, segs, max(1, n - 1) if n else None, n + 1 if n else None,
                                              _bereich(fortschritt, 0.70, 0.95))
    segs = halluzinationen_entfernen(segs)
    segmente, stimmen = sprecher_auswerten(segs, wav, arbeit, embeddings)
    fortschritt(1.0)
    return {"audioSeconds": round(gesamt, 1), "segments": segmente, "speakers": stimmen,
            "embeddingModel": getattr(motor, "abdruck_modell", None)}


def _discord(motor, auftrag, dateien, arbeit, fortschritt, sprache, hotwords) -> dict:
    """Eine Spur pro Person: keine Sprechertrennung, kein Stimmabdruck (aus Discord wird nichts gelernt)."""
    alle_segmente, stimmen, laengste = [], [], 0.0
    anteil = 0.9 / max(1, len(dateien))
    spuren = sorted(auftrag["files"], key=lambda x: x["position"])  # gleiche Reihenfolge wie beim Herunterladen
    for nr, (f, pfad) in enumerate(zip(spuren, dateien)):
        start = 0.05 + nr * anteil
        wav = arbeit / f"spur-{nr}.wav"
        audio.zusammenfuegen([pfad], wav)
        laengste = max(laengste, audio.dauer(wav))
        daten = motor.audio_laden(wav)
        segs = motor.transkribieren(daten, sprache, hotwords, _bereich(fortschritt, start, start + anteil * 0.7))
        segs = _bereinigen(segs, hotwords)
        segs = motor.ausrichten(segs, daten, sprache, _bereich(fortschritt, start + anteil * 0.7, start + anteil))
        segs = halluzinationen_entfernen(segs, nur_ohne_sprecher=False)
        for s in segs:
            s["speaker"] = f["fileId"]
        segmente, spur_stimmen = sprecher_auswerten(segs, wav, arbeit, track_member_id=f.get("trackMemberId"))
        alle_segmente += segmente
        stimmen += spur_stimmen
        wav.unlink(missing_ok=True)
    fortschritt(1.0)
    return {"audioSeconds": round(laengste, 1), "segments": alle_segmente, "speakers": stimmen}


def _uebersetzen(e: Exception) -> AudioFehler:
    text = f"{type(e).__name__}: {e}"
    klein = text.lower()
    if "out of memory" in klein or "outofmemory" in klein:
        return AudioFehler("cuda_oom", "Der Grafikspeicher des Workers war voll. Andere Programme mit "
                                       "Grafiklast schließen – es wird automatisch erneut versucht.", retryable=True)
    if any(k in text for k in ("401", "403", "gated", "Unauthorized", "restricted")):
        return AudioFehler("worker_setup", "Der Worker hat keinen Zugang zum Sprechermodell (Hugging Face). "
                                           "Die Betreiberin bzw. der Betreiber muss HF_TOKEN prüfen.", retryable=False)
    if "cudnn" in klein or "libcu" in klein:
        return AudioFehler("worker_setup", "Auf dem Worker fehlt eine CUDA-Bibliothek "
                                           "(uv sync --extra ki erneut ausführen).", retryable=False)
    return AudioFehler("worker_error", text[:500], retryable=True)


# ---------------------------------------------------------------- WhisperX (nur mit Grafikkarte)
class EinrichtungsFehler(Exception):
    """Worker kann nicht starten – verständliche Meldung für die Kommandozeile."""


class WhisperXMotor:
    def __init__(self, modell: str = "large-v3", genauigkeit: str = "int8_float16", batch: int = 8,
                 hf_token: str | None = None, quelle=None, geraet: str = "cuda", geraet_ausrichten: str = "",
                 geraet_sprecher: str = "", grenze_mb: int = 0, threads: int = 0):
        self.modell, self.genauigkeit, self.batch, self.hf_token = modell, genauigkeit, batch, hf_token
        self.geraet = geraet if geraet in ("cuda", "cpu") else "cuda"
        self.geraet_ausrichten = geraet_ausrichten if geraet_ausrichten in ("cuda", "cpu") else self.geraet
        self.geraet_sprecher = geraet_sprecher if geraet_sprecher in ("cuda", "cpu") else self.geraet
        if self.geraet == "cpu" and self.genauigkeit.endswith("float16"):
            self.genauigkeit = "int8"  # float16 gibt es auf dem Prozessor nicht
        self.grenze_mb, self.threads = max(0, grenze_mb), max(0, threads)
        self.quelle = quelle  # modelle.ServerQuelle: Sprechermodell vom Server statt von Hugging Face
        self.bereit: dict = {}  # whisper/sprecher → modelle.Bereit (feste Fassung, lokaler Ordner)

    def pruefen(self) -> dict:
        """Vor dem Start: KI-Pakete, Grafikkarte, Hugging-Face-Zugang, Modelle in fester Fassung.
        Liefert Angaben für die Zentrale."""
        from app import modelle

        cuda_bibliotheken_vorladen()
        try:
            import torch
            import whisperx  # noqa: F401
        except ImportError:
            raise EinrichtungsFehler("Die KI-Pakete fehlen. Installieren mit: uv sync --extra ki") from None
        braucht_gpu = "cuda" in (self.geraet, self.geraet_ausrichten, self.geraet_sprecher)
        if braucht_gpu and not torch.cuda.is_available():
            raise EinrichtungsFehler("PyTorch sieht keine Grafikkarte. `nvidia-smi` prüfen und ggf. den NVIDIA-Treiber "
                                     "aktualisieren – oder in der Worker-App den Prozessor wählen.")
        if braucht_gpu and self.grenze_mb:
            gesamt = torch.cuda.get_device_properties(0).total_memory / 2 ** 20
            torch.cuda.set_per_process_memory_fraction(min(1.0, self.grenze_mb / gesamt), 0)
        if self.threads:
            torch.set_num_threads(self.threads)
        try:
            sprecher = modelle.vom_server(modelle.SPRECHERMODELL, self.quelle) if self.quelle else None
            if sprecher is None:
                if not self.hf_token:
                    raise EinrichtungsFehler(
                        "Das Sprechermodell liegt noch nicht auf dem Server, und hier ist kein Hugging-Face-Zugang "
                        "eingetragen. In der Verwaltung unter Transkription den Zugang hinterlegen – der Server lädt "
                        "das Modell dann selbst und gibt es an die Worker weiter.")
                sprecher = modelle.bereitstellen(modelle.SPRECHERMODELL, self.hf_token)
            self.bereit = {"whisper": modelle.bereitstellen(modelle.whisper_repo(self.modell), self.hf_token),
                           "sprecher": sprecher}
        except modelle.ModellFehler as e:
            raise EinrichtungsFehler(str(e)) from None
        return {"gpu": torch.cuda.get_device_name(0) if braucht_gpu else "CPU",
                "vramMb": VramMesser.gesamt_mb() if braucht_gpu else None, "modell": self.modell,
                "genauigkeit": self.genauigkeit, "geraete": self.geraete, "grenzeMb": self.grenze_mb or None,
                "modelle": {b.repo: (b.fassung or "")[:12] for b in self.bereit.values()}}

    @property
    def geraete(self) -> str:
        """Kurzform für Anzeige und Protokoll, z. B. „cuda/cuda/cpu“ (Transkription/Ausrichtung/Sprecher)."""
        return f"{self.geraet}/{self.geraet_ausrichten}/{self.geraet_sprecher}"

    def _pfad(self, art: str, ersatz: str) -> str:
        b = self.bereit.get(art)
        return str(b.pfad) if b else ersatz

    @property
    def abdruck_modell(self) -> str:
        from app.modelle import SPRECHERMODELL

        b = self.bereit.get("sprecher")
        return b.kennung if b else SPRECHERMODELL

    def audio_laden(self, wav: Path):
        import whisperx

        return whisperx.load_audio(str(wav))

    def transkribieren(self, daten, sprache, hotwords, fortschritt):
        import whisperx

        opts = {"hotwords": ", ".join(hotwords)} if hotwords else None
        extra = {"threads": self.threads} if self.threads and self.geraet == "cpu" else {}
        model = whisperx.load_model(self._pfad("whisper", self.modell), self.geraet, compute_type=self.genauigkeit,
                                    language=sprache, asr_options=opts, **extra)
        try:
            return model.transcribe(daten, batch_size=self.batch, language=sprache,
                                    progress_callback=fortschritt)["segments"]
        finally:
            del model
            gpu_freigeben()

    def ausrichten(self, segmente, daten, sprache, fortschritt):
        import whisperx

        if not segmente:
            return segmente
        from whisperx.alignment import DEFAULT_ALIGN_MODELS_HF, DEFAULT_ALIGN_MODELS_TORCH

        name = None  # de/en/fr …: torchaudio, fest mit der Paketversion
        if sprache not in DEFAULT_ALIGN_MODELS_TORCH and sprache in DEFAULT_ALIGN_MODELS_HF:
            from app import modelle

            name = str(modelle.bereitstellen(DEFAULT_ALIGN_MODELS_HF[sprache], self.hf_token).pfad)
        model_a, meta = whisperx.load_align_model(language_code=sprache, device=self.geraet_ausrichten, model_name=name)
        try:
            return whisperx.align(segmente, model_a, meta, daten, self.geraet_ausrichten, return_char_alignments=False,
                                  progress_callback=fortschritt)["segments"]
        finally:
            del model_a
            gpu_freigeben()

    def stimmabdruck(self, daten, fortschritt):
        """Genau eine Person: Abdruck der Stimmgruppe und erkannte Sprechzeit (Sekunden)."""
        from whisperx.diarize import DiarizationPipeline

        pipe = DiarizationPipeline(model_name=self._pfad("sprecher", "pyannote/speaker-diarization-community-1"),
                                   token=self.hf_token, device=self.geraet_sprecher)
        try:
            diar, embeddings = pipe(daten, num_speakers=1, return_embeddings=True, progress_callback=fortschritt)
            if not embeddings:
                raise AudioFehler("voice_no_speech", "In der Aufnahme wurde keine Stimme erkannt.", retryable=False)
            sprechzeit = float((diar["end"] - diar["start"]).sum()) if len(diar) else 0.0
            return next(iter(embeddings.values())), sprechzeit
        finally:
            del pipe
            gpu_freigeben()

    def sprecher_trennen(self, daten, segmente, min_n, max_n, fortschritt):
        from whisperx.diarize import DiarizationPipeline, assign_word_speakers

        pipe = DiarizationPipeline(model_name=self._pfad("sprecher", "pyannote/speaker-diarization-community-1"),
                                   token=self.hf_token, device=self.geraet_sprecher)
        try:
            diar, embeddings = pipe(daten, min_speakers=min_n, max_speakers=max_n, return_embeddings=True,
                                    progress_callback=fortschritt)
            return assign_word_speakers(diar, {"segments": segmente})["segments"], embeddings
        finally:
            del pipe
            gpu_freigeben()
