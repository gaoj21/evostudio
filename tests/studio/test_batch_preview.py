"""Saying how big a batch is before it runs.

Stepping turns one sample into one record per date, so `n=3, step=monthly` is
eighteen runs, not three. The dialog offered no number at all, which made the
expensive choice in this app the invisible one. These cover the endpoint that
answers the question, and the two things it must not get wrong: counting dates
across samples instead of per sample, and actually starting the batch.
"""

import pytest

from conftest import make_graph, make_task


def stepped_records(samples=3, dates=6):
    """What a stepped source hands back: one record per sample per date.

    Windows differ per obligor, so no two samples share a date — which is
    exactly what makes counting dates batch-wide wrong.
    """
    out = []
    for s in range(samples):
        for d in range(dates):
            out.append({
                "sample_id": f"sample_{s}",
                "as_of": f"20{24 + s}-{d + 1:02d}-28",
                "company": f"Co {s}",
                "news_batch": f"news for {s} at step {d}",
            })
    return out


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as studio_app
    import batch as batch_module
    import graphs as graph_store
    import sources

    monkeypatch.setattr(graph_store, "GRAPHS_DIR", tmp_path / "graphs")
    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    batch_module._batches.clear()
    batch_module._threads.clear()

    feed = make_task("feed", outputs=["company", "news_batch"])
    feed["kind"] = "source"
    feed["source"] = {"type": "credit_risk", "split": "test", "n": 3,
                      "seed": 42, "step": "monthly"}
    graph = make_graph(
        [feed, make_task("judge", inputs=["company", "news_batch"], outputs=["verdict"])],
        edges=[("feed", "judge")],
        id="g-preview",
    )
    graph_store.save_graph("g-preview", graph)

    monkeypatch.setattr(sources, "records_from_source_node",
                        lambda node: stepped_records())
    monkeypatch.setattr(
        sources, "credit_risk_records",
        lambda **kw: stepped_records(dates=6 if kw.get("step") else 1))
    return TestClient(studio_app.app)


def preview(client, **body):
    res = client.post("/api/graphs/g-preview/run-batch/preview",
                      json={"source": "canvas", **body})
    assert res.status_code == 200, res.text
    return res.json()


class TestTheCount:
    def test_it_reports_the_runs_not_the_samples(self, client):
        # The whole point: three samples stepped monthly is eighteen runs, and
        # nothing in the dialog used to say so.
        assert preview(client)["total"] == 18

    def test_dates_are_counted_per_sample(self, client):
        # Counting distinct dates across the batch gives 18 here, because no
        # two obligors share a window — which would read as "3 samples over 18
        # dates each", triple the truth.
        body = preview(client)
        assert body["samples"] == 3
        assert body["steps"] == 6
        assert body["steps_min"] == 6

    def test_an_unstepped_source_reports_one_date_each(self, client):
        body = preview(client, source="credit_risk", n=3, seed=42)
        assert body["total"] == 3
        assert body["steps"] == 1

    def test_uneven_windows_report_a_range(self, client, monkeypatch):
        import sources
        short = [r for r in stepped_records()
                 if r["sample_id"] != "sample_0" or r["as_of"] < "2024-04-01"]
        monkeypatch.setattr(sources, "records_from_source_node", lambda node: short)

        body = preview(client)
        assert (body["steps_min"], body["steps"]) == (3, 6)

    def test_it_says_what_span_is_covered(self, client):
        assert preview(client)["dates"] == ["2024-01-28", "2026-06-28"]

    def test_it_names_the_fields_each_run_gets(self, client):
        # A wrong mapping shows up here as a missing field, before 18 runs of
        # the workflow discover it one at a time.
        assert set(preview(client)["fields"]) >= {"company", "news_batch"}


class TestItOnlyLooks:
    def test_previewing_starts_nothing(self, client):
        import batch as batch_module

        preview(client)
        assert batch_module._batches == {}
        assert batch_module.list_batches() == []

    def test_previewing_writes_no_batch_file(self, client, tmp_path):
        preview(client)
        assert not list((tmp_path / "batches").glob("*")) if (tmp_path / "batches").exists() else True

    def test_a_bad_source_is_refused_rather_than_counted(self, client):
        res = client.post("/api/graphs/g-preview/run-batch/preview",
                          json={"source": "nonsense"})
        assert res.status_code == 422

    def test_an_unknown_graph_is_a_404(self, client):
        res = client.post("/api/graphs/no-such-graph/run-batch/preview",
                          json={"source": "canvas"})
        assert res.status_code == 404


class TestItAgreesWithTheRealThing:
    def test_the_preview_total_is_the_batch_total(self, client, monkeypatch):
        # The number shown and the number run come from one code path; if they
        # ever diverge the preview is worse than none.
        #
        # start_batch is stubbed rather than let run: a real one spawns worker
        # threads that outlive the fixture's monkeypatch, and they then persist
        # into the developer's own studio/data. That has happened.
        import app as studio_app

        handed = {}

        def capture(graph, mapped, source, **kw):
            handed["records"] = mapped
            return "fake-batch"

        monkeypatch.setattr(studio_app.batch_store, "start_batch", capture)

        counted = preview(client)["total"]
        res = client.post("/api/graphs/g-preview/run-batch",
                          json={"source": "canvas", "workers": 1})

        assert res.status_code == 200
        assert res.json()["total"] == counted
        assert len(handed["records"]) == counted
