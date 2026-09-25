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
