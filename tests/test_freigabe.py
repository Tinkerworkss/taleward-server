"""Freigabe: Nur unterschriebene Fassungen werden geladen, angeboten und an Worker weitergereicht."""
import hashlib

import pytest

from tests.freigabe_hilfe import freigabe, unterschreiben
from tests.test_aktualisierung import APK, EXE, github, release
from tests.test_step2a import worker_token

# Mit ssh-keygen -Y sign -n taleward-freigabe erzeugt (OpenSSH 9.6) – so wie im Ablauf „Freigabe“
ECHT_SCHLUESSEL = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP6wpTFK9FF6IfXPsVJ5R2nFSQYW0RV+zi02WLlKS0JU test"
ECHT_TEXT = ("taleward-freigabe 1\nrepo Tinkerworkss/taleward-server\ntag v0.4.38\ncommit " + "a" * 40
             + "\ndatei " + "b" * 64 + " a.zip\n").encode()
ECHT_SIG = """-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg/rClMUr0UXoh9c+xUnlHacVJBh
bRFX7OLTZYuUpLQlQAAAARdGFsZXdhcmQtZnJlaWdhYmUAAAAAAAAABnNoYTUxMgAAAFMA
AAALc3NoLWVkMjU1MTkAAABAetpQ108l5M3dX4dPHQynskJdaIquFoYpHOh5CT1w5+E9//
nwqMOihot7qT3bFYE/491me28LVrX43pd0wGXBDw==
-----END SSH SIGNATURE-----
"""


def test_unterschrift_von_ssh_keygen(monkeypatch):
    from app import freigabe as fg

    monkeypatch.setattr(fg, "OEFFENTLICHER_SCHLUESSEL", ECHT_SCHLUESSEL)
    f = fg.lesen(ECHT_TEXT, ECHT_SIG, "Tinkerworkss/taleward-server", "v0.4.38")
    assert f.commit == "a" * 40 and f.dateien == {"a.zip": "b" * 64}
    with pytest.raises(fg.FreigabeFehler):
        fg.lesen(ECHT_TEXT + b"datei " + b"c" * 64 + b" boese.exe\n", ECHT_SIG, "Tinkerworkss/taleward-server",
                 "v0.4.38")
    with pytest.raises(fg.FreigabeFehler, match="anderen Fassung"):
        fg.lesen(ECHT_TEXT, ECHT_SIG, "Tinkerworkss/taleward-server", "v0.4.39")
    with pytest.raises(fg.FreigabeFehler, match="anderen Fassung"):
        fg.lesen(ECHT_TEXT, ECHT_SIG, "Fremd/taleward-server", "v0.4.38")


def test_echter_schluessel_steht_im_code():
    from app import freigabe as fg

    assert fg.OEFFENTLICHER_SCHLUESSEL != ""  # im Test durch den Testschlüssel ersetzt
    quelle = open(fg.__file__, encoding="utf-8").read()
    schluessel = "AAAAC3NzaC1lZDI1NTE5AAAAIP8G+vT3BsQPQ+D5UlN615BaEd1FCXEYcxO1KEf7u7Sd"
    assert schluessel in quelle
    assert schluessel in open("deploy/aktualisieren.sh", encoding="utf-8").read()


def test_fremder_schluessel_und_falscher_zweck():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from app import freigabe as fg

    text, _ = freigabe("Tinkerworkss/taleward-app", "v1.0.0")
    with pytest.raises(fg.FreigabeFehler, match="fremden Schlüssel"):
        fg.lesen(text, unterschreiben(text, schluessel=Ed25519PrivateKey.generate()), "Tinkerworkss/taleward-app",
                 "v1.0.0")
    with pytest.raises(fg.FreigabeFehler, match="anderen Zweck"):
        fg.lesen(text, unterschreiben(text, namensraum=b"file"), "Tinkerworkss/taleward-app", "v1.0.0")
    for kaputt in ["", "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----", "x" * 10000]:
        with pytest.raises(fg.FreigabeFehler):
            fg.lesen(text, kaputt, "Tinkerworkss/taleward-app", "v1.0.0")
    for zeilen in [b"taleward-freigabe 2\n", b"taleward-freigabe 1\nrepo a\ntag b\n",
                   b"taleward-freigabe 1\nrepo a\ntag b\ncommit " + b"a" * 40 + b"\ndatei x ../boese\n"]:
        with pytest.raises(fg.FreigabeFehler):
            fg.lesen(zeilen, unterschreiben(zeilen), "a", "b")


def test_ohne_freigabe_wird_uebergangen(client, dbs):
    from app import aktualisierung
    from app.einstellungen import meta_lesen

    gh = github([release("v1.3.0", "taleward-1.3.0.apk", APK + b"neu", freigegeben=False),
                 release("v1.2.0", "taleward-1.2.0.apk", APK)], [], ["v0.4.1", "v0.9.0"], {
        "/v1.3.0/taleward-1.3.0.apk": APK + b"neu", "/v1.2.0/taleward-1.2.0.apk": APK}, unsigniert=("v0.9.0",))
    ergebnis = aktualisierung.pruefen(dbs, gh)
    assert ergebnis["app"] == "1.2.0"  # 1.3.0 wartet noch auf die Freigabe
    assert ergebnis["server"] is None  # neuestes Tag ohne Freigabe → keine Meldung
    assert meta_lesen(dbs, "update.fehler") == ""
    assert client.get("/api/v1/info").json()["latestAppVersion"] == "1.2.0"


def test_ungueltige_freigabe_wird_gemeldet(client, dbs):
    from app import aktualisierung
    from app.einstellungen import meta_lesen

    boese = release("v1.3.0", "taleward-1.3.0.apk", APK + b"boese")
    for a in boese["assets"]:
        if a["name"] == "freigabe.txt.sig":
            a["browser_download_url"] = "https://dl.example/falsch/freigabe.txt.sig"
    from tests.test_aktualisierung import DOWNLOADS

    DOWNLOADS["/falsch/freigabe.txt.sig"] = unterschreiben(b"etwas anderes").encode()
    gh = github([boese, release("v1.2.0", "taleward-1.2.0.apk", APK)], [], [], {
        "/v1.3.0/taleward-1.3.0.apk": APK + b"boese", "/v1.2.0/taleward-1.2.0.apk": APK})
    assert aktualisierung.pruefen(dbs, gh)["app"] == "1.2.0"
    assert "Unterschrift ungültig" in meta_lesen(dbs, "update.fehler")
    assert not (aktualisierung.ablage() / "app" / "1.3.0").exists()


def test_web_zip_mit_falscher_freigabe_wird_nicht_freigegeben(client, dbs):
    from app import aktualisierung
    from app.einstellungen import meta_lesen

    zip_ = b"PK manipuliert"
    gh = github([release("v1.2.0", "taleward-web-1.2.0.zip", zip_, freigabe_inhalt=b"PK echt")], [], [],
                {"/v1.2.0/taleward-web-1.2.0.zip": zip_})
    aktualisierung.pruefen(dbs, gh)
    assert "Prüfsumme stimmt nicht" in meta_lesen(dbs, "update.fehler")
    assert aktualisierung.web_ordner(dbs) is None and client.get("/app/").status_code == 404


def test_datei_nicht_in_der_freigabe(client, dbs):
    from app import aktualisierung
    from app.einstellungen import meta_lesen

    rel = release("v1.2.0", "taleward-1.2.0.apk", APK)
    rel["assets"].append({"name": "taleward-1.2.0-neu.apk", "size": 1,
                          "browser_download_url": "https://dl.example/v1.2.0/taleward-1.2.0-neu.apk"})
    rel["assets"] = [a for a in rel["assets"] if a["name"] != "taleward-1.2.0.apk"]
    gh = github([rel], [], [], {})
    assert aktualisierung.pruefen(dbs, gh).get("app") is None
    assert "nicht freigegeben" in meta_lesen(dbs, "update.fehler")


def test_worker_bekommt_freigabe_mit(client, dbs):
    from app import aktualisierung
    from app import freigabe as fg

    gh = github([], [release("worker-v0.2.0", "TalewardWorker-Setup.exe", EXE)], [],
                {"/worker-v0.2.0/TalewardWorker-Setup.exe": EXE})
    aktualisierung.pruefen(dbs, gh)
    h = {"Authorization": f"Bearer {worker_token(dbs)}"}
    w = client.get("/worker/v1/app-update", params={"system": "windows"}, headers=h).json()
    f = fg.lesen(w["freigabe"].encode(), w["freigabeSignatur"], "Tinkerworkss/taleward-worker", "worker-v0.2.0")
    assert f.dateien["TalewardWorker-Setup.exe"] == hashlib.sha256(EXE).hexdigest() == w["sha256"]
    linux = client.get("/worker/v1/app-update", params={"system": "linux"}, headers=h).json()
    assert linux["commit"] == "a" * 40 and "freigabe" in linux


def _skript_mit_testschluessel(tmp_path):
    from tests.freigabe_hilfe import OEFFENTLICH

    skript = open("deploy/aktualisieren.sh", encoding="utf-8").read()
    alt = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP8G+vT3BsQPQ+D5UlN615BaEd1FCXEYcxO1KEf7u7Sd"
    pfad = tmp_path / "aktualisieren.sh"
    pfad.write_text(skript.replace(alt, OEFFENTLICH), encoding="utf-8")
    return pfad


@pytest.mark.skipif(not __import__("shutil").which("ssh-keygen"), reason="ssh-keygen fehlt")
def test_aktualisieren_sh_prueft_freigabe(tmp_path):
    import subprocess

    skript = _skript_mit_testschluessel(tmp_path)
    ordner = tmp_path / "f"
    ordner.mkdir()

    def pruefen(text: bytes, sig: str, tag="v0.4.38"):
        (ordner / "freigabe.txt").write_bytes(text)
        (ordner / "freigabe.txt.sig").write_text(sig)
        return subprocess.run(["bash", str(skript), "--freigabe-pruefen", str(ordner), tag], capture_output=True,
                              text=True)

    text, sig = freigabe("Tinkerworkss/taleward-server", "v0.4.38", commit="c" * 40)
    r = pruefen(text, sig)
    assert r.returncode == 0 and r.stdout.strip() == "c" * 40
    assert pruefen(text, sig, tag="v0.4.39").returncode != 0
    assert pruefen(text + b"commit " + b"d" * 40 + b"\n", sig).returncode != 0  # verändert
    fremd, fremd_sig = freigabe("Tinkerworkss/taleward-app", "v0.4.38")
    assert pruefen(fremd, fremd_sig).returncode != 0  # anderes Repo
    doppelt = b"taleward-freigabe 1\nrepo Tinkerworkss/taleward-server\ntag v0.4.38\ncommit " + b"c" * 40 + \
        b"\ncommit " + b"d" * 40 + b"\n"
    assert pruefen(doppelt, unterschreiben(doppelt)).returncode != 0
    assert pruefen(text, unterschreiben(text, namensraum=b"file")).returncode != 0
