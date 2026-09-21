"""Trajectories come from the Input type, not from field names.

An Input type may declare {"group", "order"}: records sharing the group field
are one trajectory, run in order, and a failure stops the rest of that
trajectory only. Nothing in the platform knows the names, so these use a
ledger ("account", "day") rather than any project's own fields.
"""

import pytest

import sequenced_input


@pytest.fixture
def batches(tmp_path, monkeypatch):
    from backend.api import batch as batch_module
    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    monkeypatch.setattr(batch_module, "_pause", lambda seconds: None)
    batch_module._batches.clear()
    batch_module._threads.clear()
    yield batch_module
    for batch_id in list(batch_module._threads):
        assert batch_module.wait_for(batch_id, timeout=10)


class Recorder:
    def __init__(self, fail=()):
        self.calls, self.runs, self.fail = [], {}, set(fail)

    def start_run(self, graph, record, background, gray_zone, run_id, batch_id,
                  session_started_at, **kw):
        key = (record.get("account"), record.get("day"))
        self.calls.append(key)
        self.runs[run_id] = ({"status": "failed", "error": "boom"} if key in self.fail
                             else {"status": "success", "result": {"ok": True}})

    def get_run(self, run_id):
        return self.runs.get(run_id)


DAYS = ("2026-01-01", "2026-01-02", "2026-01-03")


def run(batches, monkeypatch, records, graph, fail=(), workers=1):
    rec = Recorder(fail)
    monkeypatch.setattr(batches.runner, "start_run", rec.start_run)
    monkeypatch.setattr(batches.runner, "get_run", rec.get_run)
    bid = batches.start_batch(graph, records, {"type": sequenced_input.TYPE}, workers=workers)
    assert batches.wait_for(bid, timeout=10)
    return rec, batches.get_batch(bid)


def test_items_carry_the_declared_trajectory(batches, monkeypatch):
    sequenced_input.install(monkeypatch)
    records = sequenced_input.ledger_rows(accounts=("a", "b"), days=DAYS[:1])
    _rec, state = run(batches, monkeypatch, records, sequenced_input.graph())
    assert [i["trajectory"] for i in state["items"]] == ["a", "b"]


def test_a_failure_blocks_only_its_own_trajectory(batches, monkeypatch):
    sequenced_input.install(monkeypatch)
    records = sequenced_input.ledger_rows(accounts=("a", "b"), days=DAYS)
    rec, state = run(batches, monkeypatch, records, sequenced_input.graph(),
                     fail={("a", "2026-01-02")}, workers=2)

    by_key = {(i["inputs"]["account"], i["inputs"]["day"]): i for i in state["items"]}
    assert [by_key[("a", d)]["status"] for d in DAYS] == ["success", "failed", "blocked"]
    assert [by_key[("b", d)]["status"] for d in DAYS] == ["success"] * 3
    assert ("a", "2026-01-03") not in rec.calls
    # Each trajectory ran in its declared order.
    assert [d for a, d in rec.calls if a == "b"] == list(DAYS)
    assert [d for a, d in rec.calls if a == "a"] == list(DAYS[:2])
    assert state["status"] == "completed_with_errors"


def test_the_group_field_is_whatever_the_input_declares(batches, monkeypatch):
    # Same records, but the Input says the trajectory is "day": now a failure
    # on (a, 01-02) blocks (b, 01-02), and nothing on account "a".
    sequenced_input.install(monkeypatch, sequence={"group": "day", "order": "account"})
    records = sequenced_input.ledger_rows(accounts=("a", "b"), days=DAYS)
    rec, state = run(batches, monkeypatch, records, sequenced_input.graph(),
                     fail={("a", "2026-01-02")})
    by_key = {(i["inputs"]["account"], i["inputs"]["day"]): i["status"] for i in state["items"]}
    assert by_key[("b", "2026-01-02")] == "blocked"
    assert by_key[("a", "2026-01-03")] == "success"


def test_without_a_declared_sequence_a_failure_blocks_nothing(batches, monkeypatch):
    sequenced_input.install(monkeypatch, sequence=None)
    records = sequenced_input.ledger_rows(accounts=("a",), days=DAYS)
    rec, state = run(batches, monkeypatch, records, sequenced_input.graph(),
                     fail={("a", "2026-01-02")})
    assert [i["status"] for i in state["items"]] == ["success", "failed", "success"]
    assert "trajectory" not in state["items"][0]


def test_an_unwired_input_declares_nothing(monkeypatch):
    from backend.api import sources
    sequenced_input.install(monkeypatch)
    graph = sequenced_input.graph()
    assert sources.input_sequence(graph) == sequenced_input.SEQUENCE
    graph["edges"] = []
    assert sources.input_sequence(graph) is None
    graph = sequenced_input.graph()
    graph["tasks"][0]["enabled"] = False
    assert sources.input_sequence(graph) is None


def test_a_record_without_the_group_field_stands_alone(batches, monkeypatch):
    sequenced_input.install(monkeypatch)
    records = [{"account": "a", "day": DAYS[0], "entry": "x"},
               {"day": DAYS[1], "entry": "no account"},
               {"account": "a", "day": DAYS[2], "entry": "y"}]
    _rec, state = run(batches, monkeypatch, records, sequenced_input.graph())
    assert [i.get("trajectory") for i in state["items"]] == ["a", None, "a"]


@pytest.mark.parametrize("sequence, n, expected", [
    (sequenced_input.SEQUENCE, 4, None),   # n counts trajectories, not records
    (None, 4, 4),
    (None, 0, None),                       # 0 = everything: size unknown
])
def test_a_plan_does_not_guess_how_many_records_trajectories_yield(
        monkeypatch, sequence, n, expected):
    from backend.features.workflow import run_plan
    sequenced_input.install(monkeypatch, sequence=sequence)
    graph = sequenced_input.graph()
    graph["tasks"][0]["source"]["n"] = n
    source = run_plan._source_of(graph["tasks"], graph["edges"])
    assert source["cardinality"] == expected
