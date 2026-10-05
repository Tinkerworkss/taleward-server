"""Probelauf ohne echte KI-Modelle: Audio-Vorbereitung, Textauswertung und Ablauf mit Attrappen."""
import shutil
import subprocess
import sys
import types

import pytest

from app import probelauf as pl

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg fehlt")


def _testton(pfad, sekunden=20):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={sekunden}",
         "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", str(pfad)],
        check=True,
    )


@needs_ffmpeg
def test_audio_vorbereiten_normal_und_ausschnitt(tmp_path):
    src = tmp_path / "folge.mp3"
    _testton(src, 90)
    d = pl.audio_vorbereiten(src, tmp_path / "a.wav", ab_min=0, dauer_min=None, tisch=False)
    assert 89 < d < 91
    d = pl.audio_vorbereiten(src, tmp_path / "b.wav", ab_min=0.5, dauer_min=0.5, tisch=False)
    assert 29 < d < 31


@needs_ffmpeg
def test_tisch_simulation_und_hoerprobe(tmp_path):
    src = tmp_path / "folge.mp3"
    _testton(src, 70)
    wav = tmp_path / "t.wav"
    d = pl.audio_vorbereiten(src, wav, ab_min=0, dauer_min=None, tisch=True)
    assert 69 < d < 71  # Rauschquelle verlängert nichts
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=channels,sample_rate",
                          "-of", "csv=p=0", str(wav)], capture_output=True, text=True).stdout.strip()
    assert out == "16000,1"
    pl.hoerprobe_speichern(wav, tmp_path / "probe.mp3", d)
    assert (tmp_path / "probe.mp3").stat().st_size > 10000


@needs_ffmpeg
def test_kaputte_datei(tmp_path):
    src = tmp_path / "kaputt.mp3"
    src.write_bytes(b"keine audiodatei")
    with pytest.raises(pl.ProbelaufFehler, match="ffmpeg"):
        pl.audio_vorbereiten(src, tmp_path / "x.wav", 0, None, False)


def test_vorstellungsrunde():
    seg = [
        {"start": 10, "speaker": "Stimme 2", "text": "Also, ich bin Lena und ich spiele die Elfe Aranel Sternblatt."},
        {"start": 20, "speaker": "Stimme 3", "text": "Ich bin der Tobi, ich spiele einen Zwergen namens Grimbart."},
        {"start": 30, "speaker": "Stimme 1", "text": "Und ich bin Kai und leite heute die Runde."},
        {"start": 40, "speaker": "Stimme 4", "text": "Mein Charakter heißt Rondrik vom Walde."},
        {"start": 50, "speaker": "Stimme 2", "text": "Ich hol mir erstmal was zu trinken."},
        {"start": 2000, "speaker": "Stimme 2", "text": "Ich bin Lena und spiele immer noch die Elfe."},
    ]
    t = pl.vorstellungen_finden(seg)
    assert [x["sprecher"] for x in t] == ["Stimme 2", "Stimme 3", "Stimme 1", "Stimme 4"]
    assert t[0]["person"] == "Lena" and t[0]["figur"].startswith("Elfe Aranel")
    assert t[1]["person"] == "Tobi" and "Zwergen" in t[1]["figur"]
    assert t[2]["person"] == "Kai" and t[2]["figur"] is None
    assert t[3]["figur"].startswith("Rondrik")


def test_absaetze_und_namen():
    seg = [
        {"start": 0, "end": 2, "speaker": "SPEAKER_01", "text": "Hallo"},
        {"start": 2.5, "end": 4, "speaker": "SPEAKER_01", "text": "zusammen."},
        {"start": 4, "end": 30, "speaker": "SPEAKER_00", "text": "Willkommen in Gareth."},
    ]
    a = pl.absaetze_bilden(seg)
    assert len(a) == 2 and a[0]["text"] == "Hallo zusammen."
    assert pl.sprecher_namen(seg) == {"SPEAKER_00": "Stimme 1", "SPEAKER_01": "Stimme 2"}


# ---------- kompletter Ablauf mit Attrappen statt Modellen ----------
def _attrappen(monkeypatch, fehler_bei_diarisierung=None):
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: True, get_device_name=lambda i: "Testkarte",
                                       empty_cache=lambda: None)
    wx = types.ModuleType("whisperx")
    wx.load_audio = lambda p: [0.0] * 10

    class Modell:
        def transcribe(self, audio, **kw):
            return {"segments": [
                {"start": 1.0, "end": 5.0, "text": " Ich bin Lena und spiele die Elfe Aranel."},
                {"start": 6.0, "end": 9.0, "text": " Und ich bin Kai und leite."},
                {"start": 30.0, "end": 60.0, "text": " Ihr betretet die Taverne in Gareth, es riecht nach Bier."},
            ], "language": "de"}

    wx.load_model = lambda *a, **kw: Modell()
    wx.load_align_model = lambda **kw: ("m", {})
    wx.align = lambda segs, *a, **kw: {"segments": segs}
    diar_mod = types.ModuleType("whisperx.diarize")

    class Pipe:
        def __init__(self, token, device, model_name=None):
            if fehler_bei_diarisierung:
                raise RuntimeError(fehler_bei_diarisierung)

        def __call__(self, audio, **kw):
            return "df"

    def assign(df, ergebnis):
        for i, s in enumerate(ergebnis["segments"]):
            s["speaker"] = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_01"][i]
        return ergebnis

    diar_mod.DiarizationPipeline = Pipe
    diar_mod.assign_word_speakers = assign
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "whisperx", wx)
    monkeypatch.setitem(sys.modules, "whisperx.diarize", diar_mod)
    monkeypatch.setattr(pl, "cuda_bibliotheken_vorladen", lambda: None)
    from pathlib import Path

    from app import modelle
    monkeypatch.setattr(modelle, "bereitstellen", lambda repo, token=None: modelle.Bereit(repo, "abc", Path(repo), "neu"))


def _lauf(tmp_path, **kw):
    src = tmp_path / "Folge 12 – Die Taverne.mp3"
    _testton(src, 30)
    args = dict(sprache="de", modell="large-v3", genauigkeit="int8_float16", batch=8, ab_min=0, dauer_min=None,
                tisch=False, sprecher=None, min_sprecher=None, max_sprecher=None, namen="Gareth,Aranel",
                ohne_sprecher=False, hf_token="hf_test", ausgabe_basis=tmp_path / "out")
    args.update(kw)
    return pl.ausfuehren(src, **args)


@needs_ffmpeg
def test_ablauf_mit_attrappen(tmp_path, monkeypatch):
    _attrappen(monkeypatch)
    ordner = _lauf(tmp_path)
    txt = (ordner / "transkript.txt").read_text()
    assert "] Stimme 1: Ihr betretet die Taverne" in txt
    assert "] Stimme 2: Ich bin Lena" in txt
    bericht = (ordner / "bericht.md").read_text()
    assert "Transkription (Whisper)" in bericht and "Sprechertrennung" in bericht
    assert "| Lena |" in bericht and "Elfe Aranel" in bericht
    assert "Hochgerechnet auf 4 Stunden" in bericht
    assert not (ordner / "audio16k.wav").exists()  # umgewandeltes Audio wird gelöscht


@needs_ffmpeg
def test_ablauf_ohne_sprecher_und_tisch(tmp_path, monkeypatch):
    _attrappen(monkeypatch)
    ordner = _lauf(tmp_path, ohne_sprecher=True, hf_token=None, tisch=True)
    assert (ordner / "tisch-hoerprobe.mp3").exists()
    assert "keine Sprechertrennung" in (ordner / "bericht.md").read_text()


@needs_ffmpeg
def test_gesperrtes_modell_gibt_verstaendliche_meldung(tmp_path, monkeypatch):
    _attrappen(monkeypatch, fehler_bei_diarisierung="401 Client Error: Unauthorized, gated repo")
    with pytest.raises(pl.ProbelaufFehler, match="Bedingungen akzeptiert"):
        _lauf(tmp_path)


def test_fehlender_token(tmp_path):
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    with pytest.raises(pl.ProbelaufFehler, match="HF_TOKEN"):
        pl.ausfuehren(src, sprache="de", modell="x", genauigkeit="x", batch=1, ab_min=0, dauer_min=None,
                      tisch=False, sprecher=None, min_sprecher=None, max_sprecher=None, namen=None,
                      ohne_sprecher=False, hf_token="", ausgabe_basis=tmp_path)


def test_namens_echo_wird_entfernt():
    namen = ["Jemma Reed", "Litha Flamel", "Tubo", "Lilio", "saroman", "Phybe", "zeitiger"]
    seg = [
        {"start": 85, "text": " Namen, Jemma Reed, Litha Flamel, Tubo, Lilio, saroman, Phybe zeitiger."},
        {"start": 90, "text": " Jemma und Litha gehen zu Tubo an den Tisch."},
        {"start": 92, "text": " Jemma, Litha und Tubo waren gestern bei Phybe."},
        {"start": 95, "text": " Tubo!"},
    ]
    behalten, entfernt = pl.namens_echo_entfernen(seg, namen)
    assert [s["start"] for s in entfernt] == [85]
    # Echte Lore-Sätze mit mehreren Namen dürfen nicht als Hotword-Echo verschwinden.
    assert [s["start"] for s in behalten] == [90, 92, 95]
    assert pl.namens_echo_entfernen(seg, [])[1] == []
