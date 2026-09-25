"""The embedding model is read from the project folder when a copy is there,
so a machine without Hugging Face access never fetches it at run time."""
from pathlib import Path

from backend.memory import ltm


def fake_model(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    (folder / "model.safetensors").write_bytes(b"x")
    return folder


def test_a_copy_in_the_project_folder_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv("EAX_EMBEDDING_MODEL", raising=False)
    local = fake_model(tmp_path / "models" / "bge-small-en-v1.5")
    monkeypatch.setattr(ltm, "LOCAL_MODEL_DIRS", (local,))

    assert ltm.embedding_model() == str(local)


def test_the_environment_names_another_folder(tmp_path, monkeypatch):
    elsewhere = fake_model(tmp_path / "elsewhere")
    monkeypatch.setattr(ltm, "LOCAL_MODEL_DIRS", (tmp_path / "missing",))
    monkeypatch.setenv("EAX_EMBEDDING_MODEL", str(elsewhere))

    assert ltm.embedding_model() == str(elsewhere)


def test_without_a_complete_copy_it_is_the_hugging_face_id(tmp_path, monkeypatch):
    monkeypatch.delenv("EAX_EMBEDDING_MODEL", raising=False)
    half = tmp_path / "half"
    half.mkdir()
    (half / "config.json").write_text("{}")          # no weights: not a model
    monkeypatch.setattr(ltm, "LOCAL_MODEL_DIRS", (half,))

    assert ltm.embedding_model() == "BAAI/bge-small-en-v1.5"
