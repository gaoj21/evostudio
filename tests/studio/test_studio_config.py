from pathlib import Path


def test_data_directory_can_live_outside_the_checkout(monkeypatch, tmp_path):
    from backend.api import studio_config
    wanted = tmp_path / "state"
    monkeypatch.setenv("EAX_STUDIO_DATA_DIR", str(wanted))

    assert studio_config._configured_data_dir() == wanted.resolve()
    assert studio_config.data_path("runs").parent == studio_config.DATA_DIR


def test_by_default_data_lives_beside_the_code_not_inside_the_backend(monkeypatch):
    """A user's workflows and run output are their data, not part of the
    backend package."""
    from backend.api import studio_config
    monkeypatch.delenv("EAX_STUDIO_DATA_DIR", raising=False)
    assert studio_config._configured_data_dir() == Path(studio_config.REPO_ROOT, "studio-data")
    assert "backend" not in studio_config._configured_data_dir().parts[-2:]


def test_data_left_in_the_old_location_is_moved_once(monkeypatch, tmp_path):
    from backend.api import studio_config
    legacy = tmp_path / "backend" / "data"
    (legacy / "graphs").mkdir(parents=True)
    (legacy / "graphs" / "g.json").write_text("{}")
    monkeypatch.setattr(studio_config, "LEGACY_DATA_DIR", legacy)
    target = tmp_path / "studio-data"

    studio_config._migrate_legacy_data(target)
    assert (target / "graphs" / "g.json").read_text() == "{}"
    assert not legacy.exists()
    assert (tmp_path / "backend" / "DATA_MOVED.md").is_file()


def test_a_new_root_that_already_exists_is_left_alone(monkeypatch, tmp_path):
    from backend.api import studio_config
    legacy = tmp_path / "backend" / "data"
    (legacy / "graphs").mkdir(parents=True)
    monkeypatch.setattr(studio_config, "LEGACY_DATA_DIR", legacy)
    target = tmp_path / "studio-data"
    target.mkdir()

    studio_config._migrate_legacy_data(target)
    assert legacy.is_dir() and not any(target.iterdir())
