import pytest


@pytest.fixture(autouse=True)
def eigener_ordner(tmp_path, monkeypatch):
    monkeypatch.setenv("TALEWARD_WORKER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("TALEWARD_MOTOR", raising=False)
    monkeypatch.delenv("TALEWARD_MOTOR_QUELLE", raising=False)
    return tmp_path
