"""The Usage tab's data: total, per node, per record — with costs priced by
the machine's own llm package, and 'not reported' never shown as zero."""

from backend.features.execution import token_usage, usage_api


def usage(inp, out, cache=None, reasoning=None, calls=1):
    entry = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
             "reported_calls": calls, "source": "provider"}
    if cache is not None:
        entry["cache_read_tokens"] = cache
    if reasoning is not None:
        entry["reasoning_tokens"] = reasoning
    return entry


def test_cache_and_reasoning_tokens_are_kept_when_reported():
    from llm import LLMUsage
    reported = token_usage.reported_usage(LLMUsage(100, 20, 120, 80, 5))

    assert reported["cache_read_tokens"] == 80 and reported["reasoning_tokens"] == 5
    plain = token_usage.reported_usage({"prompt_tokens": 10, "completion_tokens": 2})
    assert "cache_read_tokens" not in plain and "reasoning_tokens" not in plain


def test_a_run_breaks_down_by_node_with_shares_and_cost():
    run = {"run_id": "r1", "status": "success", "token_usage": usage(300, 100),
           "nodes": [{"name": "a", "status": "completed", "token_usage": usage(100, 50)},
                     {"name": "b", "status": "completed", "token_usage": usage(200, 50)},
                     {"name": "src", "status": "completed"},
                     {"name": "off", "status": "skipped"}]}

    out = usage_api.run_usage(run)

    assert out["total"]["reported"] and out["total"]["cost"]["total_cost"] > 0
    by_node = {row["node"]: row for row in out["by_node"]}
    assert set(by_node) == {"a", "b", "src"}
    assert by_node["a"]["share"] == 0.375 and by_node["b"]["share"] == 0.625
    assert by_node["src"]["usage"] == {"reported": False} and by_node["src"]["share"] is None


def test_a_batch_breaks_down_by_node_and_by_record():
    batch = {"batch_id": "b1", "status": "running", "token_usage": usage(40, 10, calls=2),
             "node_progress": {"a": {"token_usage": usage(40, 10, calls=2)}},
             "items": [{"index": 0, "run_id": "r0", "status": "success", "token_usage": usage(20, 5)},
                       {"index": 1, "run_id": "r1", "status": "running", "token_usage": usage(20, 5)},
                       {"index": 2, "run_id": None, "status": "pending"}]}

    out = usage_api.batch_usage(batch)

    assert out["running"] is True
    assert [row["share"] for row in out["by_record"]] == [0.5, 0.5, None]
    assert out["by_record"][2]["usage"] == {"reported": False}
    assert out["by_node"][0]["usage"]["reported_calls"] == 2


def test_the_route_wants_exactly_one_target(studio_data):
    from fastapi.testclient import TestClient
    from backend.api.app import app
    client = TestClient(app)
    assert client.get("/api/usage").status_code == 422
    assert client.get("/api/usage?run_id=a&batch_id=b").status_code == 422
    assert client.get("/api/usage?run_id=missing").status_code == 404


def test_usage_is_read_from_any_class_that_carries_the_contract_fields():
    """The contract names LLMUsage's fields, not its class: another machine's
    package may use a slots dataclass or a plain object."""
    from dataclasses import dataclass

    @dataclass(slots=True)
    class Slotted:
        input_tokens: int
        output_tokens: int
        total_tokens: int
        cache_read_tokens: int | None = None
        reasoning_tokens: int | None = None

    class Plain:
        __slots__ = ("prompt_tokens", "completion_tokens")

        def __init__(self):
            self.prompt_tokens, self.completion_tokens = 7, 3

    class Named:
        @property
        def input_tokens(self): return 11
        @property
        def output_tokens(self): return 4
        @property
        def total_tokens(self): return 15

    assert token_usage.reported_usage(Slotted(10, 5, 15, 6))["cache_read_tokens"] == 6
    assert token_usage.reported_usage(Plain())["total_tokens"] == 10
    assert token_usage.reported_usage(Named())["input_tokens"] == 11
    assert token_usage.reported_usage(None) is None


def test_a_call_with_no_usage_is_counted_not_dropped():
    from types import SimpleNamespace
    from backend.features import model_bridge
    state = {"_usage_node": {}}
    key = token_usage.usage_key(state)
    try:
        model_bridge._report(key, SimpleNamespace(content="x", usage=None))
        model_bridge._report(key, SimpleNamespace(content="y", usage={"input_tokens": 3, "output_tokens": 1}))
    finally:
        token_usage.release_usage(state)

    assert state["token_usage"]["unreported_calls"] == 1
    assert state["token_usage"]["reported_calls"] == 1
    assert state["_usage_node"]["token_usage"]["unreported_calls"] == 1
    entry = usage_api._entry(state["token_usage"])
    assert entry["reported"] and entry["unreported_calls"] == 1
    none = usage_api._entry({"unreported_calls": 2})
    assert none == {"reported": False, "unreported_calls": 2}
    assert token_usage.combined({"unreported_calls": 2}, usage(1, 1))["unreported_calls"] == 2


def test_usage_that_only_answers_as_dict_is_read():
    """SafeChain's LLMUsage: `result.usage.as_dict()` is how its counts come out."""
    class AsDictOnly:
        __slots__ = ()

        def as_dict(self):
            return {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17,
                    "cache_read_tokens": 4, "reasoning_tokens": None}

    assert token_usage.reported_usage(AsDictOnly()) == {
        "input_tokens": 12, "output_tokens": 5, "total_tokens": 17, "cache_read_tokens": 4}
