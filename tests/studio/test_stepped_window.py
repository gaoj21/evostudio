"""Walking a window instead of handing it over whole.

A sample is one obligor over one window, and that window holds tens of dated
news items — median 78, up to 773. Fed as one blob they all arrive at once:
July's signal and December's are seen together, nothing accumulates, and the
pipeline's own "is this a duplicate of something we already alerted on"
reasoning has nothing to compare against.
"""

import pytest

from credit_risk.studio import feed as sources


def sample(news=(), filings=(), start="2024-01-01", end="2024-06-30"):
    return {
        "sample_id": "pos_1_2024-07-01",
        "company": {"name": "Acme", "cik": "1", "symbol": "ACME"},
        "window": {"start": start, "end": end},
        "news": [{"date": d, "title": f"news {d}", "text": "body"} for d in news],
        "filings": [{"filing_date": d, "form": "8-K", "items": [], "text": "body"}
                    for d in filings],
    }


def dates_in(record):
    return [line[1:11] for line in record["news_batch"].splitlines()
            if line.startswith("[")]


class TestMostRecentFirst:
    def test_a_long_window_is_read_from_its_end(self, ):
        # Taking the first N of a date-sorted list handed a six-month window's
        # worth of monitoring the first three days and threw the rest away —
        # on a pipeline whose job is to notice distress building toward the end.
        # More items than the cap, so which end gets kept is a real choice.
        many = [f"2024-{m:02d}-{d:02d}" for m in range(1, 7)
                for d in range(1, 29, 2)]
        assert len(many) > sources._NEWS_MAX_ITEMS
        seen = dates_in(sources._sample_to_record(sample(news=many)))

        assert len(seen) <= sources._NEWS_MAX_ITEMS
        assert max(seen) == max(many)      # the window's end is what it reads
        assert min(seen) > min(many)       # the opening months are dropped

    def test_filings_too(self):
        record = sources._sample_to_record(
            sample(filings=[f"2024-0{m}-01" for m in range(1, 7)]))
        assert "2024-06-01" in record["filing_batch"]


class TestSteppingAWindow:
    NEWS = ["2024-01-05", "2024-02-10", "2024-03-15", "2024-05-20", "2024-06-25"]

    def test_no_step_is_one_record_as_before(self):
        assert len(sources.step_sample(sample(news=self.NEWS), "none")) == 1

    def test_stepping_turns_one_sample_into_a_sequence(self):
        records = sources.step_sample(sample(news=self.NEWS), "monthly")
        assert len(records) > 1
        assert [r["as_of"] for r in records] == sorted(r["as_of"] for r in records)

    def test_each_step_sees_only_what_had_arrived(self):
        records = sources.step_sample(sample(news=self.NEWS), "monthly")
        for record in records:
            assert all(d <= record["as_of"] for d in dates_in(record))

    def test_later_steps_see_what_earlier_ones_did_and_more(self):
        records = sources.step_sample(sample(news=self.NEWS), "monthly")
        counts = [len(dates_in(r)) for r in records]
        assert counts == sorted(counts)
        assert counts[-1] > counts[0]

    def test_a_step_with_nothing_new_is_skipped(self):
        # A run seeing exactly what the last one saw has nothing to add and
        # costs a round of LLM calls. Both items arrive in the first step, so
        # the five months after it produce nothing.
        records = sources.step_sample(
            sample(news=["2024-01-05", "2024-01-06"], end="2024-06-30"), "monthly")
        assert len(records) == 1

    def test_the_steps_are_counted_back_from_the_window_s_close(self):
        # Otherwise the last step lands short and the window's own end is
        # never observed.
        records = sources.step_sample(sample(news=self.NEWS), "monthly")
        assert records[-1]["as_of"] == "2024-06-30"

    def test_no_step_falls_outside_the_window(self):
        for step in ("daily", "weekly", "monthly"):
            records = sources.step_sample(sample(news=self.NEWS), step)
            assert all("2024-01-01" <= r["as_of"] <= "2024-06-30" for r in records)

    def test_every_record_keeps_the_sample_it_came_from(self):
        records = sources.step_sample(sample(news=self.NEWS), "weekly")
        assert {r["sample_id"] for r in records} == {"pos_1_2024-07-01"}
        assert {r["company"] for r in records} == {"Acme"}

    def test_the_window_bounds_stay_the_window_bounds(self):
        # `as_of` moves; the window it belongs to does not.
        records = sources.step_sample(sample(news=self.NEWS), "monthly")
        assert {r["window_start"] for r in records} == {"2024-01-01"}
        assert {r["window_end"] for r in records} == {"2024-06-30"}

    def test_a_sample_with_no_news_produces_nothing(self):
        assert sources.step_sample(sample(), "weekly") == []

    def test_a_window_with_no_dates_falls_back_to_one_record(self):
        broken = sample(news=self.NEWS, start="", end="")
        assert len(sources.step_sample(broken, "weekly")) == 1

    def test_finer_steps_give_more_records(self):
        monthly = sources.step_sample(sample(news=self.NEWS), "monthly")
        weekly = sources.step_sample(sample(news=self.NEWS), "weekly")
        assert len(weekly) >= len(monthly)

    def test_an_unstepped_record_is_still_dated(self):
        # `as_of` exists either way, so a node can be dated by it regardless.
        [record] = sources.step_sample(sample(news=self.NEWS), "none")
        assert record["as_of"] == "2024-06-30"


class TestOrderingInABatch:
    """Grouping is generic: the Input type names the trajectory field.

    These use a neutral test type ("account"/"day"); the credit-risk feed
    declares its own ("sample_id"/"as_of") the same way."""

    @staticmethod
    def pairs(records, monkeypatch, sequence=None):
        import sequenced_input
        from backend.api import batch
        sequenced_input.install(monkeypatch, sequence=sequence or sequenced_input.SEQUENCE)
        pairs = [({"index": i}, r) for i, r in enumerate(records)]
        batch.assign_sequences(sequenced_input.graph(), pairs)
        return pairs

    def test_steps_of_one_sample_stay_together(self, monkeypatch):
        from backend.api import batch
        records = [{"account": "a", "day": "2024-01-01"},
                   {"account": "a", "day": "2024-02-01"},
                   {"account": "b", "day": "2024-01-01"}]
        groups = batch._grouped(self.pairs(records, monkeypatch))

        # Run at the same time, a later step's judgement can land before an
        # earlier one's, and each run's memory is what the next one reads.
        assert sorted(len(g) for g in groups) == [1, 2]

    def test_they_stay_in_order_within_the_group(self, monkeypatch):
        from backend.api import batch
        records = [{"account": "a", "day": d}
                   for d in ("2024-01-01", "2024-02-01", "2024-03-01")]
        [group] = batch._grouped(self.pairs(records, monkeypatch))
        assert [r["day"] for _item, r in group] == \
            ["2024-01-01", "2024-02-01", "2024-03-01"]

    def test_different_samples_still_run_in_parallel(self, monkeypatch):
        from backend.api import batch
        records = [{"account": s, "day": "2024-01-01"} for s in "abc"]
        assert len(batch._grouped(self.pairs(records, monkeypatch))) == 3

    def test_no_declared_sequence_means_no_grouping(self):
        from backend.api import batch

        # The old implicit sample_id + as_of rule is gone: without an Input
        # that declares a sequence, same-named fields group nothing.
        records = [{"sample_id": "a", "as_of": "1"}, {"sample_id": "a", "as_of": "2"}]
        pairs = [({"index": i}, r) for i, r in enumerate(records)]
        batch.assign_sequences({"id": "g", "tasks": [], "edges": []}, pairs)
        assert len(batch._grouped(pairs)) == 2

    def test_records_without_the_group_field_are_left_alone(self, monkeypatch):
        from backend.api import batch
        records = [{"city": "Lima"}, {"city": "Oslo"}]
        assert len(batch._grouped(self.pairs(records, monkeypatch))) == 2

    def test_nothing_is_dropped(self, monkeypatch):
        from backend.api import batch
        records = [{"account": "a", "day": "1"}, {"account": "a", "day": "2"},
                   {"account": "b", "day": "1"}, {"city": "Lima"}]
        groups = batch._grouped(self.pairs(records, monkeypatch))
        assert sum(len(g) for g in groups) == len(records)


class TestThroughTheSource:
    def test_the_source_node_passes_the_step_through(self, monkeypatch):
        from backend.api import sources as platform_sources
        captured = {}
        monkeypatch.setattr(sources, "credit_risk_records",
                            lambda **kw: captured.update(kw) or [])
        platform_sources.records_from_source_node(
            {"source": {"type": "credit_risk", "n": 1, "step": "weekly"}})
        assert captured["step"] == "weekly"

    def test_it_is_offered_as_a_setting(self):
        from backend.features import plugins

        schema = plugins.source_schemas()["credit_risk"]
        names = {c["name"] for c in schema["config"]}
        assert "step" in names
        assert "as_of" in schema["outputs"]
        # The plugin, not the platform, says what a trajectory is.
        assert schema["sequence"] == {"group": "sample_id", "order": "as_of"}
