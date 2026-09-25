"""Snapshot-then-clear, as one call.

The user's rule before every measured run: back memory up, then empty it.
Done by hand it is a thing to forget; here it is a keystroke, and it never
deletes anything without a copy first.
"""

from pathlib import Path

import pytest


@pytest.fixture
def stores(tmp_path, monkeypatch):
    from backend.api import memory_reset, studio_config, table_store
    monkeypatch.setattr(studio_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(memory_reset, "BACKUPS", tmp_path / "memory-backups")
    monkeypatch.setattr(table_store, "TABLES_DIR", tmp_path / "tables")
    table_store.upsert("g1", "decide", "Acme", "2026-01-01", {"outputs": {"d": 1}}, "t")
    table_store.upsert("g2", "decide", "Beta", "2026-01-01", {"outputs": {"d": 2}}, "t")
    (tmp_path / "memory" / "g1").mkdir(parents=True)
    (tmp_path / "memory" / "g1" / "memory.db").write_bytes(b"x" * 10)
    (tmp_path / "stm").mkdir()
    (tmp_path / "stm" / "g1.json").write_text("[]")
    return tmp_path


class TestResetKeepsACopy:
    def test_everything_is_copied_before_it_goes(self, stores):
        from backend.api import memory_reset, table_store
        out = memory_reset.snapshot_and_clear()

        backup = stores / "memory-backups" / out["stamp"]
        assert (backup / "tables" / "g1" / "decide.db").is_file()
        assert (backup / "memory" / "g1" / "memory.db").is_file()
        assert (backup / "stm" / "g1.json").is_file()
        assert table_store.count("g1", "decide") == 0
        assert not (stores / "memory" / "g1").exists()
        assert out["sizes"]["memory"] == 10

    def test_one_workflow_can_be_reset_without_touching_another(self, stores):
        from backend.api import memory_reset, table_store
        out = memory_reset.snapshot_and_clear("g1")

        assert table_store.count("g1", "decide") == 0
        assert table_store.count("g2", "decide") == 1            # untouched
        assert (stores / "memory-backups" / out["stamp"] / "tables" / "g1" / "decide.db").is_file()
        assert not (stores / "stm" / "g1.json").exists()

    def test_it_refuses_while_something_runs(self, stores):
        from backend.api import memory_reset, table_store
        with pytest.raises(RuntimeError):
            memory_reset.snapshot_and_clear(busy=True)
        assert table_store.count("g1", "decide") == 1            # nothing happened

    def test_backups_are_listed_newest_first(self, stores):
        from backend.api import memory_reset
        a = memory_reset.snapshot_and_clear()["stamp"]
        listed = memory_reset.backups()
        assert listed[0]["stamp"] == a


class TestTheEndpoint:
    @pytest.fixture
    def client(self, stores, monkeypatch):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app
        monkeypatch.setattr(studio_app.batch_store, "list_batches", lambda: [])
        monkeypatch.setattr(studio_app.runner, "list_runs", lambda graph_id=None: [])
        return TestClient(studio_app.app)

    def test_reset_for_the_open_workflow(self, client):
        from backend.api import table_store
        res = client.post("/api/memory/reset", json={"graph_id": "g1"})
        assert res.status_code == 200, res.text
        assert res.json()["graph_id"] == "g1"
        assert table_store.count("g1", "decide") == 0
        assert table_store.count("g2", "decide") == 1

    def test_a_live_batch_makes_it_a_409(self, client, monkeypatch):
        from backend.api import app as studio_app, table_store
        monkeypatch.setattr(studio_app.batch_store, "list_batches",
                            lambda: [{"batch_id": "b", "status": "running"}])
        res = client.post("/api/memory/reset", json={})
        assert res.status_code == 409
        assert table_store.count("g1", "decide") == 1


def test_a_reset_leaves_the_table_store_able_to_write_again(stores):
    """The bug that emptied a measured batch: reset removed the files, the
    store's memo said they were open, every write after that failed."""
    from backend.api import memory_reset, table_store
    memory_reset.snapshot_and_clear("g1")
    table_store.upsert("g1", "decide", "Acme", "2026-02-01", {"outputs": {"d": 3}}, "t")
    rows = table_store.rows("g1", "decide", "Acme")
    assert [r["payload"]["outputs"]["d"] for r in rows] == [3]



class TestBackupAndClearAreSeparate:
    """Backing up and emptying are two operations: one takes nothing away and
    is allowed at any time, the other is the step before a measured run."""

    def test_a_backup_leaves_memory_in_place(self, stores):
        from backend.api import memory_reset

        out = memory_reset.snapshot()

        assert out["saved"] and out["cleared"] is False
        assert (stores / "tables" / "g1").exists()
        assert (stores / "memory" / "g1" / "memory.db").exists()
        assert (stores / "stm" / "g1.json").exists()
        assert (Path(out["backup"]) / "tables" / "g1").exists()

    def test_a_backup_of_nothing_names_no_folder(self, tmp_path, monkeypatch):
        from backend.api import memory_reset, studio_config

        monkeypatch.setattr(studio_config, "DATA_DIR", tmp_path / "empty")
        monkeypatch.setattr(memory_reset, "BACKUPS", tmp_path / "empty" / "memory-backups")

        out = memory_reset.snapshot()

        assert out["saved"] is False and out["backup"] is None

    def test_clearing_backs_up_by_default(self, stores):
        from backend.api import memory_reset

        out = memory_reset.clear()

        assert out["cleared"] and out["backup"]
        assert not (stores / "memory" / "g1").exists()
        assert (Path(out["backup"]) / "memory" / "g1" / "memory.db").exists()

    def test_clearing_without_a_copy_is_possible_but_explicit(self, stores):
        from backend.api import memory_reset

        out = memory_reset.clear(backup=False)

        assert out["cleared"] and out["backup"] is None
        assert memory_reset.backups() == []
        assert not (stores / "memory" / "g1").exists()

    def test_two_backups_in_the_same_second_are_two_backups(self, stores):
        from backend.api import memory_reset

        first, second = memory_reset.snapshot(), memory_reset.snapshot()

        assert first["stamp"] != second["stamp"]
        assert len(memory_reset.backups()) == 2

    def test_a_backup_is_allowed_while_something_runs(self, stores):
        """Refusing a backup because a run is in flight would deny the user
        the one operation that cannot lose anything."""
        from backend.api import memory_reset

        assert memory_reset.snapshot()["saved"] is True
        with pytest.raises(RuntimeError):
            memory_reset.clear(busy=True)
