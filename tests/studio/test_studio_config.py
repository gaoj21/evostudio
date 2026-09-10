from pathlib import Path


def test_data_directory_can_live_outside_the_checkout(monkeypatch, tmp_path):
    from backend.api import studio_config
    wanted = tmp_path / "state"
    monkeypatch.setenv("EAX_STUDIO_DATA_DIR", str(wanted))

    assert studio_config._configured_data_dir() == wanted.resolve()
    assert studio_config.data_path("runs").parent == studio_config.DATA_DIR


def test_default_data_directory_uses_backend_layout(monkeypatch):
    from backend.api import studio_config
    monkeypatch.delenv("EAX_STUDIO_DATA_DIR", raising=False)
    assert studio_config._configured_data_dir() == \
        Path(studio_config.REPO_ROOT, "backend", "data")
