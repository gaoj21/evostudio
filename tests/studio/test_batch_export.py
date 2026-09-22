"""Getting a scored batch out of the Studio as a file.

The reason to evaluate 200 records is to hand someone the result, so these
cover the shapes that break a spreadsheet: records that do not agree on their
fields, text containing commas and newlines, and a metric that returns more
than a single number.
"""

import csv
import io
import json

import pytest

from backend.api import batch_export
def read_csv(text):
    csv.field_size_limit(10 ** 7)
    return list(csv.DictReader(io.StringIO(text)))


def batch(items, **extra):
    return {
        "batch_id": "b1", "graph_id": "g1", "status": "completed",
        "created_at": "2026-09-01T00:00:00+00:00",
        "source": {"type": "upload", "filename": "x.jsonl"},
        "metric": None, "summary": None, "total": len(items),
        "items": items, **extra,
    }


class TestCsv:
    def test_one_row_per_record(self):
        rows = read_csv(batch_export.to_csv(batch([
            {"index": 0, "status": "success", "inputs": {"city": "Lima"},
             "output_summary": '{"answer": "Lima"}', "run_id": "r0"},
            {"index": 1, "status": "failed", "inputs": {"city": "Oslo"},
             "error": "timed out", "run_id": "r1"},
        ])))
        assert [r["index"] for r in rows] == ["0", "1"]
        assert rows[0]["input_city"] == "Lima"
        assert rows[0]["output"] == '{"answer": "Lima"}'
        assert rows[1]["error"] == "timed out"

    def test_the_score_and_its_label_sit_beside_each_record(self):
        rows = read_csv(batch_export.to_csv(batch([
            {"index": 0, "status": "success", "inputs": {"q": "capital?"},
             "label": "Lima", "score": 1.0, "output_summary": "Lima"},
        ], metric="exact_match")))
        assert rows[0]["score"] == "1.0"
        assert rows[0]["label"] == "Lima"

    def test_records_that_disagree_about_their_fields(self):
        # A preprocessor can add a field, a source can omit one. Taking the
        # first record's keys would silently drop every later column.
        rows = read_csv(batch_export.to_csv(batch([
            {"index": 0, "inputs": {"a": 1}, "status": "success"},
            {"index": 1, "inputs": {"a": 2, "b": 3}, "status": "success"},
        ])))
        assert rows[0]["input_b"] == ""
        assert rows[1]["input_b"] == "3"

    def test_text_with_commas_and_newlines_survives_the_round_trip(self):
        messy = 'Reported "record" losses, again\nand a second line'
        rows = read_csv(batch_export.to_csv(batch([
            {"index": 0, "status": "success", "inputs": {"news": messy},
             "output_summary": messy},
        ])))
        assert rows[0]["input_news"] == messy
        assert rows[0]["output"] == messy

    def test_structured_values_are_json_not_python_reprs(self):
        # str({'a': 1}) gives "{'a': 1}", which no JSON reader will take back.
        rows = read_csv(batch_export.to_csv(batch([
            {"index": 0, "status": "success",
             "inputs": {"tags": ["x", "y"], "meta": {"a": 1}}},
        ])))
        assert json.loads(rows[0]["input_tags"]) == ["x", "y"]
        assert json.loads(rows[0]["input_meta"]) == {"a": 1}

    def test_a_metric_that_returns_more_than_a_number(self):
        rows = read_csv(batch_export.to_csv(batch([
            {"index": 0, "status": "success", "inputs": {}, "score": 0.5,
             "score_detail": {"precision": 0.4, "recall": 0.7}},
        ], metric="f1")))
        assert rows[0]["score_precision"] == "0.4"
        assert rows[0]["score_recall"] == "0.7"

    def test_a_record_the_metric_could_not_score(self):
        rows = read_csv(batch_export.to_csv(batch([
            {"index": 0, "status": "success", "inputs": {}, "score": None,
             "score_detail": {"error": "no label"}},
        ], metric="exact_match")))
        # Blank, not "None": the cell means "not scored", and a reader should
        # not have to know Python to see that.
        assert rows[0]["score"] == ""
        assert rows[0]["score_error"] == "no label"

    def test_records_a_cancellation_never_reached_are_still_listed(self):
        rows = read_csv(batch_export.to_csv(batch([
            {"index": 0, "status": "success", "inputs": {"city": "Lima"}},
            {"index": 1, "status": "cancelled", "inputs": {"city": "Oslo"}},
        ], status="cancelled")))
        # The export says what the batch covered, which includes what it did not.
        assert [r["status"] for r in rows] == ["success", "cancelled"]

    def test_an_empty_batch_still_produces_a_header(self):
        text = batch_export.to_csv(batch([]))
        assert text.splitlines()[0].startswith("index,status,score,label")
        assert read_csv(text) == []


class TestJson:
    def test_it_carries_the_context_needed_to_read_the_numbers(self):
        doc = json.loads(batch_export.to_json(batch([
            {"index": 0, "status": "success", "inputs": {"q": "a"}, "score": 1.0},
        ], metric="exact_match",
            summary={"mean": 1.0, "scored": 1, "total": 1})))
        # A bare list of scores is not interpretable once it leaves the tool.
        assert doc["metric"] == "exact_match"
        assert doc["summary"]["mean"] == 1.0
        assert doc["source"]["filename"] == "x.jsonl"
        assert len(doc["results"]) == 1

    def test_the_records_match_the_csv(self):
        state = batch([
            {"index": 0, "status": "success", "inputs": {"a": 1, "b": 2},
             "score": 0.5, "output_summary": "out"},
        ])
        rows = read_csv(batch_export.to_csv(state))
        results = json.loads(batch_export.to_json(state))["results"]
        assert set(results[0]) == set(rows[0])
        assert results[0]["input_a"] == 1     # typed here, text in the CSV
        assert rows[0]["input_a"] == "1"


class TestFilename:
    def test_it_names_the_workflow_and_the_batch(self):
        assert batch_export.filename(batch([]), "csv") == "g1-batch-b1.csv"

    def test_a_batch_with_no_workflow_still_gets_a_name(self):
        state = batch([]); state["graph_id"] = None
        assert batch_export.filename(state, "json") == "workflow-batch-b1.json"


class TestEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from backend.api import app as studio_app
        from backend.api import batch as batch_module
        monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
        batch_module._batches.clear()
        batch_module._threads.clear()
        batch_module._persist_batch(batch([
            {"index": 0, "status": "success", "inputs": {"city": "Lima"},
             "score": 1.0, "output_summary": "Lima"},
        ], metric="exact_match", summary={"mean": 1.0, "scored": 1, "total": 1}))
        return TestClient(studio_app.app)

    def test_csv_arrives_as_a_download(self, client):
        res = client.get("/api/batches/b1/export", params={"format": "csv"})
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/csv")
        # Without this the browser renders it in a tab instead of saving it.
        assert 'attachment; filename="g1-batch-b1.csv"' in res.headers["content-disposition"]
        assert read_csv(res.text)[0]["input_city"] == "Lima"

    def test_json_arrives_as_a_download(self, client):
        res = client.get("/api/batches/b1/export", params={"format": "json"})
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/json")
        assert 'filename="g1-batch-b1.json"' in res.headers["content-disposition"]
        assert res.json()["metric"] == "exact_match"

    def test_csv_is_the_default(self, client):
        res = client.get("/api/batches/b1/export")
        assert res.headers["content-type"].startswith("text/csv")

    def test_an_unknown_batch_is_a_404(self, client):
        assert client.get("/api/batches/nope/export").status_code == 404

    def test_an_unsupported_format_says_what_is_supported(self, client):
        res = client.get("/api/batches/b1/export", params={"format": "xlsx"})
        assert res.status_code == 422
        assert "csv or json" in res.json()["detail"]
