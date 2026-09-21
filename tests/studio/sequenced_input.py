"""A test Input type that declares trajectories, with no domain field names.

Studio groups records into ordered trajectories only when the wired Input
type says which field is the trajectory and which is its order
(backend/features/plugins.py, "sequence"). Tests of that generic behaviour
install this type instead of leaning on a project's own dataset: the field
names here ("account", "day") mean nothing to the platform.

Not a test module (no `test_` prefix); imported by tests in this folder.
"""

from conftest import make_graph, make_task

TYPE = "test_ledger"
SEQUENCE = {"group": "account", "order": "day"}


def install(monkeypatch, records=(), *, type_=TYPE, sequence=SEQUENCE, info=None,
            outputs=("account", "day", "entry")):
    """Make `type_` a plugin Input type for the rest of the test.

    Returns a holder whose "records" list the type hands back, and whose
    "configs" list records every config it was asked for.
    """
    from backend.features import plugins

    holder = {"records": [dict(r) for r in records], "configs": []}

    def records_hook(config):
        holder["configs"].append(dict(config))
        return [dict(r) for r in holder["records"]]

    schema = {
        "label": "Test ledger",
        "description": "Rows of a ledger, one trajectory per account.",
        "config": [{"name": "n", "label": "Rows", "type": "number", "default": 0}],
        "outputs": list(outputs),
        "records": records_hook,
        "project": "tests",
    }
    if sequence:
        schema["sequence"] = dict(sequence)
    if info:
        schema["info"] = info
    monkeypatch.setattr(plugins, "_source_types", lambda: {type_: schema})
    return holder


def ledger_rows(accounts=("a",), days=("2026-01-01", "2026-01-02", "2026-01-03")):
    return [{"account": a, "day": d, "entry": f"{a} on {d}"} for a in accounts for d in days]


def graph(type_=TYPE, id="g-ledger", outputs=("account", "day", "entry"), **extra):
    """Input node of `type_` wired into one LLM task."""
    feed = make_task("feed", outputs=list(outputs))
    feed["kind"] = "source"
    feed["source"] = {"type": type_, "n": 0}
    judge = make_task("judge", inputs=["entry"], outputs=["verdict"])
    return make_graph([feed, judge], edges=[("feed", "judge")], id=id, **extra)
