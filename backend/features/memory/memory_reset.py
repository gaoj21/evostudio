"""Back memory up, empty it, or both — the steps around a measured run.

A run that starts with memory carried over from an earlier one is not
measuring the pipeline, it is measuring the pipeline plus whatever it had
been told before. So there are three operations, and the caller chooses:

- `snapshot()` — copy the stores aside and leave them in place;
- `clear()` — empty them (it snapshots first unless told not to);
- `snapshot_and_clear()` — the usual step before a measured run.

Nothing here ever removes a backup, and `clear(backup=False)` is the only
way to lose data — it says so in the API and the UI.
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


def snapshot(graph_id: str | None = None) -> dict:
    """Copy the stores aside, leaving memory exactly as it is.

    Safe at any time: a copy of a store being written to may catch a write
    mid-flight, which is why a reset before a measured run is still the
    cleaner moment — but a backup must never be refused for being
    inconvenient.
    """
    stamp = _stamp()
    dest = BACKUPS / stamp
    # Two backups in the same second are two backups, not one folder.
    suffix = 2
    while dest.exists():
        dest = BACKUPS / f"{stamp}-{suffix}"
        suffix += 1
    stamp = dest.name
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
        else:
            shutil.copy2(target, copy_to)
    # Nothing there to copy: say so rather than name a folder that was never
    # written — a backup nobody can find is worse than none.
    saved = dest.exists()
    return {"backup": str(dest) if saved else None, "saved": saved, "stamp": stamp,
            "graph_id": graph_id, "sizes": sizes, "cleared": False}


def clear(graph_id: str | None = None, *, busy: bool = False, backup: bool = True) -> dict:
    """Empty the stores. Refused while anything runs.

    `backup=False` deletes without a copy; nothing else in Studio does that,
    and the caller has to ask for it explicitly.
    """
    if busy:
        raise RuntimeError("A run or batch is in progress; memory is left as it is.")
    taken = snapshot(graph_id) if backup else {"backup": None, "saved": False,
                                               "stamp": _stamp(), "graph_id": graph_id,
                                               "sizes": {}}
    sizes = dict(taken["sizes"])
    for store, target in _targets(graph_id):
        if not target.exists():
            sizes.setdefault(store, 0)
            continue
        sizes.setdefault(store, _size(target))
        if target.is_dir():
            shutil.rmtree(target)
            if graph_id is None:
                target.mkdir(parents=True, exist_ok=True)
        else:
            target.unlink()
    # The server keeps a memo of table files it has opened; the files are
    # gone now, and a memo that outlives them costs every later write.
    from backend.api import table_store
    table_store.forget(graph_id)
    return {**taken, "sizes": sizes, "cleared": True}


def snapshot_and_clear(graph_id: str | None = None, *, busy: bool = False) -> dict:
    """Back up, then empty — the step before a measured run."""
    return clear(graph_id, busy=busy, backup=True)


def backups() -> list[dict]:
    if not BACKUPS.is_dir():
        return []
    return sorted(({"stamp": p.name, "path": str(p), "size": _size(p)}
                   for p in BACKUPS.iterdir() if p.is_dir()),
                  key=lambda b: b["stamp"], reverse=True)
