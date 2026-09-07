"""Getting back to a batch that has already run.

Batches were persisted from the start but had no way to reach them: the id
lived only in the tab that started them, so a finished evaluation was gone the
moment the page reloaded. These cover the listing that closes that gap.
"""

import json

import pytest


@pytest.fixture
def batches(tmp_path, monkeypatch):
    from studio.backend import batch as batch_module
    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    batch_module._batches.clear()
    batch_module._threads.clear()
    return batch_module


def write(batches, batch_id, *, graph_id="g1", status="completed",
          created_at="2026-09-01T00:00:00+00:00", items=None, **extra):
    """A batch as it would be left on disk by a process that has since exited."""
    state = {
        "batch_id": batch_id, "graph_id": graph_id, "status": status,
        "created_at": created_at, "source": {"type": "upload", "filename": "x.jsonl"},
        "metric": None, "summary": None,
        "items": items if items is not None else [{"index": 0, "status": "success"}],
        "total": len(items) if items is not None else 1,
        **extra,
    }
    batches._persist_batch(state)
    return state


class TestListBatches:
    def test_a_batch_outlives_the_process_that_ran_it(self, batches):
        write(batches, "b1")
        batches._batches.clear()   # as if the server had restarted

        listed = batches.list_batches()
        assert [b["batch_id"] for b in listed] == ["b1"]
        assert listed[0]["status"] == "completed"

    def test_newest_first(self, batches):
        write(batches, "old", created_at="2026-09-01T00:00:00+00:00")
        write(batches, "new", created_at="2026-09-05T00:00:00+00:00")
        write(batches, "mid", created_at="2026-09-03T00:00:00+00:00")

        assert [b["batch_id"] for b in batches.list_batches()] == ["new", "mid", "old"]

    def test_only_this_workflow_s_batches(self, batches):
        write(batches, "mine", graph_id="g1")
        write(batches, "theirs", graph_id="g2")

        assert [b["batch_id"] for b in batches.list_batches(graph_id="g1")] == ["mine"]

    def test_the_listing_leaves_the_records_behind(self, batches):
        # A batch document is mostly its records — hundreds of them, each with
        # its output. Sending those to render a history list would be absurd.
        write(batches, "b1", items=[{"index": i, "status": "success",
                                     "output_summary": "x" * 500}
                                    for i in range(200)])
        entry = batches.list_batches()[0]

        assert "items" not in entry
        assert entry["total"] == 200
        assert entry["counts"] == {"success": 200}

    def test_it_counts_how_each_record_ended(self, batches):
        write(batches, "b1", items=[
            {"index": 0, "status": "success"}, {"index": 1, "status": "success"},
            {"index": 2, "status": "failed"}, {"index": 3, "status": "cancelled"},
        ])
        assert batches.list_batches()[0]["counts"] == {
            "success": 2, "failed": 1, "cancelled": 1,
        }

    def test_an_evaluation_carries_its_score_into_the_listing(self, batches):
        # The score is the only reason to look up a past evaluation, so it has
        # to survive the trip through the digest.
        write(batches, "b1", metric="exact_match",
              summary={"scored": 8, "total": 10, "mean": 0.75,
                       "min": 0.0, "max": 1.0, "perfect": 6, "unscored": 2})
        entry = batches.list_batches()[0]

        assert entry["metric"] == "exact_match"
        assert entry["summary"]["mean"] == 0.75
        assert entry["summary"]["scored"] == 8

    def test_a_running_batch_is_listed_from_memory(self, batches, monkeypatch):
        batches._batches["live"] = {
            "batch_id": "live", "graph_id": "g1", "status": "running",
            "created_at": "2026-09-06T00:00:00+00:00",
            "items": [{"index": 0, "status": "running"},
                      {"index": 1, "status": "pending"}],
            "total": 2, "source": {}, "metric": None, "summary": None,
        }
        entry = batches.list_batches()[0]

        # This is what makes a reload able to reattach to work still going.
        assert entry["status"] == "running"
        assert entry["counts"] == {"running": 1, "pending": 1}

    def test_memory_wins_over_disk_for_the_same_batch(self, batches):
        # Persisted at the start, so the file says "running" long after the
        # batch in memory has moved on.
        write(batches, "b1", status="running")
        batches._batches["b1"] = {
            "batch_id": "b1", "graph_id": "g1", "status": "completed",
            "created_at": "2026-09-01T00:00:00+00:00", "items": [], "total": 0,
            "source": {}, "metric": None, "summary": None,
        }
        listed = batches.list_batches()

        assert len(listed) == 1
        assert listed[0]["status"] == "completed"

    def test_a_corrupt_file_does_not_take_the_listing_down(self, batches):
        write(batches, "good")
        (batches.BATCHES_DIR / "broken.json").write_text("{not json")

        assert [b["batch_id"] for b in batches.list_batches()] == ["good"]

    def test_no_batches_yet_is_an_empty_list_not_an_error(self, batches):
        assert batches.list_batches() == []
        assert batches.list_batches(graph_id="never-ran") == []

    def test_the_listing_is_capped(self, batches):
        for i in range(60):
            write(batches, f"b{i:02d}", created_at=f"2026-09-01T00:{i:02d}:00+00:00")
        assert len(batches.list_batches(limit=50)) == 50


class TestHistoryEndpoint:
    @pytest.fixture
    def client(self, batches):
        from fastapi.testclient import TestClient

        from studio.backend import app as studio_app
        return TestClient(studio_app.app)

    def test_it_lists_the_workflow_s_batches(self, client, batches):
        write(batches, "mine", graph_id="g1", metric="f1",
              summary={"mean": 0.9, "scored": 3, "total": 3})
        write(batches, "theirs", graph_id="g2")

        res = client.get("/api/batches", params={"graph_id": "g1"})
        assert res.status_code == 200
        body = res.json()
        assert [b["batch_id"] for b in body] == ["mine"]
        assert body[0]["summary"]["mean"] == 0.9

    def test_without_a_workflow_it_lists_them_all(self, client, batches):
        write(batches, "a", graph_id="g1")
        write(batches, "b", graph_id="g2")
        assert len(client.get("/api/batches").json()) == 2

    def test_the_full_batch_is_still_a_separate_fetch(self, client, batches):
        write(batches, "b1", items=[{"index": 0, "status": "success",
                                     "output_summary": "the answer"}])
        listing = client.get("/api/batches").json()[0]
        assert "items" not in listing

        full = client.get("/api/batches/b1").json()
        assert full["items"][0]["output_summary"] == "the answer"
