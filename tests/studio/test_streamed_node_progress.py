"""A DataLoader batch, streamed chunk by chunk and run node by node, shows each
node done the moment the chunk has finished it — and its usage as it grows."""

import threading
import time

from test_node_by_node import batches, calls, three_nodes  # noqa: F401  (fixtures)


def test_streamed_chunks_show_node_progress_and_usage_live(batches, calls, monkeypatch):
    from backend.api import runner
    from backend.features.execution import token_usage
    answer = runner.execute_llm_node

    async def spending(agent, task, inputs, state):
        out = await answer(agent, task, inputs, state)
        token_usage.record(state, {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        return out

    monkeypatch.setattr(runner, "execute_llm_node", spending)

    def chunks(cancelled, on_info=None):
        for start in (0, 16):
            rows = [{"id": f"r{i}"} for i in range(start, start + 16)]
            yield rows, None, len(rows)

    batch_id = batches.start_batch(three_nodes(), [], {"type": "canvas"}, workers=4,
                                   record_chunks=chunks, mode="node")
    seen = []
    while not batches.wait_for(batch_id, timeout=0.005):
        b = batches.get_batch(batch_id)
        p = b.get("node_progress") or {}
        seen.append((b.get("stage"), {n: v["completed"] for n, v in p.items()},
                     len(b.get("items") or []), (b.get("token_usage") or {}).get("total_tokens")))
    # While the first chunk was on c, a and b had already finished all 16.
    on_c = [s for s in seen if s[0] and s[0]["node"] == "c" and s[2] == 16]
    assert on_c, seen[-5:]
    assert on_c[-1][1]["a"] == 16 and on_c[-1][1]["b"] == 16, on_c[-1]
    # Usage moved before the batch was over.
    live = [s[3] for s in seen if s[3]]
    assert live and min(live) < 32 * 3 * 12, seen[-3:]
    final = batches.get_batch(batch_id)
    assert final["token_usage"]["total_tokens"] == 32 * 3 * 12
