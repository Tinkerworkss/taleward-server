"""Mit dem Computer starten – Windows: Eintrag unter HKCU\\...\\Run, Linux: ~/.config/autostart.
Der Windows-Installer setzt denselben Eintrag, wenn der Haken bei der Installation gesetzt ist."""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

NAME = "Taleward Worker"
SCHLUESSEL = r"Software\Microsoft\Windows\CurrentVersion\Run"


def befehl() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--hintergrund"]
    starter = os.environ.get("TALEWARD_WORKER_STARTER")  # Linux-Installation: Startskript
    if starter:
        return [starter, "--hintergrund"]
    return [sys.executable, "-m", "taleward_worker", "--hintergrund"]


def _linux_datei() -> Path:
    basis = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return basis / "autostart" / "taleward-worker.desktop"


def aktiv() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SCHLUESSEL) as k:
                winreg.QueryValueEx(k, NAME)
                return True
        except OSError:
            return False
    return _linux_datei().exists()


def setzen(an: bool) -> None:
    if sys.platform == "win32":
        import subprocess
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SCHLUESSEL, 0, winreg.KEY_SET_VALUE) as k:
            if an:
                winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(befehl()))
            else:
                try:
                    winreg.DeleteValue(k, NAME)
                except OSError:
                    pass
        return
    datei = _linux_datei()
    if not an:
        datei.unlink(missing_ok=True)
        return
    datei.parent.mkdir(parents=True, exist_ok=True)
    datei.write_text("\n".join([
        "[Desktop Entry]", "Type=Application", f"Name={NAME}",
        "Comment=Verarbeitet Aufnahmen für Taleward im Hintergrund",
        "Exec=" + " ".join(shlex.quote(t) for t in befehl()),
        "Icon=taleward-worker", "X-GNOME-Autostart-enabled=true", "Terminal=false", ""]), encoding="utf-8")
