"""Graph CRUD for EvoAgentX Studio.

Persists canvas graphs as JSON at studio-data/graphs/{id}.json and validates
them by constructing a real SequentialWorkFlowGraph.
"""

import copy
import json
import math
import os
import uuid
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from backend.api.studio_config import DATA_DIR
from backend.features.persistence import is_safe_id, name_tag, replace, safe_id

GRAPHS_DIR = DATA_DIR / "graphs"

_lock = threading.Lock()

# Keys kept on a canvas task; everything else (e.g. x/y positions) is stripped
# before handing tasks to SequentialWorkFlowGraph.
TASK_KEYS = [
    "name", "description", "inputs", "outputs", "prompt",
    "system_prompt", "parse_mode", "tool_names",
]

# Canvas-only: `skill_names` is resolved into the node's system prompt at run
# time (skills_api.inject_into_tasks), and `memory` is read by the Studio when
# it writes to the node's store (memory_policy) — neither reaches the framework.


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    # A name in Chinese (say) keeps a tag of what was dropped, rather than
    # every such workflow becoming "graph", or "信用 Risk" becoming "risk".
    tag = name_tag(name)
    return f"{slug or 'graph'}-{tag}" if tag else (slug or "graph")


def _graph_path(graph_id: str) -> Path:
    return GRAPHS_DIR / f"{safe_id(graph_id, 'graph id')}.json"


def graph_exists(graph_id: str) -> bool:
    return is_safe_id(graph_id) and _graph_path(graph_id).is_file()


def create_graph(name: str, goal: str) -> dict:
    with _lock:
        GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        graph_id = free_id(name)
        graph = {
            "id": graph_id,
            "name": name or graph_id,
            "goal": goal or "",
            "output_dir": "runs",
            "tasks": [],
            "edges": [],
            "updated_at": _utcnow(),
        }
        _save(graph)
        return graph


def free_id(name: str, taken: str | None = None) -> str:
    """An unused id for a workflow called `name`.

    `taken` is an id the caller already owns, so re-deriving its own id does
    not count as a collision.
    """
    base = _slugify(name or "graph")
    candidate = base
    n = 2
    while candidate != taken and (graph_exists(candidate) or _has_data(candidate)):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _data_paths(graph_id: str) -> list[Path]:
    """Everything filed under a workflow's id besides its graph file."""
    from backend.api import memory_store
    from backend.api import stm_store
    from backend.api import table_store
    from backend.api import evaluator_tools
    from backend.api import workspace as workspace_mod
    # The unsaved evaluator draft too: left behind, a new workflow given the
    # id would open with the deleted one's pasted code.
    return [workspace_mod.workspace_root(graph_id), memory_store.MEMORY_DIR / graph_id,
            table_store.TABLES_DIR / graph_id, stm_store.STM_DIR / f"{graph_id}.json",
            evaluator_tools.DRAFTS_DIR / f"{graph_id}.json"]


def _has_data(graph_id: str) -> bool:
    """Does some workflow's data still sit under this id?

    A workflow deleted before its data was archived left its memory and
    history behind; a new workflow given the id would inherit both.
    """
    if is_archiving(graph_id) or any(p.exists() for p in _data_paths(graph_id)):
        return True
    from backend.api import runner
    return bool(runner._run_index(graph_id)[0]) or bool(_history_files(graph_id))


def rename_graph(graph_id: str, name: str) -> dict:
    """Give a workflow a new name, and the identity to match.

    A workflow's id is its identity everywhere: the file on disk, the folder
    its run artifacts land in, its memory stores, the name of the project it
    exports as. Leaving that behind when the workflow is renamed makes the name
    decoration — you call something "Supplier Risk", hand the export to a
    colleague, and they receive untitled-workflow-3.zip.

    Returns the graph under its new id. The old id is not left behind.
    """
    graph = load_graph(graph_id)
    if graph is None:
        raise GraphValidationError([f"Workflow '{graph_id}' not found"])

    new_id = free_id(name, taken=graph_id)
    if new_id == graph_id:
        graph["name"] = name
        with _lock:
            _save(graph)
        return graph

    busy = _in_flight(graph_id)
    if busy:
        raise GraphValidationError([
            f"'{graph_id}' has {busy} in progress. Renaming moves the folders "
            "they are writing to, so let them finish first."
        ])

    # free_id counts leftover data as taken, so a destination in use here is
    # a race; refuse before anything moves rather than merge two workflows.
    occupied = [p.name for p in _data_paths(new_id) if p.exists()]
    if occupied:
        raise GraphValidationError([
            f"Cannot rename to '{new_id}': data is already filed under that id "
            f"({', '.join(occupied)})."
        ])

    with _lock:
        # Legacy Chat Agents and sessions are keyed by the original graph ID.
        # Keep that owner stable when the editable graph name/ID changes.
        graph['task_id'] = graph.get('task_id') or graph_id
        # The marker goes first, as for a delete: a server stopped anywhere
        # after this finishes the rename at its next start (resume_refiles).
        _write_marker(graph_id, new_id, graph['task_id'], kind="rename")
        graph["id"] = new_id
        graph["name"] = name
        _save(graph)
        _graph_path(graph_id).unlink(missing_ok=True)
    failed = _move_graph_data(graph_id, new_id)
    if failed:
        # The rename stands (the graph is saved under its new id); what did
        # not move is said rather than silently left under the old one.
        return {**graph, "rename_warnings": failed}
    return graph


def _in_flight(graph_id: str) -> str:
    """What is still running for a workflow, as a phrase, or an empty string."""
    from backend.api import batch as batch_store
    from backend.api import runner
    runs = sum(1 for r in runner.list_runs(graph_id=graph_id)
               if r.get("status") == "running")
    batches = sum(1 for b in batch_store.list_batches(graph_id=graph_id)
                  if b.get("status") in ("running", "cancelling"))
    parts = []
    if runs:
        parts.append(f"{runs} run{'s' if runs > 1 else ''}")
    if batches:
        parts.append(f"{batches} batch{'es' if batches > 1 else ''}")
    # An Evolve task saves its result, and a watcher starts its runs, under
    # the id they were started with.
    try:
        from backend.features.evaluation import evolve_api
        with evolve_api._lock:
            if any(t.get("graph_id") == graph_id and t.get("status") == "running"
                   for t in evolve_api._tasks.values()):
                parts.append("an Evolve task")
    except ImportError:                 # optimizer dependencies not installed
        pass
    from backend.features.execution import watcher
    if watcher.graph_watch_status(graph_id).get("watching"):
        parts.append("a source watch")
    return " and ".join(parts)


def _move_graph_data(old_id: str, new_id: str) -> list[str]:
    """Carry a renamed workflow's own data across; returns what did not move.

    Per item: a workflow that has never run has none of these, and failing to
    move one is not a reason to leave the rename half-done — but it is said,
    never skipped in silence.
    """
    import logging
    import shutil

    failed = []
    for old, new in zip(_data_paths(old_id), _data_paths(new_id)):
        if not old.exists():
            continue
        try:
            if new.exists():
                raise FileExistsError(f"{new} already exists")
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "rename %s -> %s: could not move %s: %s", old_id, new_id, old, exc)
            failed.append(f"{old.name} stayed under '{old_id}': {exc}")

    # The schedule is filed under the id: every rename carries it, the
    # Chat's save_graph and an Evolve apply as well as the rename endpoint.
    try:
        from backend.features.execution import scheduler
        scheduler.rename(old_id, new_id)
    except Exception as exc:  # noqa: BLE001 - said, not fatal to the rename
        failed.append(f"the schedule stayed under '{old_id}': {exc}")
    # What each source watcher has already run: left behind, watching the
    # renamed workflow would run every old record again.
    from backend.features.execution import watcher
    graph = load_graph(new_id) or {}
    for task in graph.get("tasks") or []:
        old, new = watcher._seen_path(old_id, task.get("name") or ""), \
            watcher._seen_path(new_id, task.get("name") or "")
        if old.is_file() and not new.exists():
            try:
                replace(old, new)
            except OSError as exc:
                failed.append(f"{old.name} stayed under '{old_id}': {exc}")

    # Runs and batches still in memory follow at once; the files — every
    # run and batch the workflow ever had, on a synced folder minutes for a
    # long history — are refiled after the rename has answered, as for a
    # delete. Meanwhile they are listed under the new id (history_ids).
    _repoint_live(history_ids(old_id), new_id)
    threading.Thread(target=_refile, args=(old_id, new_id),
                     name=f"rename-{old_id}", daemon=True).start()
    return failed


# Ids whose history is still being refiled, each to the id it goes to (a
# rename's new id, a delete's archived one). Loaded from the markers on first
# use, so a refile a restart cut short is listed right from the start.
_forward: dict[str, str] = {}
_forward_from: list = [None]
# One refile at a time per file: a delete or second rename racing a refile
# re-reads the file and skips it once it no longer names the old id.
_history_write_lock = threading.Lock()


def _forward_map() -> dict:
    """Called with _archiving_lock held."""
    folder = _refile_markers_dir()
    if _forward_from[0] != folder:
        _forward.clear()
        _forward_from[0] = folder
        if folder.is_dir():
            for marker in folder.glob("*.json"):
                try:
                    data = json.loads(marker.read_text(encoding="utf-8"))
                    _forward.setdefault(str(data["old_id"]), str(data["new_id"]))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
    return _forward


def history_ids(graph_id: str) -> set:
    """`graph_id` and every id whose history is still being refiled to it.

    A renamed workflow's runs and batches keep its old id on disk until the
    background refile reaches them; everything that lists or checks a
    workflow's history goes through this, so they are its own meanwhile."""
    ids = {graph_id}
    with _archiving_lock:
        forward = _forward_map()
        if not forward:
            return ids
        grew = True
        while grew:
            grew = False
            for old, new in forward.items():
                if new in ids and old not in ids:
                    ids.add(old)
                    grew = True
    return ids


def current_id(graph_id: str) -> str:
    """The id a workflow's history is being refiled to, following renames
    still in progress; `graph_id` itself otherwise."""
    seen = set()
    with _archiving_lock:
        forward = _forward_map()
        while graph_id in forward and graph_id not in seen:
            seen.add(graph_id)
            graph_id = forward[graph_id]
    return graph_id


def _repoint_live(ids: set, new_id: str) -> None:
    """Runs, batches and Evolve tasks still in memory, to `new_id`."""
    from backend.api import batch as batch_store
    from backend.api import runner
    stores = [runner._runs, batch_store._batches]
    try:
        from backend.features.evaluation import evolve_api
        stores.append(evolve_api._tasks)
    except ImportError:
        pass
    for store in stores:
        for state in list(store.values()):
            if state.get("graph_id") in ids:
                state["graph_id"] = new_id


def _evolve_graph(document: dict) -> dict:
    return {"graph_id": document.get("graph_id")}


def _history_files(graph_id: str) -> list[tuple[Path, int | None]]:
    """The run and batch files filed under `graph_id`, each with the indent
    its writer uses. Chosen on the small cached view each listing already
    keeps of every file — not by parsing every run and batch, which is the
    whole of the data directory."""
    from backend.api import batch as batch_store
    from backend.api import runner
    from backend.features.persistence import file_view
    # A run still in memory is listed from memory (path None), but its file
    # is on disk too and must follow, or it keeps the old id.
    ids = history_ids(graph_id)
    found = []
    for run_id, _, path in runner._run_index(graph_id)[0]:
        path = path or runner.RUNS_DIR / f"{run_id}.json"
        if path.is_file():
            found.append((path, 2))
    if batch_store.BATCHES_DIR.is_dir():
        for path in batch_store.BATCHES_DIR.glob("*.json"):
            digest = file_view(path, "batch-digest", batch_store._digest, persist=True)
            if digest is not None and digest.get("graph_id") in ids:
                found.append((path, None))
    # Evolve tasks are listed by workflow too.
    try:
        from backend.features.evaluation import evolve_api
    except ImportError:
        return found
    if evolve_api.EVOLVE_DIR.is_dir():
        for path in evolve_api.EVOLVE_DIR.glob("*/result.json"):
            view = file_view(path, "evolve-graph", _evolve_graph, persist=True)
            if view is not None and view.get("graph_id") in ids:
                found.append((path, "evolve"))
    return found


def _repoint_history(old_id: str, new_id: str, files=None) -> None:
    """Point this workflow's past runs, batches and Evolve tasks at its new id.

    Without this its own history disappears from the History panel the moment
    it is renamed — the records are still there, filed under a name that no
    longer exists. Only this workflow's files are rewritten, each atomically,
    and the listings' views of them are updated with them. Each goes to where
    the workflow is now (a rename or delete since started refiles it too)."""
    from backend.api import batch as batch_store
    from backend.api import runner
    from backend.features.persistence import file_version, remember_view, write_json
    ids = history_ids(old_id)
    _repoint_live(ids, current_id(new_id))
    for path, indent in (_history_files(old_id) if files is None else files):
        # A batch file is also written by its batch: under its lock, so a
        # snapshot written meanwhile is neither lost nor overwritten.
        batch_lock = batch_store._persist_lock if indent is None else _no_lock
        with _history_write_lock, batch_lock:
            # A writer that does not take these locks may change the file
            # between the read and the write: then read it again rather
            # than write the older copy over it.
            data = None
            for _attempt in range(3):
                version = file_version(path)
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    data = None
                    break
                if data.get("graph_id") not in ids:
                    data = None
                    break
                data["graph_id"] = current_id(new_id)
                if file_version(path) == version:
                    break
            else:
                data = None
            if data is None:
                continue
            try:
                write_json(path, data, indent=2 if indent == "evolve" else indent)
            except OSError:
                continue
            if indent is None:
                remember_view(path, "batch-digest", batch_store._digest(data), persist=True)
            elif indent == "evolve":
                remember_view(path, "evolve-graph", _evolve_graph(data), persist=True)
            else:
                remember_view(path, "run-meta", runner._run_meta(data), persist=True)


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_no_lock = _NoLock()


# Ids whose history is still being refiled after a delete: taken until done.
_archiving: set[str] = set()
_archiving_lock = threading.Lock()


def _refile_markers_dir() -> Path:
    """One marker per refile still to finish. Written before a refile starts
    and removed when it is done, so a refile the server stopped in the middle
    of is finished at the next start instead of leaving part of the history
    under the deleted id. A folder, not `*.json` beside the graphs: the graph
    listing globs those."""
    return GRAPHS_DIR / ".refiling"


def _refile_marker(graph_id: str) -> Path:
    return _refile_markers_dir() / f"{safe_id(graph_id, 'graph id')}.json"


def is_archiving(graph_id: str) -> bool:
    """Is a deleted workflow's history still being refiled away from this id?

    Counts a marker left by a server that stopped mid-refile as well, so the
    id stays taken from the first request after a restart, before the
    startup cleanup has picked the refile up again."""
    if graph_id in _archiving:
        return True
    return is_safe_id(graph_id) and _refile_marker(graph_id).is_file()


def _refile(old_id: str, new_id: str, files=None) -> None:
    """Refile `old_id`'s history under `new_id`, then drop its marker.

    Idempotent: a file already pointing elsewhere is skipped, so running it
    again after an interrupted attempt only finishes what is left."""
    import logging
    try:
        _repoint_history(old_id, new_id, files)
    except Exception as exc:  # noqa: BLE001 - a background thread must not die silently
        # The marker stays: the next start tries again.
        logging.getLogger(__name__).warning(
            "delete %s: could not refile its history: %s", old_id, exc)
    else:
        _refile_marker(old_id).unlink(missing_ok=True)
        with _archiving_lock:
            _forward_map().pop(old_id, None)
    finally:
        with _archiving_lock:
            _archiving.discard(old_id)


def resume_refiles() -> list[str]:
    """Finish the refiles a previous process left half-done; returns their ids.

    Called from the startup cleanup thread. Each id is held in `_archiving`
    while its refile runs, as during the original delete."""
    import logging
    folder = _refile_markers_dir()
    if not folder.is_dir():
        return []
    pending = []
    for marker in sorted(folder.glob("*.json")):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            old_id, new_id, task_id = data["old_id"], data["new_id"], data.get("task_id")
            kind = data.get("kind") or "delete"
            if not (is_safe_id(old_id) and is_safe_id(new_id)) or marker.stem != old_id:
                raise ValueError("ids do not match the marker")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logging.getLogger(__name__).warning(
                "unreadable refile marker %s left in place: %s", marker, exc)
            continue
        with _archiving_lock:
            if old_id in _archiving:        # this process is already on it
                continue
            _archiving.add(old_id)
        pending.append((old_id, new_id, task_id, kind))
    for old_id, new_id, task_id, kind in pending:
        if kind == "rename":
            _resume_rename(old_id, new_id)
            continue
        if _graph_path(old_id).is_file():
            # Stopped before the workflow file went: nothing was deleted.
            _refile_marker(old_id).unlink(missing_ok=True)
            with _archiving_lock:
                _archiving.discard(old_id)
            continue
        # Moving what is still in place and refiling are both safe to repeat.
        _archive_graph_data(old_id, new_id, background=False, task_id=task_id)
    return [old_id for old_id, *_ in pending]


def _resume_rename(old_id: str, new_id: str) -> None:
    """Finish a rename a stopped server left half-done."""
    if _graph_path(old_id).is_file() and not _graph_path(new_id).is_file():
        # Stopped before the workflow was saved under its new id: no rename.
        _refile_marker(old_id).unlink(missing_ok=True)
        with _archiving_lock:
            _archiving.discard(old_id)
            _forward_map().pop(old_id, None)
        return
    _graph_path(old_id).unlink(missing_ok=True)
    target = current_id(new_id)
    for old, new in zip(_data_paths(old_id), _data_paths(target)):
        if old.exists() and not new.exists():
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.move(str(old), str(new))
            except OSError:
                pass
    _refile(old_id, new_id)


def _save(graph: dict) -> None:
    graph["updated_at"] = _utcnow()
    GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
    target = _graph_path(graph['id'])
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(graph, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def load_graph(graph_id: str) -> dict | None:
    if not is_safe_id(graph_id):
        return None
    path = _graph_path(graph_id)
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return migrate_flow(json.load(f))


def list_graphs() -> list[dict]:
    if not GRAPHS_DIR.is_dir():
        return []
    out = []
    for path in sorted(GRAPHS_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                g = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": g.get("id", path.stem),
            "name": g.get("name", ""),
            "goal": g.get("goal", ""),
            "updated_at": g.get("updated_at", ""),
            "project_id": g.get("project_id"),
            "task_id": g.get("task_id"),
        })
    out.sort(key=lambda g: g.get("updated_at", ""), reverse=True)
    return out


def delete_graph(graph_id: str) -> bool:
    if not is_safe_id(graph_id):
        return False
    with _lock:
        path = _graph_path(graph_id)
        if not path.is_file():
            return False
        # The marker goes first: a server stopped anywhere after this point
        # finishes the delete at its next start (resume_refiles), instead of
        # leaving data or history under an id a new workflow could take.
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            task_id = document.get("task_id")
            nodes = [t.get("name") for t in document.get("tasks") or [] if isinstance(t, dict)]
        except (OSError, ValueError, AttributeError):
            task_id, nodes = None, []
        archived_id = _archived_id(graph_id)
        _write_marker(graph_id, archived_id, task_id)
        path.unlink()
        _archive_watch_state(graph_id, archived_id, nodes)
    _archive_graph_data(graph_id, archived_id, task_id=task_id)
    return True


def _archive_watch_state(graph_id: str, archived_id: str, nodes) -> None:
    """What each source watcher already ran, set aside with the rest: a new
    workflow of that name must not skip records as already seen."""
    from backend.features.execution import watcher
    stamp = archived_id.rsplit(".deleted-", 1)[1]
    for name in nodes:
        seen = watcher._seen_path(graph_id, name or "")
        if seen.is_file():
            target = seen.parent / f".deleted-{stamp}" / seen.name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                replace(seen, target)
            except OSError:
                pass


def _archived_id(graph_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{graph_id}.deleted-{stamp}"


def _write_marker(graph_id: str, archived_id: str, task_id: str | None = None,
                  kind: str = "delete") -> None:
    import logging
    from backend.features.persistence import write_json
    with _archiving_lock:
        _archiving.add(graph_id)
        _forward_map()[graph_id] = archived_id
    try:
        write_json(_refile_marker(graph_id), {
            "old_id": graph_id, "new_id": archived_id, "task_id": task_id,
            "kind": kind, "started_at": _utcnow()})
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "delete %s: could not record its pending archive: %s", graph_id, exc)


def _archive_graph_data(graph_id: str, archived_id: str | None = None,
                        background: bool = True, task_id: str | None = None) -> str:
    """Set a deleted workflow's data aside, so a new one of that name starts
    empty rather than inheriting its memory and history.

    Moved, never removed: each item goes to a `.deleted-<timestamp>` folder
    beside where it was, and its runs and batches are refiled under that
    archived id. Returns the archived id.
    """
    import logging
    import shutil

    if archived_id is None:
        archived_id = _archived_id(graph_id)
        _write_marker(graph_id, archived_id)
    stamp = archived_id.rsplit(".deleted-", 1)[1]
    # Its Chat Agents' conversations too, filed under the task id (or, for
    # older workflows, the graph id): a new workflow of that name must not
    # inherit them.
    try:
        from backend.features.agents import harness_api
        harness_api.archive_owner([task_id, graph_id], archived_id)
    except Exception as exc:  # noqa: BLE001 - the rest of the delete still goes ahead
        logging.getLogger(__name__).warning(
            "delete %s: could not archive its Chat Agents: %s", graph_id, exc)
    for item in _data_paths(graph_id):
        if not item.exists():
            continue
        target = item.parent / f".deleted-{stamp}" / item.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(target))
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "delete %s: could not archive %s: %s", graph_id, item, exc)
    # Refiling rewrites every run and batch the workflow ever had — on a
    # synced folder, minutes for a long history — so it happens after the
    # delete has answered. Until it is done the id counts as taken; a marker
    # on disk carries that, and the refile itself, across a restart.
    files = _history_files(graph_id)
    if files and background:
        threading.Thread(target=_refile, args=(graph_id, archived_id, files),
                         name=f"archive-{graph_id}", daemon=True).start()
    else:
        _refile(graph_id, archived_id, files)
    return archived_id


def check_shape(body: dict) -> None:
    """A body whose nodes or edges are not lists of objects is the client's
    mistake (422), not a crash deep inside migration (500)."""
    for key in ("tasks", "edges"):
        value = body.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise GraphValidationError([f"'{key}' must be a list of objects."])
    for key in ("name", "goal"):
        if body.get(key) is not None and not isinstance(body[key], str):
            raise GraphValidationError([f"'{key}' must be text."])


def chat_agent_tasks(tasks, edges):
    """The tasks with every Chat Agent node in its stored form (each with an
    agent_id of its own); refuses invalid settings and data edges touching
    one. See backend/features/agents/chat_agents.py."""
    from backend.features.agents import chat_agents
    errors = chat_agents.check_edges(tasks, edges)
    if errors:
        raise GraphValidationError(errors)
    try:
        return chat_agents.normalize_tasks(tasks)
    except ValueError as exc:
        raise GraphValidationError([str(exc)]) from None


def validate_harness_tasks(tasks):
    for task in tasks:
        config = task.get('harness')
        if config is not None:
            from backend.features.agents.graph_agent import ENGINES
            if not isinstance(config, dict) or config.get('engine') not in ENGINES:
                raise GraphValidationError(['Invalid Agent harness.'])
            for key, default, minimum, maximum in [('max_steps', 20, 1, 100), ('timeout', 300, 10, 1800)]:
                value = config.get(key, default)
                if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                    raise GraphValidationError([f'Harness {key} must be between {minimum} and {maximum}.'])


def save_graph(graph_id: str, body: dict) -> dict:
    """Persist a PUT body as the graph document.

    Renaming the workflow renames its identity with it, so the returned graph
    may carry a different id from the one in the URL — the caller follows it.
    """
    check_shape(body)
    existing = load_graph(graph_id) or {}
    validate_harness_tasks(body.get('tasks', []))
    tasks = chat_agent_tasks(body.get('tasks', []), body.get('edges', []))
    resources = body.get('memory_resources', existing.get('memory_resources', []))
    positions = body.get('memory_positions', existing.get('memory_positions', {}))
    if not isinstance(resources, list) or len(resources) > 200 or any(
        not isinstance(r, dict) or not isinstance(r.get('space_id'), str)
        or not re.fullmatch('[a-f0-9]{32}', r['space_id'])
        or not isinstance(r.get('name', ''), str) or len(r.get('name', '')) > 120
        for r in resources):
        raise GraphValidationError(['Invalid memory resource list.'])
    if len({r['space_id'] for r in resources}) != len(resources):
        raise GraphValidationError(['Each shared memory space appears only once on the canvas.'])
    if not isinstance(positions, dict) or len(positions) > 1000:
        raise GraphValidationError(['Invalid memory positions.'])
    points = list(positions.values()) + [{k: r[k] for k in ('x', 'y') if k in r} for r in resources]
    if any(not isinstance(point, dict) or any(
        key not in ('x', 'y') or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for key, value in point.items()) for point in points):
        raise GraphValidationError(['Memory positions must use finite x/y coordinates.'])
    # Every check before the rename: a rename moves the workflow's files, so a
    # save refused after it would leave them moved and the canvas unsaved.
    output_dir = (body.get("output_dir") or "runs").strip().strip("/") or "runs"
    if output_dir.startswith("..") or "/../" in f"/{output_dir}/":
        raise GraphValidationError(
            [f"output_dir {output_dir!r} must stay inside the workspace"]
        )
    from backend.api import workspace as workspace_mod
    reserved = workspace_mod.check_output_dir(output_dir)
    if reserved:
        raise GraphValidationError([reserved])
    new_name = body.get("name")
    if existing and new_name and new_name != existing.get("name"):
        # Do this first: everything below writes under the id, and the point of
        # a rename is that those writes land under the new one.
        renamed = rename_graph(graph_id, new_name)
        graph_id = renamed["id"]
        # The rename pins the Chat Agents' owner (task_id) to the old id; the
        # document read before it has not got it, and dropping it here left
        # the workflow's Chat Agents behind under the old id.
        existing = {**existing, "task_id": renamed.get("task_id")}
    graph = {
        "id": graph_id,
        "name": body.get("name", existing.get("name", graph_id)),
        "goal": body.get("goal", existing.get("goal", "")),
        "output_dir": output_dir,
        "project_id": body.get("project_id", existing.get("project_id")),
        "task_id": existing.get("task_id") or body.get("task_id"),
        # Name of a custom tool run over each input record before the workflow
        # sees it. Part of the workflow definition, so it travels with exports.
        "preprocess": (body.get("preprocess", existing.get("preprocess")) or None),
        "memory_resources": body.get("memory_resources", existing.get("memory_resources", [])),
        "memory_positions": body.get("memory_positions", existing.get("memory_positions", {})),
        # Which outputs go to human review (backend/features/execution/review.py).
        "review": review_rule(body.get("review", existing.get("review"))),
        # Evaluation lives in the Evaluate & Evolve panel, not on the canvas:
        # the workflow's evaluators are the user's Python code, kept here.
        # A client that does not send the list keeps the stored one.
        "evaluators": evaluator_list(body.get("evaluators", existing.get("evaluators", []))),
        "flow_version": body.get("flow_version", 0),
        "tasks": tasks,
        "edges": body.get("edges", []),
    }
    with _lock:
        _save(graph)
    return load_graph(graph_id)


def evaluator_list(evaluators):
    """Validate the workflow's evaluators, as review_rule does for review."""
    from backend.features.evaluation.evaluator_tools import validate_evaluators
    try:
        return validate_evaluators(evaluators)
    except Exception as exc:
        raise GraphValidationError([str(exc)]) from None


def set_evaluators(graph_id: str, evaluators) -> dict:
    """Replace a workflow's evaluators, leaving the canvas untouched."""
    validated = evaluator_list(evaluators)
    # Read and write under one lock: a canvas save landing in between would
    # otherwise be overwritten with the document as it was before it.
    with _lock:
        graph = load_graph(graph_id)
        if graph is None:
            raise GraphValidationError(["Workflow not found."])
        graph["evaluators"] = validated
        _save(graph)
    from backend.features.evaluation.evaluator_tools import clear_saved_drafts
    clear_saved_drafts(graph_id, graph["evaluators"])
    return load_graph(graph_id)


def review_rule(rule):
    if rule in (None, {}):
        return None
    from backend.features.execution.review import validate_rule
    try:
        return validate_rule(rule)
    except ValueError as exc:
        raise GraphValidationError([str(exc)]) from None


class GraphValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def is_source_task(task: dict) -> bool:
    """Canvas Input node (a dataset, DataLoader, API or project Input type) — not an LLM task."""
    return task.get("kind") == "source"


def is_tool_task(task: dict) -> bool:
    """Canvas tool node (kind="tool", deterministic, no LLM) — runs in Python
    before the framework graph, so it may only depend on source nodes, other
    tool nodes, or workflow inputs."""
    return task.get("kind") == "tool"


def validate_tool_tasks(tasks: list[dict]) -> None:
    """Static checks for canvas tool nodes: the tool must exist.

    A tool may feed on an LLM node's output now — the engine runs nodes in
    canvas order, whatever their kind — so the old "tools run first" rule
    and its refusal are gone.
    """
    from backend.api import tools_registry
    for task in tasks or []:
        if not is_tool_task(task):
            continue
        tool_name = task.get("tool")
        if not tool_name or tools_registry.find_tool(tool_name) is None:
            raise GraphValidationError(
                [f"Tool node '{task.get('name')}' references unknown or "
                 f"unavailable tool {tool_name!r}"]
            )

def parked_task_names(tasks: list[dict], edges: list[dict]) -> set:
    """Nodes switched off: `enabled: false`.

    Used to be inferred — any non-source node with no edges was a "parked"
    draft, but only once the canvas had an edge somewhere else, so adding an
    unrelated line could stop a node running. Whether a node takes part is
    now something the node says. Old graphs get the flag on first load
    (migrate_flow), from what the old rule would have decided.
    """
    # A Chat Agent never runs; runs see the workflow without it
    # (chat_agents.workflow_only), and this keeps it out wherever not.
    return {t.get("name") for t in tasks or []
            if t.get("enabled") is False or t.get("kind") == "chat_agent"}

def validate_source_tasks(tasks: list[dict]) -> None:
    """Static checks for canvas source nodes (known type, required config)."""
    from backend.api.source_apis import all_source_types

    known = all_source_types()
    for task in tasks or []:
        if not is_source_task(task):
            continue
        config = task.get("source") or {}
        schema = known.get(config.get("type"))
        if schema is None:
            raise GraphValidationError(
                [f"Source node '{task.get('name')}' has unknown or missing "
                 f"source type: {config.get('type')!r} "
                 f"(expected one of {sorted(known)})"]
            )
        if config.get('type') == 'dataloader':
            from backend.features.data.dataloaders import validate
            try:
                validate(config)
            except Exception as exc:
                raise GraphValidationError([str(exc)]) from exc
        for field in schema["config"]:
            if field.get("required") and not config.get(field["name"]):
                raise GraphValidationError(
                    [f"Source node '{task.get('name')}' requires "
                     f"'{field['name']}' in its source config"]
                )


# Canvas layout: one column per topological depth.
COL_WIDTH = 260
ROW_HEIGHT = 150


def auto_layout(graph: dict) -> None:
    """Place each task in the column of its longest dependency chain.

    Longest path rather than first-seen depth: a node fed by both a source and
    a three-deep chain belongs after the chain, not beside the source.
    """
    tasks = graph.get("tasks", []) or []
    edges = graph.get("edges", []) or []
    names = {t["name"] for t in tasks}
    incoming = {n: [] for n in names}
    for e in edges:
        if e.get("source") in names and e.get("target") in names:
            incoming[e["target"]].append(e["source"])

    depth: dict[str, int] = {}

    def resolve(name: str, seen: frozenset) -> int:
        if name in depth:
            return depth[name]
        if name in seen:  # a cycle cannot be laid out by depth; break it here
            return 0
        parents = incoming.get(name, [])
        d = 0 if not parents else 1 + max(resolve(p, seen | {name}) for p in parents)
        depth[name] = d
        return d

    for name in names:
        resolve(name, frozenset())

    per_column: dict[int, int] = {}
    for task in sorted(tasks, key=lambda t: (depth.get(t["name"], 0), t["name"])):
        d = depth.get(task["name"], 0)
        row = per_column.get(d, 0)
        per_column[d] = row + 1
        task["x"] = d * COL_WIDTH
        task["y"] = row * ROW_HEIGHT


def subgraph_from(tasks: list[dict], edges: list[dict],
                  start_at: list[str]) -> tuple[list[dict], list[dict]]:
    """The part of the graph reachable from `start_at`, those nodes included.

    Starting mid-pipeline means the upstream nodes simply are not part of this
    run: whatever they would have produced becomes an input the caller supplies
    (usually the output of an earlier run). Unknown names are ignored so a
    stale selection cannot make a run impossible to start.
    """
    if not start_at:
        return tasks, edges
    by_name = {t.get("name"): t for t in tasks}
    starts = [n for n in start_at if n in by_name]
    if not starts:
        raise GraphValidationError(
            [f"None of {start_at} is a node in this workflow"]
        )

    outgoing: dict[str, list[str]] = {}
    for edge in edges or []:
        outgoing.setdefault(edge.get("source"), []).append(edge.get("target"))

    reachable: set[str] = set()
    queue = list(starts)
    while queue:
        name = queue.pop()
        if name in reachable or name not in by_name:
            continue
        reachable.add(name)
        queue.extend(outgoing.get(name) or [])

    kept_tasks = [t for t in tasks if t.get("name") in reachable]
    kept_edges = [e for e in (edges or [])
                  if e.get("source") in reachable and e.get("target") in reachable]
    return kept_tasks, kept_edges


def validate_task_skills(tasks: list[dict]) -> None:
    """Reject references to skills that no longer exist."""
    from backend.api import skills_api
    try:
        skills_api.validate_skill_names(tasks)
    except skills_api.SkillError as e:
        raise GraphValidationError([str(e)])


def validate_task_memory(tasks: list[dict]) -> None:
    """Reject memory settings that would quietly keep nothing."""
    from backend.api import memory_policy
    siblings = {t.get("name"): t for t in tasks if t.get("name")}
    errors = [e for task in tasks for e in memory_policy.validate(task, siblings)]
    if errors:
        raise GraphValidationError(errors)


def strip_task(task: dict) -> dict:
    """Remove UI-only fields (x/y, skill_names, memory) from a canvas task.

    Parameters are filled to the shape the framework requires: the canvas
    always writes `type` and `description`, but a task that arrived another
    way (an import, a script) may name a field and nothing more, and the
    framework then fails on it in a way that reads as a bug in the node.
    """
    out = {k: task[k] for k in TASK_KEYS if k in task and task[k] is not None}
    for side in ("inputs", "outputs"):
        if side in out:
            out[side] = [{"type": "str", "description": p.get("name", ""), **p}
                         for p in out[side] if isinstance(p, dict)]
    return out


def topo_sort_tasks(tasks: list[dict], edges: list[dict]) -> list[dict]:
    """Order tasks by topological sort of the canvas edges.

    Ties are broken by the input order of the tasks list (Kahn's algorithm
    with the smallest original index first). Edges referencing unknown tasks
    or forming a cycle raise GraphValidationError.
    """
    index = {t.get("name"): i for i, t in enumerate(tasks)}
    if None in index:
        raise GraphValidationError(["Every task must have a name."])
    if len(index) != len(tasks):
        raise GraphValidationError(["Duplicate task names are not allowed."])

    in_degree = {name: 0 for name in index}
    children = {name: [] for name in index}
    errors = []
    seen = set()
    for edge in edges or []:
        src, tgt = edge.get("source"), edge.get("target")
        if src not in index:
            errors.append(f"Edge source '{src}' does not match any task name.")
            continue
        if tgt not in index:
            errors.append(f"Edge target '{tgt}' does not match any task name.")
            continue
        if (src, tgt) in seen:
            continue
        seen.add((src, tgt))
        children[src].append(tgt)
        in_degree[tgt] += 1
    if errors:
        raise GraphValidationError(errors)

    ready = sorted([n for n, d in in_degree.items() if d == 0], key=index.get)
    ordered = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for child in children[name]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
        ready.sort(key=index.get)

    if len(ordered) != len(tasks):
        remaining = [n for n in index if n not in ordered]
        raise GraphValidationError(
            [f"Edges contain a cycle involving tasks: {remaining}"]
        )

    by_name = {t["name"]: t for t in tasks}
    return [by_name[n] for n in ordered]


def compute_workflow_inputs(ordered_tasks: list[dict],
                            edges: list[dict] | None = None) -> list[dict]:
    """Task inputs no edge feeds, in topological order.

    Only a drawn edge feeds an input now. Two nodes that happen to share a
    field name are not connected by it, and a source wired to nothing feeds
    nothing — so both leave the field to be asked for.
    """
    if any(e.get("mappings") is None and not e.get("control_only") for e in edges or []):
        # Bare edges from before mappings existed: decide them the way load does.
        migrate_flow({"tasks": ordered_tasks, "edges": edges})
    parked = parked_task_names(ordered_tasks, edges)
    active = [t for t in ordered_tasks if t.get("name") not in parked]
    bindings = compile_bindings(active, [
        e for e in (edges or [])
        if e.get("source") not in parked and e.get("target") not in parked])
    workflow_inputs, seen = [], set()
    for task in active:
        if is_source_task(task):
            continue
        fed = bindings.get(task.get("name"), {})
        for inp in task.get("inputs", []) or []:
            name = inp.get("name")
            if name in fed or name in seen:
                continue
            seen.add(name)
            workflow_inputs.append({
                "name": name,
                "type": inp.get("type", "str"),
                "description": inp.get("description", ""),
                "required": inp.get("required", True),
                # The first task that needs it, so the run form can say what
                # each value is actually for.
                "consumed_by": task.get("name"),
            })
    # Fields a node's memory is keyed or dated by, when no node produces them:
    # they come with the data, so the workflow takes them as (optional) inputs.
    from backend.features.memory.bindings import binding_inputs
    for extra in binding_inputs(active):
        if extra["name"] not in seen:
            seen.add(extra["name"])
            workflow_inputs.append(extra)
    return workflow_inputs


def validate_graph(graph: dict, check_memory: bool = True) -> tuple[list[dict], list[dict]]:
    """Validate a canvas graph against the EvoAgentX framework.

    Source nodes (kind="source") and tool nodes (kind="tool") are excluded
    from the framework task list but keep their place in the topological
    order, so their outputs count as produced for workflow_inputs
    computation.

    Returns (ordered_tasks, workflow_inputs) on success; raises
    GraphValidationError with the framework's error messages on failure.
    """
    from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

    # A body from before edges carried mappings — an old client, an old
    # export — is migrated here the same way a stored graph is on load, so
    # the transition is one rule in one place. On a copy: validating must
    # not change what was handed in.
    check_shape(graph)
    graph = migrate_flow(copy.deepcopy(graph))
    # Chat Agents are checked as what they are, then left out: they take no
    # part in runs, so nothing below (framework, bindings, inputs) sees them.
    from backend.features.agents.chat_agents import workflow_only
    chat_agent_tasks(graph.get("tasks", []) or [], graph.get("edges", []) or [])
    graph = workflow_only(graph)
    tasks = graph.get("tasks", []) or []
    edges = graph.get("edges", []) or []
    evaluator_list(graph.get("evaluators"))
    validate_harness_tasks(tasks)
    validate_source_tasks(tasks)
    validate_tool_tasks(tasks)
    validate_task_skills(tasks)
    # Memory settings are checked when the canvas is saved, where they can
    # be fixed; a plan or an export is about structure and must not refuse
    # a workflow over a context field that arrives with the record.
    if check_memory:
        validate_task_memory(tasks)
    ordered = topo_sort_tasks(tasks, edges)
    parked = parked_task_names(tasks, edges)
    # Each LLM node is checked on its own — prompt against declared inputs,
    # the framework's own rules. Not as one framework graph: that also
    # demanded output names unique across nodes and inferred edges from
    # them, and the edges are the canvas's to decide now.
    for task in ordered:
        if is_source_task(task) or is_tool_task(task) or task["name"] in parked:
            continue
        try:
            SequentialWorkFlowGraph(goal=graph.get("goal", ""), tasks=[strip_task(task)])
        except GraphValidationError:
            raise
        except Exception as e:
            raise GraphValidationError([str(e)])
    compile_bindings([t for t in ordered if t["name"] not in parked],
                     [e for e in edges if e.get("source") not in parked
                      and e.get("target") not in parked])
    return ordered, compute_workflow_inputs(ordered, edges)


# ---------------------------------------------------------------------------
# Flow semantics (Phase 3): edges carry data, nodes say whether they take part
# ---------------------------------------------------------------------------

FLOW_VERSION = 3


def _fields(task: dict, side: str) -> list[str]:
    return [p.get("name") for p in (task.get(side) or []) if p.get("name")]


def lift_evaluators(graph: dict) -> dict:
    """Move evaluator nodes off the canvas into `graph["evaluators"]`.

    Evaluation is not part of the workflow graph any more: the node's
    `evaluator` configuration becomes the entry (its old `node` timing, which
    meant "as soon as the outputs wired to me are ready", becomes `run`), the
    node goes, and so does every edge that touched it. Idempotent: a graph
    with no evaluator node is left as it is, and an entry already in the list
    is never added twice.
    """
    tasks = graph.get("tasks") or []
    nodes = [t for t in tasks if t.get("kind") == "evaluator"]
    if not nodes:
        graph.setdefault("evaluators", graph.get("evaluators") or [])
        return graph
    from backend.features.evaluation.evaluator_tools import normalize
    lifted = list(graph.get("evaluators") or [])
    taken = {e.get("name") for e in lifted}
    names = set()
    for task in nodes:
        names.add(task.get("name"))
        if task.get("name") in taken:
            continue
        config = copy.deepcopy(task.get("evaluator") or {})
        timing = config.get("timing") or "run"
        entry = normalize({**config, "name": task.get("name"),
                           "description": task.get("description", ""),
                           "timing": "run" if timing == "node" else timing,
                           "enabled": task.get("enabled", True)})
        lifted.append(entry)
    graph["evaluators"] = lifted
    graph["tasks"] = [t for t in tasks if t.get("kind") != "evaluator"]
    graph["edges"] = [e for e in (graph.get("edges") or [])
                      if e.get("source") not in names and e.get("target") not in names]
    return graph


def migrate_flow(graph: dict) -> dict:
    """Give an old graph explicit edge mappings and `enabled` flags, once.

    Old edges were bare `{source, target}` and the framework decided what, if
    anything, crossed them by matching field names. That decision is now made
    here, on load, and written down:

    - exactly the same-name pairs between the two nodes become the mappings;
    - an edge with nothing in common becomes control-only, with a warning;
    - nodes the old parked rule would have left out become `enabled: false`.

    Version 3 takes evaluation off the canvas: an `evaluator` node becomes an
    entry in `graph["evaluators"]` and its node and edges are dropped.

    A graph already at FLOW_VERSION is returned untouched.
    """
    if not isinstance(graph, dict) or graph.get("flow_version", 0) >= FLOW_VERSION:
        return graph
    version = graph.get("flow_version", 0)
    lift_evaluators(graph)
    if version >= 2:
        graph["flow_version"] = FLOW_VERSION
        return graph
    tasks = graph.get("tasks") or []
    edges = graph.get("edges") or []
    by_name = {t.get("name"): t for t in tasks}
    warnings = []

    # What the old rule parked: non-source nodes with no edges, provided the
    # canvas had an edge somewhere.
    connected = {e.get("source") for e in edges} | {e.get("target") for e in edges}
    for task in tasks:
        if "enabled" in task:
            continue
        drafted = (connected and not is_source_task(task)
                   and task.get("name") not in connected)
        task["enabled"] = not drafted

    for edge in edges:
        if edge.get("mappings") is not None or edge.get("control_only"):
            continue
        src, dst = by_name.get(edge.get("source")), by_name.get(edge.get("target"))
        if src is None or dst is None:
            continue
        shared = [f for f in _fields(src, "outputs") if f in _fields(dst, "inputs")]
        if shared:
            edge["mappings"] = [{"from": f, "to": f} for f in shared]
        else:
            edge["control_only"] = True
            warnings.append(
                f"Edge {edge.get('source')} → {edge.get('target')} carried no "
                "field (no output matches an input); it is now control-only and just orders the two.")
    # What the old engine did without an edge: every node could read any
    # earlier node's output by name. Write each such read down as an edge, so
    # the run keeps meaning what it meant. One earlier producer: an edge with
    # that mapping. Several: nobody can say which was meant — flagged, and
    # the plan asks for the value instead of guessing.
    try:
        order = [t.get("name") for t in topo_sort_tasks(tasks, edges)]
    except GraphValidationError:
        order = [t.get("name") for t in tasks]
    enabled = [n for n in order if by_name.get(n, {}).get("enabled", True) is not False]
    fed = compile_bindings([by_name[n] for n in enabled],
                           [e for e in edges if e.get("source") in enabled
                            and e.get("target") in enabled])
    added: dict[tuple, dict] = {}
    for position, name in enumerate(enabled):
        task = by_name[name]
        if is_source_task(task):
            continue
        for field in _fields(task, "inputs"):
            if field in fed.get(name, {}):
                continue
            producers = [earlier for earlier in enabled[:position]
                         if field in _fields(by_name[earlier], "outputs")
                         and (not is_source_task(by_name[earlier]) or earlier in connected)]
            if len(producers) == 1:
                key = (producers[0], name)
                # `implied`: drawn by the migration, not by a person. The canvas
                # renders these quietly; they are real dependencies all the same.
                added.setdefault(key, {"source": producers[0], "target": name,
                                       "mappings": [], "implied": True})
                added[key]["mappings"].append({"from": field, "to": field})
            elif len(producers) > 1:
                warnings.append(
                    f"'{name}.{field}' used to be read from whichever of {producers} "
                    "ran last; that cannot be known. It is now asked for as an input — "
                    "draw the edge you mean.")
    for key, new_edge in added.items():
        existing = next((e for e in edges if (e.get("source"), e.get("target")) == key), None)
        if existing is None:
            edges.append(new_edge)
            warnings.append(
                f"Added edge {key[0]} → {key[1]} carrying "
                f"{[m['from'] for m in new_edge['mappings']]}: the old engine passed "
                "these by name without a line on the canvas.")
        else:
            existing.pop("control_only", None)
            existing["mappings"] = (existing.get("mappings") or []) + new_edge["mappings"]
    graph["edges"] = edges
    graph["flow_version"] = FLOW_VERSION
    if warnings:
        graph["migration_warnings"] = warnings
    return graph


def compile_bindings(tasks: list[dict], edges: list[dict]) -> dict:
    """What each node's inputs are fed by, from the edges alone.

    Returns {node: {input: {"node": producer, "field": output}}}. Raises
    GraphValidationError for an edge that carries no mapping and is not
    control-only, a mapping naming a field that does not exist, or an input
    with two producers — the run would have to guess, and guessing was the
    bug.
    """
    by_name = {t.get("name"): t for t in tasks or []}
    bindings: dict[str, dict] = {t.get("name"): {} for t in tasks or []}
    errors = []
    for edge in edges or []:
        src, dst = edge.get("source"), edge.get("target")
        if src not in by_name or dst not in by_name:
            continue
        if edge.get("control_only"):
            continue
        mappings = edge.get("mappings")
        if not mappings:
            errors.append(f"Edge {src} → {dst} has no field mapping. Map an "
                          "output to an input, or mark it control-only.")
            continue
        outs = _fields(by_name[src], "outputs")
        ins = _fields(by_name[dst], "inputs")
        for m in mappings:
            frm, to = m.get("from"), m.get("to")
            if frm not in outs:
                errors.append(f"Edge {src} → {dst} maps '{frm}', which '{src}' "
                              f"does not produce (it produces {outs or 'nothing'}).")
                continue
            if to not in ins:
                errors.append(f"Edge {src} → {dst} maps to '{to}', which '{dst}' "
                              f"does not take (it takes {ins or 'nothing'}).")
                continue
            held = bindings[dst].get(to)
            if held and (held["node"], held["field"]) != (src, frm):
                errors.append(f"'{dst}.{to}' has two producers: "
                              f"{held['node']}.{held['field']} and {src}.{frm}.")
                continue
            bindings[dst][to] = {"node": src, "field": frm}
    if errors:
        raise GraphValidationError(errors)
    return bindings
