# PyInstaller: Taleward Worker als Ordner mit TalewardWorker(.exe). Aufruf aus worker-app/:
#   uv run pyinstaller packaging/taleward-worker.spec --noconfirm
# Unter Windows muss vorher bin/uv.exe liegen (lädt der GitHub-Ablauf herunter).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

wurzel = Path(SPECPATH).parent
windows = sys.platform == "win32"
daten = [(str(wurzel / "taleward_worker" / "ui"), "taleward_worker/ui")]
uv = wurzel / "bin" / ("uv.exe" if windows else "uv")
if uv.exists():
    daten.append((str(uv), "taleward_worker/bin"))

versteckt = collect_submodules("pystray") + ["webview.platforms.winforms", "webview.platforms.edgechromium"] \
    if windows else collect_submodules("pystray") + ["webview.platforms.qt"]

a = Analysis([str(wurzel / "taleward_worker" / "__main__.py")], pathex=[str(wurzel)], datas=daten,
             hiddenimports=versteckt, excludes=["tkinter", "matplotlib", "numpy"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TalewardWorker", console=False,
          icon=str(wurzel / "packaging" / "windows" / "taleward-worker.ico"), upx=False)
COLLECT(exe, a.binaries, a.datas, name="TalewardWorker", upx=False)
