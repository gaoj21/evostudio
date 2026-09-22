"""Back memory up, then empty it — the step before a measured run.

A run that starts with memory carried over from an earlier one is not
measuring the pipeline, it is measuring the pipeline plus whatever it had
been told before. The user's rule is: snapshot, then clear, before every
run. This is that rule as one call, so it is not a thing to remember.

Nothing is ever deleted outright: every reset leaves a timestamped copy
under `memory-backups/`, and those are never removed here.
"""

import shutil
from datetime import datetime, timezone

from backend.api.studio_config import data_path

STORES = ("memory", "tables", "stm")
BACKUPS = data_path("memory-backups")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _size(path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.is_dir() else 0


def _targets(graph_id: str | None):
    """What a reset touches: every store, or only one workflow's part of each."""
    for store in STORES:
        root = data_path(store)
        if graph_id is None:
            yield store, root
        elif store == "stm":
            yield store, root / f"{graph_id}.json"
        else:
            yield store, root / graph_id


def snapshot_and_clear(graph_id: str | None = None, *, busy: bool = False) -> dict:
    """Copy the stores aside, then empty them. Refused while anything runs.

    Returns where the copy went and how much each store held, so the caller
    can say so; a reset that reports nothing is one nobody trusts.
    """
    if busy:
        raise RuntimeError("A run or batch is in progress; memory is left as it is.")
    stamp = _stamp()
    dest = BACKUPS / stamp
    sizes = {}
    for store, target in _targets(graph_id):
        if not target.exists():
            sizes[store] = 0
            continue
        sizes[store] = _size(target)
        copy_to = dest / store / (target.name if graph_id else "")
        copy_to.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            shutil.copytree(target, copy_to if graph_id else dest / store, dirs_exist_ok=True)
            shutil.rmtree(target)
            if graph_id is None:
                target.mkdir(parents=True, exist_ok=True)
        else:
            shutil.copy2(target, copy_to)
            target.unlink()
    # The server keeps a memo of table files it has opened; the files are
    # gone now, and a memo that outlives them costs every later write.
    from backend.api import table_store
    table_store.forget(graph_id)
    return {"backup": str(dest), "stamp": stamp, "graph_id": graph_id, "sizes": sizes}


def backups() -> list[dict]:
    if not BACKUPS.is_dir():
        return []
    return sorted(({"stamp": p.name, "path": str(p), "size": _size(p)}
                   for p in BACKUPS.iterdir() if p.is_dir()),
                  key=lambda b: b["stamp"], reverse=True)
