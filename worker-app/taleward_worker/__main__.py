"""Start: taleward-worker [--hintergrund] [--pruefen]"""
from __future__ import annotations

import sys


def main() -> int:
    argumente = sys.argv[1:]
    if "--pruefen" in argumente:
        # Für den Paketbau: alles laden, nichts anzeigen
        from taleward_worker import autostart, dienst, einzeln, hardware, motor, oberflaeche, pfade, server  # noqa: F401
        import webview  # noqa: F401

        ui = pfade.ressourcen() / "ui" / "index.html"
        print("Taleward Worker ok:", ui.exists(), hardware.bericht()["system"])
        return 0 if ui.exists() else 1
    from taleward_worker.oberflaeche import WorkerApp

    WorkerApp(hintergrund="--hintergrund" in argumente).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
