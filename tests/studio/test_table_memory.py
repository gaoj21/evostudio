"""Table memory: Coze's 数据库 rather than its 知识库.

A node that tracks an obligor is keeping a record, not doing fuzzy lookup.
Built on a vector corpus that record failed silently in about a dozen ways —
a store that never attached, never wrote, or matched nothing still produced a
workflow that ran and answered plausibly. A row is either there or it is not,
and these check that it is.
"""

import json
import threading

import pytest

from studio.backend import memory_policy
from studio.backend import table_store
@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(table_store, "TABLES_DIR", tmp_path / "tables")
    return table_store


def put(store, subject, at, verdict, recorded_at="2026-09-07 01:00:00"):
    store.upsert("g1", "decide", subject, at,
                 {"inputs": {"company": subject, "as_of": at},
                  "outputs": {"decision": verdict}}, recorded_at)


def verdicts(rows):
    return [r["payload"]["outputs"]["decision"] for r in rows]


class TestARowIsEitherThereOrNot:
    def test_what_goes_in_comes_back(self, store):
        put(store, "Sleep Number", "2026-01-12", "suppress")
        assert verdicts(store.rows("g1", "decide", "Sleep Number")) == ["suppress"]

    def test_you_can_count_them(self, store):
        # The cheap answer to "did it write?", which the vector store never had.
        assert store.count("g1", "decide") == 0
        put(store, "Sleep Number", "2026-01-12", "suppress")
        put(store, "Wayfair", "2026-06-02", "alert")
        assert store.count("g1", "decide") == 2

    def test_a_node_that_never_wrote_reads_empty_rather_than_erroring(self, store):
        assert store.rows("g1", "nobody") == []
        assert store.subjects("g1", "nobody") == []
        assert store.count("g1", "nobody") == 0

    def test_each_node_keeps_its_own_table(self, store):
        put(store, "Sleep Number", "2026-01-12", "suppress")
        store.upsert("g1", "investigate", "Sleep Number", "2026-01-12",
                     {"outputs": {"context": "a pack"}}, "t")
        assert store.count("g1", "decide") == 1
        assert store.count("g1", "investigate") == 1
        assert sorted(store.nodes("g1")) == ["decide", "investigate"]

    def test_a_node_name_that_is_not_a_filename_still_works(self, store):
        store.upsert("g1", "decide/../etc", "X", "", {"outputs": {}}, "t")
        assert store.count("g1", "decide/../etc") == 1
        assert "decide/../etc" not in [str(p) for p in store.nodes("g1")]


class TestOneRowPerSubjectPerDate:
    def test_a_company_accumulates_states(self, store):
        for month in range(1, 7):
            put(store, "Sleep Number", f"2026-{month:02d}-01", f"v{month}")

        rows = store.rows("g1", "decide", "Sleep Number")
        assert [r["at"] for r in rows] == [f"2026-{m:02d}-01" for m in range(1, 7)]

    def test_re_running_a_date_corrects_it_rather_than_duplicating(self, store):
        put(store, "Sleep Number", "2026-01-12", "suppress")
        put(store, "Sleep Number", "2026-01-12", "alert")

        rows = store.rows("g1", "decide", "Sleep Number")
        assert verdicts(rows) == ["alert"]

    def test_two_companies_do_not_mix(self, store):
        put(store, "Sleep Number", "2026-01-12", "suppress")
        put(store, "Wayfair", "2026-01-12", "alert")

        assert verdicts(store.rows("g1", "decide", "Sleep Number")) == ["suppress"]
        assert store.subjects("g1", "decide") == ["Sleep Number", "Wayfair"]

    def test_write_order_does_not_decide_read_order(self, store):
        # A batch runs its records in parallel, so the later date is often
        # written first.
        put(store, "Sleep Number", "2026-06-11", "later")
        put(store, "Sleep Number", "2026-01-12", "earlier")

        assert verdicts(store.rows("g1", "decide", "Sleep Number")) == \
            ["earlier", "later"]


class TestPointInTime:
    """The read is a WHERE clause, so it is right by construction rather than
    by a filter someone has to remember to apply."""

    @pytest.fixture
    def history(self, store):
        for month in (1, 3, 6):
            put(store, "Sleep Number", f"2026-{month:02d}-01", f"v{month}")
        return store

    def test_a_run_sees_only_what_preceded_it(self, history):
        assert verdicts(history.before("g1", "decide", "Sleep Number",
                                       "2026-05-01", 10)) == ["v1", "v3"]

    def test_a_run_does_not_read_its_own_previous_answer(self, history):
        # Re-running one date must behave like running it the first time.
        assert verdicts(history.before("g1", "decide", "Sleep Number",
                                       "2026-01-01", 10)) == []

    def test_an_undated_row_cannot_be_placed_and_is_left_out(self, store):
        put(store, "Sleep Number", "", "from an older schema")
        put(store, "Sleep Number", "2026-01-01", "dated")

        assert verdicts(store.before("g1", "decide", "Sleep Number",
                                     "2026-05-01", 10)) == ["dated"]

    def test_with_no_cutoff_the_whole_record_is_read(self, history):
        assert verdicts(history.before("g1", "decide", "Sleep Number", "", 10)) == \
            ["v1", "v3", "v6"]

    def test_the_limit_keeps_the_latest_states_not_the_oldest(self, history):
        # Truncating from the wrong end hands a monitoring pipeline the
        # opening months and throws away what just happened.
        assert verdicts(history.before("g1", "decide", "Sleep Number",
                                       "2026-12-01", 2)) == ["v3", "v6"]

    def test_recorded_at_is_kept_but_never_compared(self, store):
        # Valid time and transaction time are two clocks. This row was learned
        # long after the date it is about; the cutoff must use the date.
        put(store, "Sleep Number", "2025-01-01", "old news",
            recorded_at="2026-09-07 02:00:00")

        assert verdicts(store.before("g1", "decide", "Sleep Number",
                                     "2025-06-01", 10)) == ["old news"]
        assert store.rows("g1", "decide", "Sleep Number")[0]["recorded_at"] \
            == "2026-09-07 02:00:00"


class TestConcurrentWriters:
    def test_every_record_of_a_batch_survives(self, store):
        # The vector store needed a lock, a re-open inside it, and an
        # add-then-delete ordering to get this right, and did not at first:
        # eight runs left two entries. Here the primary key does it.
        dates = [f"2026-{m:02d}-01" for m in range(1, 13)]
        # A thread that raises just looks like a missing row, which is how an
        # intermittent lock failure hid as "flaky". Collect the reason.
        failures = []

        def write(d):
            try:
                put(store, "Sleep Number", d, f"v{d}")
            except Exception as e:  # noqa: BLE001 - reported, not handled
                failures.append(f"{d}: {e!r}")

        threads = [threading.Thread(target=write, args=(d,)) for d in dates]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not failures, f"writers raised: {failures}"
        kept = {r["at"] for r in store.rows("g1", "decide", "Sleep Number")}
        assert kept == set(dates), f"lost {sorted(set(dates) - kept)}"

    def test_different_companies_written_at_once(self, store):
        names = [f"Co {i}" for i in range(12)]
        threads = [threading.Thread(target=put, args=(store, n, "2026-01-01", "v"))
                   for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert store.count("g1", "decide") == len(names)


class TestThePolicyChoosesTheKind:
    """Coze makes you pick between 数据库 and 知识库; naming a subject picks."""

    def test_naming_a_subject_means_a_table(self):
        assert memory_policy.policy(
            {"memory": {"match": "company"}})["kind"] == "table"

    def test_no_subject_means_similarity_search(self):
        assert memory_policy.policy({"memory": {"retrieve": 3}})["kind"] == "recall"

    def test_it_can_be_said_outright(self):
        assert memory_policy.policy(
            {"memory": {"match": "company", "kind": "recall"}})["kind"] == "recall"

    def test_nonsense_falls_back_rather_than_failing_the_run(self):
        assert memory_policy.policy(
            {"memory": {"match": "company", "kind": "sqlite"}})["kind"] == "table"


class TestWhatReachesThePrompt:
    task = {"name": "decide",
            "memory": {"match": "company", "at": "as_of", "retrieve": 10,
                       "context": ["as_of"], "inputs": ["company"]}}

    def test_the_block_is_dated_and_in_order(self, store):
        for month in (1, 3):
            put(store, "Sleep Number", f"2026-{month:02d}-01", f"v{month}")

        found = memory_policy.table_read(store, "g1", self.task,
                                         {"company": "Sleep Number",
                                          "as_of": "2026-06-01"})
        block = memory_policy.table_block(found["rows"], self.task,
                                          found["subject"], found["cutoff"])

        assert "recorded for company = 'Sleep Number' before 2026-06-01" in block
        assert block.index("v1") < block.index("v3")

    def test_a_run_that_names_no_subject_asks_nothing(self, store):
        assert memory_policy.table_read(store, "g1", self.task, {"as_of": "2026-01-01"}) \
            is None

    def test_a_recall_node_is_not_routed_to_a_table(self, store):
        plain = {"name": "summarise", "memory": {"retrieve": 3}}
        assert memory_policy.table_read(store, "g1", plain, {"company": "X"}) is None

    def test_what_a_run_contributes(self):
        row = memory_policy.table_write(self.task, {
            "inputs": {"company": "Sleep Number", "as_of": "2026-01-12"},
            "outputs": {"decision": "suppress"}})

        assert row["subject"] == "Sleep Number"
        assert row["at"] == "2026-01-12"
        assert row["payload"]["outputs"] == {"decision": "suppress"}

    def test_a_recall_node_contributes_no_row(self):
        assert memory_policy.table_write({"name": "s", "memory": {}}, {"outputs": {}}) \
            is None

    def test_the_payload_survives_a_round_trip(self, store):
        row = memory_policy.table_write(self.task, {
            "inputs": {"company": "Sleep Number", "as_of": "2026-01-12"},
            "outputs": {"decision": json.dumps({"action": "alert", "score": 95})}})
        store.upsert("g1", "decide", row["subject"], row["at"], row["payload"], "t")

        back = store.rows("g1", "decide", "Sleep Number")[0]
        assert json.loads(back["payload"]["outputs"]["decision"])["score"] == 95


class TestItRecoversRatherThanFailingQuietly:
    """The schema is set up once per file and remembered, which is what made
    twelve writers stop taking twelve exclusive locks before inserting. The
    memo has to be given up when the file goes away, or every later write
    fails as "no such table" for the life of the process."""

    def test_a_deleted_file_is_rebuilt(self, store):
        put(store, "Sleep Number", "2026-01-12", "first")
        store.path("g1", "decide").unlink()

        put(store, "Sleep Number", "2026-02-12", "after the file went")
        assert verdicts(store.rows("g1", "decide", "Sleep Number")) == \
            ["after the file went"]

    def test_a_write_that_cannot_land_raises_rather_than_vanishing(self, store,
                                                                   monkeypatch):
        # A silent failure is what the vector store did; anything but that.
        import sqlite3

        def refuse(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "_connect", refuse)
        monkeypatch.setattr(store, "_WRITE_ATTEMPTS", 2)
        with pytest.raises(sqlite3.OperationalError):
            put(store, "Sleep Number", "2026-01-12", "never lands")
