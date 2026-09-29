"""Transkriptions-Probelauf: WhisperX + pyannote auf einer Audiodatei, ohne App und ohne Datenbank.

Aufruf:  uv run chronik probelauf /pfad/zur/folge.mp3 [Optionen]
Ergebnis: data/probelauf/<name>-<zeit>/  mit transkript.txt, transkript.json, bericht.md
"""
from __future__ import annotations

import ctypes
import gc
import glob
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SAMPLE_RATE = 16000


class ProbelaufFehler(Exception):
    """Fehler mit verständlicher Meldung für die Kommandozeile."""


# ---------------------------------------------------------------- Hilfsfunktionen
def fmt_zeit(sek: float) -> str:
    sek = int(round(sek))
    return f"{sek // 3600:02d}:{sek % 3600 // 60:02d}:{sek % 60:02d}"


def fmt_dauer(sek: float) -> str:
    if sek < 90:
        return f"{sek:.0f} s"
    return f"{sek / 60:.1f} min"


class VramMesser:
    """Misst den höchsten Grafikspeicherverbrauch über nvidia-smi (erfasst auch CTranslate2, nicht nur PyTorch)."""

    def __init__(self):
        self.start_mb: int | None = None
        self.max_mb: int = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _lesen() -> int | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().splitlines()[0]
            return int(out.split(",")[0])
        except Exception:
            return None

    @staticmethod
    def gesamt_mb() -> int | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().splitlines()[0]
            return int(out)
        except Exception:
            return None

    def __enter__(self):
        self.start_mb = self._lesen()
        self.max_mb = self.start_mb or 0

        def loop():
            while not self._stop.wait(1.0):
                v = self._lesen()
                if v is not None and v > self.max_mb:
                    self.max_mb = v

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


@dataclass
class Phase:
    name: str
    sekunden: float
    vram_max_mb: int | None


@dataclass
class Bericht:
    phasen: list[Phase] = field(default_factory=list)

    def messen(self, name: str):
        bericht = self

        class _Ctx:
            def __enter__(self_inner):
                print(f"▶ {name} …", flush=True)
                self_inner.t0 = time.perf_counter()
                self_inner.vram = VramMesser().__enter__()
                return self_inner

            def __exit__(self_inner, exc_type, *rest):
                self_inner.vram.__exit__()
                dauer = time.perf_counter() - self_inner.t0
                bericht.phasen.append(Phase(name, dauer, self_inner.vram.max_mb or None))
                if exc_type is None:
                    print(f"  ✓ {name}: {fmt_dauer(dauer)}", flush=True)
                return False

        return _Ctx()


def cuda_bibliotheken_vorladen() -> None:
    """CTranslate2 findet cuDNN/cuBLAS aus den pip-Paketen sonst manchmal nicht (libcudnn_ops.so.9 …).
    Windows: PyTorch bringt die DLLs in torch/lib mit – den Ordner für CTranslate2 auffindbar machen."""
    if sys.platform == "win32":
        import importlib.util
        import os

        spec = importlib.util.find_spec("torch")
        if spec and spec.origin:
            lib = str(Path(spec.origin).parent / "lib")
            if os.path.isdir(lib):
                os.add_dll_directory(lib)
                os.environ["PATH"] = lib + os.pathsep + os.environ.get("PATH", "")
        return
    try:
        import nvidia  # noqa: F401  (Namespace der pip-Pakete nvidia-*)
    except ImportError:
        return
    basis = Path(sys.modules["nvidia"].__path__[0])
    for muster in ("cublas/lib/libcublas*.so.*", "cudnn/lib/libcudnn*.so.*"):
        for pfad in sorted(glob.glob(str(basis / muster))):
            try:
                ctypes.CDLL(pfad, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


def gpu_freigeben() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# ---------------------------------------------------------------- Audio vorbereiten
# Simuliert ein Handy mitten auf dem Tisch: Mono, weniger Höhen/Tiefen, Raumhall, Grundrauschen.
TISCH_FILTER = (
    "[0:a]aformat=channel_layouts=mono,aresample=16000,"
    "highpass=f=150,lowpass=f=6500,"
    "aecho=0.8:0.55:23|47|71:0.35|0.25|0.15,"
    "volume=0.8[s];"
    "anoisesrc=color=pink:amplitude=0.02:sample_rate=16000[n];"
    # amix halbiert die Lautstärke bei zwei Eingängen – volume=2 gleicht das aus (auch mit ffmpeg 4.4)
    "[s][n]amix=inputs=2:duration=first,volume=2[out]"
)


def audio_vorbereiten(quelle: Path, ziel: Path, ab_min: float, dauer_min: float | None, tisch: bool) -> float:
    """Schneidet/wandelt nach 16 kHz mono WAV. Gibt die Dauer in Sekunden zurück."""
    if shutil.which("ffmpeg") is None:
        raise ProbelaufFehler("ffmpeg fehlt. Installieren mit: sudo apt install -y ffmpeg")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if ab_min > 0:
        cmd += ["-ss", str(ab_min * 60)]
    if dauer_min:
        cmd += ["-t", str(dauer_min * 60)]
    cmd += ["-i", str(quelle)]
    if tisch:
        cmd += ["-filter_complex", TISCH_FILTER, "-map", "[out]"]
    else:
        cmd += ["-ac", "1", "-ar", str(SAMPLE_RATE)]
    cmd += ["-c:a", "pcm_s16le", str(ziel)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise ProbelaufFehler(f"ffmpeg konnte die Datei nicht lesen:\n{res.stderr.strip()[-800:]}")
    dauer = ziel.stat().st_size / (SAMPLE_RATE * 2)  # 16 bit mono, WAV-Kopf vernachlässigbar
    if dauer < 5:
        raise ProbelaufFehler("Die Aufnahme ist kürzer als 5 Sekunden – falscher Ausschnitt?")
    return dauer


def hoerprobe_speichern(wav: Path, ziel: Path, dauer_s: float) -> None:
    """60 s aus der Mitte als MP3, damit man die Tisch-Simulation anhören kann."""
    start = max(0.0, dauer_s / 2 - 30)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-t", "60",
         "-i", str(wav), "-c:a", "libmp3lame", "-q:a", "5", str(ziel)],
        capture_output=True,
    )


# ---------------------------------------------------------------- Namenshilfe
def namens_echo_entfernen(segmente: list[dict], namen: list[str]) -> tuple[list[dict], list[dict]]:
    """Entfernt Abschnitte, in denen Whisper nur die Namensliste „nachplappert“ (Halluzination bei Stille/Rauschen)."""
    if not namen:
        return segmente, []
    namenswoerter = {w.casefold() for n in namen for w in re.findall(r"\w+", n)} | {"namen", "name"}
    behalten, entfernt = [], []
    for seg in segmente:
        woerter = [w.casefold() for w in re.findall(r"\w+", seg.get("text", ""))]
        treffer = sum(1 for w in woerter if w in namenswoerter)
        verschiedene = len({w for w in woerter if w in namenswoerter})
        if len(woerter) >= 3 and verschiedene >= 3 and treffer / len(woerter) >= 0.7:
            entfernt.append(seg)
        else:
            behalten.append(seg)
    return behalten, entfernt


# ---------------------------------------------------------------- Vorstellungsrunde
_VORSTELLUNG = [
    re.compile(
        r"\bich bin (?:der |die )?(?P<person>[A-ZÄÖÜ][\wäöüß\-]+)"
        r"[^.?!]{0,40}?\b(?:spiele|spiel)\s+(?:heute\s+)?(?:den |die |das |einen |eine |ein )?"
        r"(?P<figur>[\wÄÖÜäöüß][\wÄÖÜäöüß\- ]{1,40})",
        re.IGNORECASE,
    ),
    re.compile(r"\bmein(?:e)? (?:Charakter|Figur|Held|Heldin)\s+(?:heißt|ist)\s+(?P<figur>[\wÄÖÜäöüß\- ]{2,40})", re.IGNORECASE),
    re.compile(r"\bich (?:spiele|spiel)\s+(?:heute\s+)?(?:den |die |das |einen |eine |ein )(?P<figur>[\wÄÖÜäöüß\- ]{2,40})", re.IGNORECASE),
    re.compile(
        r"\bich bin (?:der |die )?(?P<person>[A-ZÄÖÜ][\wäöüß\-]+)[^.?!]{0,30}?\b(?:leite|bin (?:euer |eure |der |die )?"
        r"(?:Spielleit\w*|Meister\w*|SL|GM|DM))",
        re.IGNORECASE,
    ),
]


def vorstellungen_finden(segmente: list[dict], bis_sekunde: float = 15 * 60) -> list[dict]:
    treffer = []
    for seg in segmente:
        if seg["start"] > bis_sekunde:
            break
        text = seg.get("text", "")
        for rx in _VORSTELLUNG:
            m = rx.search(text)
            if m:
                treffer.append({
                    "start": seg["start"],
                    "sprecher": seg.get("speaker", "?"),
                    "person": (m.groupdict().get("person") or "").strip() or None,
                    "figur": (m.groupdict().get("figur") or "").strip(" ,.") or None,
                    "zitat": text.strip()[:160],
                })
                break
    return treffer


# ---------------------------------------------------------------- Ausgabe
def absaetze_bilden(segmente: list[dict]) -> list[dict]:
    """Aufeinanderfolgende Segmente derselben Stimme zu Absätzen zusammenfassen."""
    out: list[dict] = []
    for seg in segmente:
        spk = seg.get("speaker") or "UNBEKANNT"
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if out and out[-1]["speaker"] == spk and seg["start"] - out[-1]["end"] < 2.0:
            out[-1]["text"] += " " + text
            out[-1]["end"] = seg["end"]
        else:
            out.append({"speaker": spk, "start": seg["start"], "end": seg["end"], "text": text})
    return out


def sprecher_namen(segmente: list[dict]) -> dict[str, str]:
    """SPEAKER_00 → „Stimme 1“ usw., sortiert nach Redeanteil."""
    anteil: dict[str, float] = {}
    for s in segmente:
        spk = s.get("speaker")
        if spk:
            anteil[spk] = anteil.get(spk, 0) + (s["end"] - s["start"])
    reihenfolge = sorted(anteil, key=lambda k: -anteil[k])
    return {spk: f"Stimme {i + 1}" for i, spk in enumerate(reihenfolge)}


def sprecher_statistik(segmente: list[dict], namen: dict[str, str]) -> list[dict]:
    stats: dict[str, dict] = {}
    gesamt = 0.0
    for s in segmente:
        spk = s.get("speaker")
        if not spk or spk == "UNBEKANNT":  # wird separat als Hinweis gezählt
            continue
        d = s["end"] - s["start"]
        gesamt += d
        st = stats.setdefault(spk, {"sekunden": 0.0, "segmente": 0, "beispiele": []})
        st["sekunden"] += d
        st["segmente"] += 1
        if len(st["beispiele"]) < 3 and len(s.get("text", "")) > 40 and s["start"] > 60 * len(st["beispiele"]):
            st["beispiele"].append((s["start"], s["text"].strip()[:140]))
    rows = []
    for spk, st in sorted(stats.items(), key=lambda kv: -kv[1]["sekunden"]):
        rows.append({
            "name": namen.get(spk, spk), "roh": spk, "sekunden": st["sekunden"],
            "anteil": st["sekunden"] / gesamt if gesamt else 0, "segmente": st["segmente"],
            "beispiele": st["beispiele"],
        })
    return rows


def ausgabe_schreiben(ordner: Path, segmente: list[dict], namen: dict[str, str]) -> None:
    absaetze = absaetze_bilden(segmente)
    with open(ordner / "transkript.txt", "w", encoding="utf-8") as f:
        for a in absaetze:
            f.write(f"[{fmt_zeit(a['start'])}] {namen.get(a['speaker'], a['speaker'])}: {a['text']}\n\n")
    daten = [
        {"start": round(s["start"], 2), "end": round(s["end"], 2),
         "speaker": namen.get(s.get("speaker"), s.get("speaker")), "text": (s.get("text") or "").strip()}
        for s in segmente
    ]
    (ordner / "transkript.json").write_text(json.dumps(daten, ensure_ascii=False, indent=1), encoding="utf-8")


def bericht_schreiben(ordner: Path, *, quelle: Path, einst: dict, dauer_audio: float, bericht: Bericht,
                      stats: list[dict], vorstellungen: list[dict], vram_gesamt: int | None,
                      vram_start: int | None, hinweise: list[str]) -> str:
    z = [f"# Probelauf: {quelle.name}", "", f"Erstellt: {datetime.now():%d.%m.%Y %H:%M}", ""]
    z += ["## Einstellungen", ""]
    z += [f"- {k}: {v}" for k, v in einst.items()]
    z += ["", "## Laufzeit", "", f"Audiolänge: **{fmt_dauer(dauer_audio)}** ({fmt_zeit(dauer_audio)})", ""]
    z += ["| Phase | Dauer | Echtzeitfaktor | Grafikspeicher max. |", "|---|---|---|---|"]
    gesamt = 0.0
    for p in bericht.phasen:
        gesamt += p.sekunden
        faktor = f"{dauer_audio / p.sekunden:.0f}× schneller" if p.sekunden > 0 else "–"
        vram = f"{p.vram_max_mb / 1024:.1f} GB" if p.vram_max_mb else "–"
        z.append(f"| {p.name} | {fmt_dauer(p.sekunden)} | {faktor} | {vram} |")
    z.append(f"| **Gesamt** | **{fmt_dauer(gesamt)}** | {dauer_audio / gesamt:.0f}× schneller | |")
    if vram_gesamt:
        z += ["", f"Grafikkarte: {vram_gesamt / 1024:.1f} GB gesamt, davon vor dem Start schon "
                  f"{(vram_start or 0) / 1024:.1f} GB belegt (Windows, Bildschirm)."]
    if gesamt > 0:
        z += ["", f"Hochgerechnet auf 4 Stunden Audio: etwa **{fmt_dauer(gesamt / dauer_audio * 4 * 3600)}**."]
    if stats:
        z += ["", "## Erkannte Stimmen", "",
              "| Stimme | Redezeit | Anteil | Abschnitte |", "|---|---|---|---|"]
        for s in stats:
            z.append(f"| {s['name']} | {fmt_dauer(s['sekunden'])} | {s['anteil'] * 100:.0f} % | {s['segmente']} |")
        z += ["", "### Beispiele je Stimme (zum Prüfen, ob die Trennung stimmt)", ""]
        for s in stats:
            z.append(f"**{s['name']}**")
            for t, txt in s["beispiele"]:
                z.append(f"- [{fmt_zeit(t)}] {txt}")
            z.append("")
    z += ["## Vorstellungsrunde (erste 15 Minuten)", ""]
    if vorstellungen:
        z += ["| Zeit | Stimme | Person | Figur | Zitat |", "|---|---|---|---|---|"]
        for v in vorstellungen:
            z.append(f"| {fmt_zeit(v['start'])} | {v['sprecher']} | {v['person'] or '–'} | {v['figur'] or '–'} | "
                     f"{v['zitat'].replace('|', '/')} |")
    else:
        z.append("Keine Sätze wie „Ich bin … und spiele …“ gefunden.")
    if hinweise:
        z += ["", "## Hinweise", ""] + [f"- {h}" for h in hinweise]
    z += ["", "## Bitte prüfen", "",
          "1. Stimmen die Sprecherwechsel? Stichproben in `transkript.txt` gegen das Original anhören.",
          "2. Werden Eigennamen (Orte, Figuren) richtig geschrieben?",
          "3. Gibt es erfundene Sätze in Pausen oder Musik (typischer Whisper-Fehler)?",
          "4. Ist eine Stimme in zwei aufgeteilt oder sind zwei Stimmen zusammengelegt?"]
    text = "\n".join(z) + "\n"
    (ordner / "bericht.md").write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------- Hauptablauf
def ausfuehren(
    quelle: Path, *, sprache: str, modell: str, genauigkeit: str, batch: int,
    ab_min: float, dauer_min: float | None, tisch: bool, sprecher: int | None,
    min_sprecher: int | None, max_sprecher: int | None, namen: str | None,
    ohne_sprecher: bool, hf_token: str | None, ausgabe_basis: Path,
) -> Path:
    if not quelle.is_file():
        raise ProbelaufFehler(f"Datei nicht gefunden: {quelle}")
    if not ohne_sprecher and not hf_token:
        raise ProbelaufFehler(
            "Für die Sprechertrennung fehlt HF_TOKEN in der .env.\n"
            "Anleitung: docs/ENTWICKLUNG.md, „Echte Transkription“. Oder vorerst ohne Sprechertrennung: --ohne-sprecher"
        )

    cuda_bibliotheken_vorladen()
    try:
        import torch
    except ImportError:
        raise ProbelaufFehler("Die KI-Pakete fehlen. Installieren mit: uv sync --extra ki")
    if not torch.cuda.is_available():
        raise ProbelaufFehler(
            "PyTorch sieht keine Grafikkarte. Prüfe `nvidia-smi` in Ubuntu und aktualisiere ggf. den "
            "NVIDIA-Treiber (unter WSL: in Windows), siehe docs/ENTWICKLUNG.md."
        )
    import os

    os.environ["PYANNOTE_METRICS_ENABLED"] = "false"  # keine Nutzungsdaten an pyannote.ai
    import whisperx
    from whisperx.diarize import DiarizationPipeline, assign_word_speakers

    from app import modelle

    try:  # gleiche feste Fassungen wie der Worker
        whisper_pfad = str(modelle.bereitstellen(modelle.whisper_repo(modell), hf_token).pfad)
        sprecher_pfad = (modelle.SPRECHERMODELL if ohne_sprecher
                         else str(modelle.bereitstellen(modelle.SPRECHERMODELL, hf_token).pfad))
    except modelle.ModellFehler as e:
        raise ProbelaufFehler(str(e)) from None

    stempel = datetime.now().strftime("%Y%m%d-%H%M")
    ordner = ausgabe_basis / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', quelle.stem)[:40]}-{stempel}"
    ordner.mkdir(parents=True, exist_ok=True)
    wav = ordner / "audio16k.wav"
    bericht = Bericht()
    hinweise: list[str] = []
    vram_gesamt = VramMesser.gesamt_mb()
    vram_start = VramMesser._lesen()
    print(f"Grafikkarte: {torch.cuda.get_device_name(0)}, {(vram_gesamt or 0) / 1024:.1f} GB, "
          f"davon {(vram_start or 0) / 1024:.1f} GB schon belegt")

    try:
        with bericht.messen("Audio vorbereiten"):
            dauer = audio_vorbereiten(quelle, wav, ab_min, dauer_min, tisch)
            if tisch:
                hoerprobe_speichern(wav, ordner / "tisch-hoerprobe.mp3", dauer)
        print(f"  Audiolänge: {fmt_zeit(dauer)}")
        audio = whisperx.load_audio(str(wav))

        asr_options = {}
        namensliste = [n.strip() for n in (namen or "").split(",") if n.strip()]
        if namensliste:
            # Nur „hotwords“, kein initial_prompt: Ein Einleitungssatz wurde bei schlechtem Ton
            # schon einmal wörtlich ins Transkript übernommen („Namen, Jemma Reed, …“).
            asr_options["hotwords"] = ", ".join(namensliste)

        with bericht.messen("Transkription (Whisper)"):
            try:
                model = whisperx.load_model(
                    whisper_pfad, "cuda", compute_type=genauigkeit, language=sprache, asr_options=asr_options or None
                )
            except Exception as e:
                if "cudnn" in str(e).lower() or "libcu" in str(e).lower():
                    raise ProbelaufFehler(f"CUDA-Bibliothek nicht gefunden: {e}")
                raise
            ergebnis = model.transcribe(audio, batch_size=batch, language=sprache, print_progress=True)
            ergebnis["segments"], entfernt = namens_echo_entfernen(ergebnis["segments"], namensliste)
            for seg in entfernt:
                hinweise.append(
                    f"Abschnitt bei {fmt_zeit(seg['start'])} entfernt, weil er nur die Namenshilfe wiederholt: "
                    f"„{seg['text'].strip()[:80]}“"
                )
            del model
            gpu_freigeben()

        with bericht.messen("Wortgenaue Zeitstempel (Alignment)"):
            model_a, meta = whisperx.load_align_model(language_code=sprache, device="cuda")
            ergebnis = whisperx.align(ergebnis["segments"], model_a, meta, audio, "cuda",
                                      return_char_alignments=False)
            del model_a
            gpu_freigeben()

        segmente = ergebnis["segments"]
        if not ohne_sprecher:
            with bericht.messen("Sprechertrennung (pyannote)"):
                try:
                    pipe = DiarizationPipeline(model_name=sprecher_pfad, token=hf_token, device="cuda")
                except Exception as e:
                    msg = str(e)
                    if any(k in msg for k in ("401", "403", "gated", "Unauthorized", "restricted", "authoriz")):
                        raise ProbelaufFehler(
                            "Hugging Face verweigert den Zugriff auf das Sprechermodell. Prüfe:\n"
                            "  1. Bedingungen akzeptiert auf https://huggingface.co/pyannote/speaker-diarization-community-1\n"
                            "  2. HF_TOKEN in der .env richtig kopiert (beginnt mit hf_)"
                        )
                    raise
                kw = {}
                if sprecher:
                    kw["num_speakers"] = sprecher
                else:
                    if min_sprecher:
                        kw["min_speakers"] = min_sprecher
                    if max_sprecher:
                        kw["max_speakers"] = max_sprecher
                diar = pipe(audio, **kw)
                ergebnis = assign_word_speakers(diar, ergebnis)
                segmente = ergebnis["segments"]
                del pipe
                gpu_freigeben()

        namen_map = sprecher_namen(segmente) if not ohne_sprecher else {}
        for s in segmente:
            if "speaker" not in s or s["speaker"] is None:
                s["speaker"] = None if ohne_sprecher else "UNBEKANNT"
        stats = sprecher_statistik(segmente, namen_map) if not ohne_sprecher else []
        vorstellungen = vorstellungen_finden(
            [{**s, "speaker": namen_map.get(s.get("speaker"), s.get("speaker") or "–")} for s in segmente]
        )

        # Hinweise für die Auswertung
        if stats:
            klein = [s for s in stats if s["anteil"] < 0.02]
            if klein:
                hinweise.append(
                    f"{len(klein)} Stimme(n) mit unter 2 % Redeanteil – oft Fehlzuordnungen, Einspieler oder Musik. "
                    "Mit --max-sprecher lässt sich das begrenzen."
                )
            ohne = sum(1 for s in segmente if s.get("speaker") in (None, "UNBEKANNT"))
            if ohne:
                hinweise.append(f"{ohne} Abschnitte ohne Stimmzuordnung (meist Überlappungen oder sehr kurze Einwürfe).")
        leer = sum(1 for s in segmente if len((s.get("text") or "").strip()) < 2)
        if leer:
            hinweise.append(f"{leer} fast leere Abschnitte.")

        ausgabe_schreiben(ordner, segmente, namen_map)
        einst = {
            "Datei": str(quelle), "Sprache": sprache, "Modell": modell, "Genauigkeit": genauigkeit,
            "Batch": batch, "Ausschnitt": f"ab {ab_min:g} min" + (f", {dauer_min:g} min lang" if dauer_min else ", bis Ende"),
            "Tisch-Simulation": "ja" if tisch else "nein",
            "Sprecher": (str(sprecher) if sprecher else f"automatisch (min {min_sprecher or '–'}, max {max_sprecher or '–'})")
            if not ohne_sprecher else "keine Sprechertrennung",
            "Namenshilfe": namen or "–",
        }
        bericht_schreiben(
            ordner, quelle=quelle, einst=einst, dauer_audio=dauer, bericht=bericht, stats=stats,
            vorstellungen=vorstellungen, vram_gesamt=vram_gesamt, vram_start=vram_start, hinweise=hinweise,
        )
    finally:
        # Das umgewandelte Audio wird nicht aufbewahrt (Datenschutz, Speicherplatz)
        if wav.exists():
            wav.unlink()
    return ordner
