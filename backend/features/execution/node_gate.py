"""Node-by-node execution for one chunk of a batch.

A batch reads its records in chunks (a DataLoader batch is one chunk). By
default each record walked the whole workflow on its own, so at any moment
every node held some record: the canvas lit everything at once, and no node
ever had its calls together to hand the provider as one batch.

Here every record of a chunk starts together and they advance in step: no
record begins node `i` until every record still running has finished node
`i - 1`. So the chunk moves through the workflow one node at a time — the
order the canvas shows — and a node's model calls arrive together, which is
what lets the provider batch coalesce them.

A record that fails, is blocked or is stopped leaves the gate and never holds
the others back. Chunks still run one after another, so whatever a record
writes to memory is written before the next chunk starts.
"""

from __future__ import annotations

import threading
from typing import Callable


class NodeGate:
    """The step every record of one chunk keeps with the others."""

    def __init__(self, nodes: list[str], workers: int,
                 on_stage: Callable[[dict], None] | None = None,
                 limit_model_calls: bool = True):
        self.nodes = list(nodes)
        self._cv = threading.Condition()
        # run id -> how many nodes it has finished
        self._progress: dict[str, int] = {}
        self._order: dict[str, int] = {}
        self._joined = 0
        self._completed = [0] * len(self.nodes)
        # Who has been let into each node so far, in record order.
        self._started: list[set] = [set() for _ in self.nodes]
        self._on_stage = on_stage
        # All records of a chunk are running, so the model calls in flight
        # are bounded here instead of by the thread pool. Counted under the
        # same lock as the order, so calls also *start* in record order.
        # With the provider batch configured the coalescer decides, and a
        # limit here would only split one wave into several requests.
        self._limit = max(1, int(workers)) if limit_model_calls else None
        self._in_flight = 0

    # -- membership -------------------------------------------------------

    def join(self, run_id: str) -> None:
        """Take part. Idempotent: a record registered before its thread
        started (`expect`) keeps its place when it arrives."""
        with self._cv:
            if run_id in self._progress:
                return
            self._progress[run_id] = 0
            self._order[run_id] = self._joined
            self._joined += 1
            self._report()
            self._cv.notify_all()

    # A record is registered before its thread starts: otherwise the first
    # records to arrive see nobody behind them and run ahead into the next
    # node while the rest of the chunk is still building its agents.
    expect = join

    def leave(self, run_id: str) -> None:
        with self._cv:
            self._progress.pop(run_id, None)
            self._report()
            self._cv.notify_all()

    # -- stepping ---------------------------------------------------------

    def wait_turn(self, run_id: str, index: int,
                  cancelled: Callable[[], bool] = lambda: False,
                  model_call: bool = False) -> bool:
        """Block until this record may start node `index`.

        That is when every other running record has finished node
        `index - 1`, every running record ahead of it in the batch has
        already started this node, and — for a model call — a slot is free.
        Returns whether a model slot was taken (give it back with
        `release_slot`). Returns early, holding nothing, when stopped.
        """
        mine = self._order.get(run_id, 0)
        with self._cv:
            while not cancelled():
                if any(p < index for rid, p in self._progress.items() if rid != run_id):
                    self._cv.wait(0.2)
                    continue
                if 0 <= index < len(self._started) and any(
                        self._order.get(rid, 0) < mine and rid not in self._started[index]
                        and p <= index
                        for rid, p in self._progress.items() if rid != run_id):
                    self._cv.wait(0.2)
                    continue
                slot = model_call and self._limit is not None
                if slot and self._in_flight >= self._limit:
                    self._cv.wait(0.2)
                    continue
                if 0 <= index < len(self._started):
                    self._started[index].add(run_id)
                if slot:
                    self._in_flight += 1
                self._cv.notify_all()
                return slot
            return False

    def done(self, run_id: str, index: int) -> None:
        with self._cv:
            self._progress[run_id] = index + 1
            if 0 <= index < len(self._completed):
                self._completed[index] += 1
            self._report()
            self._cv.notify_all()

    def release_slot(self) -> None:
        with self._cv:
            self._in_flight = max(0, self._in_flight - 1)
            self._cv.notify_all()

    # -- progress ---------------------------------------------------------

    def stage(self) -> dict | None:
        """Where the chunk is: the earliest node a running record is on."""
        with self._cv:
            return self._stage_locked()

    def _stage_locked(self) -> dict | None:
        if not self.nodes:
            return None
        if self._progress:
            index = min(self._progress.values())
        else:
            index = len(self.nodes)            # everyone has left: the chunk is over
        if index >= len(self.nodes):
            return None
        # Records that can still reach this node: the ones running now plus
        # the ones that already passed it and left.
        total = sum(1 for p in self._progress.values() if p <= index) + self._completed[index]
        return {"node": self.nodes[index], "index": index + 1, "of": len(self.nodes),
                "done": self._completed[index], "total": total}

    def _report(self) -> None:
        if self._on_stage is None:
            return
        try:
            self._on_stage(self._stage_locked())
        except Exception:
            pass                      # progress is a view; it never stops a run
