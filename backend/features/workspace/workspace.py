"""The workspace: a workflow's project on disk.

Under studio-data/workspace/<graph_id>/ sits the project itself — the same
thing `Export` hands to someone else, whole, so that what you can browse here
is what you could actually deploy:

- workflow.py, run.py       — the runnable code, rewritten from the canvas
  graph.json, manifest.json   whenever the workflow is saved. The canvas is
  README.md, requirements.txt  the source; this is what it compiles to.
  .env.example
- tools/, skills/           — the custom toolkits and skills it uses
- data/sample_input.json    — a filled-in example of what it takes
- vendor/evoagentx, llm,    — the framework and the provider/memory layers as
  memory, and whatever        they exist here, local changes included. The same
  repo modules its tools      for every workflow and changing only when this
  reach                       repo does, so it is stamped and left alone rather
                              than rewritten on every save.

and everything its runs produce:

- runs/<run_id>/input.json        — the effective inputs, source records merged
- runs/<started-at>/nodes/<name>.jsonl — one file per node of that run or batch
- runs/<run_id>/output.json       — the result, plus every node's full output
- files/                          — working dir for storage-backed toolkits
  (StorageToolkit / CMDToolkit / PythonInterpreterToolkit are pointed here via
  LocalStorageHandler; FileToolkit paths come from the LLM and are not
  redirectable)

Only new runs write artifacts; historical runs are untouched.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from backend.api.studio_config import data_path

_REPO_ROOT = Path(__file__).resolve().parents[3]

WORKSPACE_DIR = data_path("workspace")
MAX_READ_BYTES = 100 * 1024


def workspace_root(graph_id: str) -> Path:
    return WORKSPACE_DIR / graph_id


def files_dir(graph_id: str) -> Path:
    return workspace_root(graph_id) / "files"


# What the project is made of, as opposed to what its runs produce. Rewritten
# from the canvas on every save, so anything left over from an earlier shape of
# the workflow — a tool it no longer uses — is cleared out first. Only what a
# generation wrote is cleared: these folders are shared with the user's own
# files (an uploaded data/customers.csv), which a save must never take.
PROJECT_DIRS = ("tools", "skills", "data")
# The files the last generation wrote, so the next one removes exactly those.
GENERATED_MANIFEST = ".generated-files"

# The framework and the layers under it: 262 files, 3.8 MB, identical for every
# workflow and changing only when this repo does. Rewriting that on every save
# would be 3.8 MB of churn to produce the same bytes, so it is stamped with a
# fingerprint of its source and left alone while that matches.
VENDOR_DIR = "vendor"
VENDOR_STAMP = ".vendor-stamp"

# What the workflow has learned. Not files on disk — a memory store is a vector
# index in a database — so this is a read-through view of the live store rather
# than a copy, which would go stale the moment the next run wrote to it.
MEMORY_DIR_NAME = "memory"
# The two kinds, named rather than merged: long-term is a vector store searched
# by similarity across every run; short-term is a log of one session, read back
# in order. They answer different questions and are worth telling apart.
LONG_TERM = "long-term"
TABLE = "table"
SHORT_TERM = "short-term"


def write_project(graph: dict) -> list[str]:
    """Compile the canvas into the workspace as the whole runnable project.

    Everything `Export` hands over lives here, the vendored framework
    included: the workspace is the project, so it has to be the thing you
    could actually deploy, not a summary of it.

    Returns the paths written. Best effort: a workflow that cannot be compiled
    yet (half-built, a tool mid-edit) must not block saving it, so the failure
    is reported to the caller rather than raised.
    """
    import shutil

    from backend.api import export_api
    graph_id = graph.get("id") or "graph"
    root = workspace_root(graph_id)
    root.mkdir(parents=True, exist_ok=True)

    fingerprint = export_api.vendor_fingerprint()
    stamp = root / VENDOR_STAMP
    vendor_current = (
        (root / VENDOR_DIR).is_dir()
        and stamp.is_file()
        and stamp.read_text(encoding="utf-8").strip() == fingerprint
    )

    if graph.get('evaluators') or any((t.get('source') or {}).get('type') == 'dataloader' for t in graph.get('tasks', [])):
        # Platform resources are referenced by identity. Keep an inspectable
        # graph artifact, but no stale Python runner that would skip evaluators.
        files = {'workflow.json': json.dumps(graph, ensure_ascii=False, indent=2),
                 'README.md': '# Studio workflow\n\nThis workflow uses DataLoaders or Evaluators. Run it in Studio. Resource IDs refer to uploaded runtime data; graph JSON does not contain those files.\n'}
        vendor = {}
    else:
        files, vendor = export_api.project_files(graph, include_vendor=not vendor_current)

    # Only files the previous generation wrote and this one does not: the
    # workspace also holds the user's uploads and notes, and run artifacts.
    # A workspace from before the manifest existed loses nothing.
    for rel in sorted(_generated_files(root) - set(files)):
        try:
            stale = _resolve_in_workspace(graph_id, rel)
        except WorkspaceError:
            continue
        if stale.is_file():
            stale.unlink(missing_ok=True)
            _prune_empty_parents(stale.parent, root)
    if not vendor_current:
        shutil.rmtree(root / VENDOR_DIR, ignore_errors=True)

    written = []
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(rel)
    for rel, blob in vendor.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        written.append(rel)
    if not vendor_current:
        stamp.write_text(fingerprint, encoding="utf-8")
    (root / GENERATED_MANIFEST).write_text(
        json.dumps(sorted(files), ensure_ascii=False, indent=0), encoding="utf-8")
    return sorted(written)


def _generated_files(root: Path) -> set[str]:
    """What the last generation wrote outside vendor/, per its manifest."""
    try:
        listed = json.loads((root / GENERATED_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {p for p in listed if isinstance(p, str)} if isinstance(listed, list) else set()


def _prune_empty_parents(folder: Path, root: Path) -> None:
    """Remove folders a stale generated file leaves empty, up to the root."""
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def check_output_dir(output_dir: str) -> str | None:
    """Why run artifacts cannot go to `output_dir`, or None.

    Generated folders are rewritten from the canvas on every save; run
    output sharing one would mix what a run produced with what a save may
    replace.
    """
    first = (output_dir or "").strip().strip("/").split("/")[0]
    if first in set(PROJECT_DIRS) | {VENDOR_DIR, MEMORY_DIR_NAME}:
        return (f"output_dir {output_dir!r} is inside '{first}/', which the "
                "workspace keeps for the generated project. Use another folder, "
                "e.g. 'runs'.")
    return None


def _safe_name(name: str) -> str:
    """A node's name as a filename, without letting it escape the folder."""
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(name or ""))
    return cleaned.strip("-") or "node"


def _session_stamp(state: dict) -> str:
    """The folder a run's lines go under: when the user pressed Run.

    Every record of a batch shares its batch's start; a single run has its
    own. Wall-clock, local, to the second, so the folders sort as they
    happened and read as times rather than ids.
    """
    raw = state.get("session_started_at") or state.get("created_at") or ""
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if when.tzinfo is not None:
            when = when.astimezone()
        return when.strftime("%Y%m%d-%H%M%S")
    except ValueError:
        return datetime.now().strftime("%Y%m%d-%H%M%S")


def write_run_artifacts(graph_id: str, state: dict, output_dir: str = "runs") -> None:
    """Append this run to its session's per-node files, then to its run log.

    Each run, and each batch as a whole, gets a folder named by when it
    started — `<output_dir>/<YYYYmmdd-HHMMSS>/` — and inside it every node has
    one file, `nodes/<node>.jsonl`, that each record of that session appends a
    line to: what the node was handed, what it produced, which run and batch
    it was. A batch of a hundred records is one folder, and its `decide.jsonl`
    reads as every decision of that batch in order. The run-level summary goes
    to `runs.jsonl` beside `nodes/`.

    JSON lines rather than one JSON array: appending to an array means
    rewriting the whole file every run. Nodes whose task sets
    save_output=false are recorded with their output omitted. Best effort: a
    run never fails because its trace could not be written.
    """
    try:
        save_flags = state.get("_save_output_flags") or {}
        base = _resolve_in_workspace(graph_id, f"{output_dir}/{_session_stamp(state)}")
        (base / "nodes").mkdir(parents=True, exist_ok=True)
        node_io = state.get("_node_io") or {}
        source_records = state.get("_source_records") or {}
        now = datetime.now(timezone.utc).isoformat()
        # `session` is when this run or batch started — the folder's name,
        # repeated on every line so a line still says where it came from.
        common = {"run_id": state["run_id"], "graph_id": graph_id,
                  "batch_id": state.get("batch_id"), "session": _session_stamp(state),
                  "at": now}
        for position, node in enumerate(state.get("nodes", []) or [], start=1):
            name = node.get("name")
            io = node_io.get(name) or {}
            output = io.get("output", node.get("output"))
            if name in source_records:            # source nodes: the full record
                output = source_records[name]
            if save_flags.get(name) is False:
                output = None                     # node opted out of persistence
            line = {**common, "position": position, "node": name,
                    "status": node.get("status"), "inputs": io.get("inputs") or {},
                    "output": output}
            with open(base / "nodes" / f"{_safe_name(name)}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        summary = {**common, "status": state.get("status"),
                   "plan_id": state.get("plan_id"), "graph_revision": state.get("graph_revision"),
                   "inputs": state.get("input_summary") or {},
                   "error": state.get("error"), "result": state.get("result"),
                   "created_at": state.get("created_at")}
        with open(base / "runs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _memory_entries(graph_id: str) -> list[dict]:
    """The graph's long-term memory, as a folder of readable files.

    One folder per node that remembers, one file per thing it remembered,
    newest first — so what a workflow has learned sits beside its code and its
    runs instead of behind a tab in the run drawer.
    """
    from backend.api import memory_store
    listing: list[dict] = []
    try:
        agents = memory_store.list_agents(graph_id)
    except Exception:
        return listing
    for agent in agents:
        listing.append({"path": f"{MEMORY_DIR_NAME}/{LONG_TERM}/{agent}", "dir": True})
        entries = _ordered_memory(graph_id, agent)
        for name, entry in zip(_memory_names(entries), entries):
            body = _memory_text(entry)
            listing.append({
                "path": f"{MEMORY_DIR_NAME}/{LONG_TERM}/{agent}/{name}",
                "size": len(body.encode("utf-8")),
                "mtime": str(entry.get("timestamp") or ""),
            })
    if listing:
        listing.insert(0, {"path": f"{MEMORY_DIR_NAME}/{LONG_TERM}", "dir": True})

    listing += _table_entries(graph_id)
    listing += _session_entries(graph_id)
    if listing:
        listing.insert(0, {"path": MEMORY_DIR_NAME, "dir": True})
    return listing


def _session_entries(graph_id: str) -> list[dict]:
    """The short-term log, one folder per session and one file per entry.

    In the order things happened, which is the whole point of it — so these are
    numbered forwards, unlike the long-term store where newest-first is what
    you want.
    """
    from backend.api import stm_store
    listing: list[dict] = []
    try:
        sessions = stm_store.sessions(graph_id)
    except Exception:
        return listing
    for session in sessions:
        safe = _safe_name(session)
        listing.append({"path": f"{MEMORY_DIR_NAME}/{SHORT_TERM}/{safe}", "dir": True})
        for position, entry in enumerate(
                stm_store.recent(graph_id, session, stm_store.MAX_PER_SESSION), start=1):
            body = json.dumps(entry, indent=2, ensure_ascii=False, default=str)
            listing.append({
                "path": f"{MEMORY_DIR_NAME}/{SHORT_TERM}/{safe}/{position:03d}.json",
                "size": len(body.encode("utf-8")),
                "mtime": str(entry.get("at") or ""),
            })
    if listing:
        listing.insert(0, {"path": f"{MEMORY_DIR_NAME}/{SHORT_TERM}", "dir": True})
    return listing


def _ordered_memory(graph_id: str, agent: str) -> list[dict]:
    """One node's memory, newest first.

    The position in this list is the entry's filename, so the listing and the
    reader have to agree on it — hence one function rather than the same sort
    written twice.
    """
    from backend.api import memory_store
    try:
        entries = memory_store.list_entries(graph_id, agent)
    except Exception:
        return []
    return sorted(entries, key=lambda e: str(e.get("timestamp") or ""), reverse=True)


def _memory_text(entry: dict) -> str:
    """One memory entry, as something worth opening.

    The stored content is JSON that has usually been encoded a second time on
    the way into the store; left as it comes out it is a wall of escaped
    quotes, so it is unwrapped here.
    """
    from backend.api import memory_policy
    readable = dict(entry)
    content = entry.get("content")
    for _ in range(3):
        if not isinstance(content, str):
            break
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            break
    readable["content"] = content
    readable["summary"] = memory_policy.render(entry.get("content"), limit=10_000)
    return json.dumps(readable, indent=2, ensure_ascii=False, default=str)


def tree(graph_id: str) -> list[dict]:
    """Relative file listing (path, size, mtime) of the graph's workspace.

    Memory is folded in as a virtual folder: it is part of what this workflow
    is, but it lives in a vector store rather than as files.
    """
    root = workspace_root(graph_id)
    from . import dataset_mounts
    entries = _memory_entries(graph_id) + dataset_mounts.entries(graph_id)
    if not root.is_dir():
        return entries
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        # Bookkeeping, not part of the project: it says which version of the
        # framework is sitting in vendor/, and means nothing to a reader.
        if rel in (VENDOR_STAMP, GENERATED_MANIFEST) or "__pycache__" in rel:
            continue
        if path.is_dir():
            entries.append({"path": rel, "dir": True, "absolute_path": str(path.resolve())})
            continue
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append({
            "path": rel, "absolute_path": str(path.resolve()),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return entries


class WorkspaceError(Exception):
    """path traversal / not found (mapped to HTTP 400/404)."""
    def __init__(self, message, not_found=False):
        super().__init__(message)
        self.not_found = not_found


def _resolve_in_workspace(graph_id: str, relpath: str) -> Path:
    """Resolve relpath inside the graph workspace; rejects traversal."""
    root = workspace_root(graph_id).resolve()
    target = (root / relpath).resolve()
    if not str(target).startswith(str(root) + "/") and target != root:
        raise WorkspaceError(f"Path escapes the workspace: {relpath!r}")
    return target


def _table_entries(graph_id: str) -> list[dict]:
    """The node tables, one folder per node and one file per subject.

    Named by what it is about — `bitcoin-depot.json`, not `007.json`, which
    said only how recently it happened to be written. Being able to open a
    company and see its whole record is the thing this was missing.
    """
    from backend.api import table_store
    listing: list[dict] = []
    try:
        nodes = table_store.nodes(graph_id)
    except Exception:
        return listing
    for node in nodes:
        listing.append({"path": f"{MEMORY_DIR_NAME}/{TABLE}/{node}", "dir": True})
        for subject in table_store.subjects(graph_id, node):
            body = _table_text(graph_id, node, subject)
            listing.append({
                "path": f"{MEMORY_DIR_NAME}/{TABLE}/{node}/{_safe_name(subject)}.json",
                "size": len(body.encode("utf-8")),
                "mtime": "",
            })
    if listing:
        listing.insert(0, {"path": f"{MEMORY_DIR_NAME}/{TABLE}", "dir": True})
    return listing


def _table_text(graph_id: str, node: str, subject: str) -> str:
    """One subject's whole record, oldest first."""
    from backend.api import table_store
    rows = table_store.rows(graph_id, node, subject)
    return json.dumps({"subject": subject, "node": node,
                       "rows": len(rows), "record": rows},
                      indent=2, ensure_ascii=False, default=str)


def _table_file(graph_id: str, relpath: str) -> dict | None:
    """A table row-set addressed as a path, or None if that is not one."""
    from backend.api import table_store
    parts = relpath.split("/")
    if (len(parts) != 4 or parts[0] != MEMORY_DIR_NAME or parts[1] != TABLE
            or not parts[3].endswith(".json")):
        return None
    stem = parts[3][:-len(".json")]
    for subject in table_store.subjects(graph_id, parts[2]):
        if _safe_name(subject) == stem:
            body = _table_text(graph_id, parts[2], subject)
            return {"path": relpath, "size": len(body.encode("utf-8")),
                    "truncated": False, "content": body, "readonly": True}
    raise WorkspaceError(f"No such memory entry: {relpath!r}", not_found=True)


def _memory_names(entries: list[dict]) -> list[str]:
    """Filenames for one node's memory, in the order given.

    A node that tracks a subject keeps one entry per subject, so the subject's
    name is what makes the file findable — `sleep-number.json` rather than
    `007.json`, which said only how recently it happened to be written. Kept
    in one function because the listing and the reader must agree on it.
    """
    from backend.api import memory_policy
    names, used = [], {}
    for position, entry in enumerate(entries, start=1):
        body = memory_policy._unwrap(entry.get("content"))
        subject = body.get("subject") if isinstance(body, dict) else None
        stem = ""
        if isinstance(subject, dict):
            value = next((v for v in subject.values() if v), None)
            stem = _safe_name(str(value)) if value else ""
        if not stem:
            stem = f"{position:03d}"
        # Two subjects can reduce to one filename; the first keeps it.
        used[stem] = used.get(stem, 0) + 1
        names.append(f"{stem}.json" if used[stem] == 1
                     else f"{stem}-{used[stem]}.json")
    return names


def _memory_file(graph_id: str, relpath: str) -> dict | None:
    """A memory entry addressed as a path, or None if that is not one."""
    found = _table_file(graph_id, relpath)
    if found is not None:
        return found
    parts = relpath.split("/")
    if (parts[0] != MEMORY_DIR_NAME or len(parts) != 4
            or parts[1] not in (LONG_TERM, SHORT_TERM)
            or not parts[3].endswith(".json")):
        return None
    if parts[1] == LONG_TERM:
        entries = _ordered_memory(graph_id, parts[2])
        names = _memory_names(entries)
        if parts[3] in names:
            body = _memory_text(entries[names.index(parts[3])])
            return {"path": relpath, "size": len(body.encode("utf-8")),
                    "truncated": False, "content": body, "readonly": True}
        raise WorkspaceError(f"No such memory entry: {relpath!r}", not_found=True)

    # The session log stays numbered: it is a sequence of things that happened,
    # and the order is the only name a line of it has.
    from backend.api import stm_store
    try:
        position = int(parts[3][:-len(".json")])
    except ValueError:
        position = 0
    entries = [e for s in stm_store.sessions(graph_id) if _safe_name(s) == parts[2]
               for e in stm_store.recent(graph_id, s, stm_store.MAX_PER_SESSION)]
    if not 1 <= position <= len(entries):
        raise WorkspaceError(f"No such memory entry: {relpath!r}", not_found=True)
    body = json.dumps(entries[position - 1], indent=2, ensure_ascii=False, default=str)
    return {"path": relpath, "size": len(body.encode("utf-8")),
            "truncated": False, "content": body, "readonly": True}


def read_file(graph_id: str, relpath: str) -> dict:
    """Read a workspace file as text (truncated past 100KB)."""
    remembered = _memory_file(graph_id, relpath)
    if remembered is not None:
        return remembered
    from . import dataset_mounts
    mounted = dataset_mounts.resolve(graph_id, relpath)
    target = mounted or _resolve_in_workspace(graph_id, relpath)
    if not target.is_file():
        raise WorkspaceError(f"No such workspace file: {relpath!r}", not_found=True)
    with target.open("rb") as stream:
        raw = stream.read(MAX_READ_BYTES + 1)
    truncated = len(raw) > MAX_READ_BYTES
    import codecs
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(raw[:MAX_READ_BYTES], final=not truncated)
        binary = "\x00" in text
    except UnicodeDecodeError:
        text, binary = "", True
    return {"path": relpath, "size": target.stat().st_size, "binary": binary,
            "truncated": truncated, "content": "" if binary else text, "readonly": mounted is not None, "absolute_path":str(target.resolve())}


MAX_WRITE_BYTES = 10 * 1024 * 1024


def _refuse_if_memory(relpath: str, verb: str) -> None:
    """Memory is a view of a vector store, not a folder of files."""
    from . import dataset_mounts
    dataset_mounts.refuse(relpath)
    if relpath == MEMORY_DIR_NAME or relpath.startswith(f"{MEMORY_DIR_NAME}/"):
        raise WorkspaceError(
            f"{relpath!r} is a view of this workflow's long-term memory, not a "
            f"file — there is nothing to {verb}. Turn memory off on a node, or "
            "change what it keeps, in the Inspector."
        )


def write_file(graph_id: str, relpath: str, content: bytes) -> dict:
    """Create/overwrite a workspace file (max 10MB), creating parent dirs."""
    _refuse_if_memory(relpath, "write")
    if not relpath or relpath.endswith("/"):
        raise WorkspaceError(f"Invalid file path: {relpath!r}")
    if len(content) > MAX_WRITE_BYTES:
        raise WorkspaceError(f"File too large: {len(content)} bytes (max {MAX_WRITE_BYTES})")
    target = _resolve_in_workspace(graph_id, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {"path": relpath, "size": target.stat().st_size}


def upload_file(graph_id: str, relpath: str, stream) -> dict:
    """Copy uploads in bounded chunks; publish only complete files, without overwriting."""
    import os
    import shutil
    import tempfile

    _refuse_if_memory(relpath, "write")
    if not relpath or relpath.endswith("/"):
        raise WorkspaceError(f"Invalid file path: {relpath!r}")
    target = _resolve_in_workspace(graph_id, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".upload-", delete=False) as out:
            temp = Path(out.name)
            shutil.copyfileobj(stream, out, length=1024 * 1024)
        try:
            os.link(temp, target)
        except FileExistsError:
            raise WorkspaceError(f"{relpath!r} already exists. Choose another path or delete the existing file first.")
        return {"path": relpath, "size": target.stat().st_size, "absolute_path": str(target)}
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def make_dir(graph_id: str, relpath: str) -> dict:
    """Create a workspace directory (parents included)."""
    _refuse_if_memory(relpath, "create")
    if not relpath or relpath.endswith("/"):
        raise WorkspaceError(f"Invalid directory path: {relpath!r}")
    target = _resolve_in_workspace(graph_id, relpath)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": relpath}


def delete_file(graph_id: str, relpath: str, recursive: bool = False) -> dict:
    """Delete a workspace file, or a directory.

    A directory with anything in it is only removed when the caller has said
    so: a right-click on `runs/` is one slip away from every run this workflow
    ever produced. Returns what went, so the caller can say.
    """
    import shutil

    _refuse_if_memory(relpath, "delete")
    target = _resolve_in_workspace(graph_id, relpath)
    if target.is_dir():
        contents = [p for p in target.rglob("*") if p.is_file()]
        if contents and not recursive:
            raise WorkspaceError(
                f"{relpath!r} holds {len(contents)} file(s). Deleting it removes "
                "them too."
            )
        shutil.rmtree(target)
        return {"deleted": relpath, "files": len(contents), "directory": True}
    if not target.is_file():
        raise WorkspaceError(f"No such workspace file: {relpath!r}", not_found=True)
    target.unlink()
    return {"deleted": relpath, "files": 1, "directory": False}


def _download_memory(graph_id: str, relpath: str) -> tuple[str, str, bytes]:
    """A memory folder, zipped from the live store."""
    import io
    import zipfile

    wanted = relpath.split("/")
    inside = [e for e in _memory_entries(graph_id)
              if not e.get("dir") and e["path"].startswith(f"{relpath}/")]
    if not inside:
        raise WorkspaceError(f"No such memory path: {relpath!r}", not_found=True)
    name = wanted[-1]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in inside:
            body = read_file(graph_id, entry["path"])["content"]
            archive.writestr(f"{name}/{entry['path'][len(relpath) + 1:]}", body)
    return f"{name}.zip", "application/zip", buffer.getvalue()


def download(graph_id: str, relpath: str = "") -> tuple[str, str, bytes]:
    """(filename, media type, bytes) for a workspace path.

    A file comes back whole — unlike reading it for the editor, which stops at
    100KB because that is for looking at, not for keeping. A directory, or the
    workspace itself, comes back zipped: a run is a folder of files and it is
    the run you want, not each file in turn.
    """
    import io
    import mimetypes
    import zipfile

    remembered = _memory_file(graph_id, relpath)
    if remembered is not None:
        return (relpath.replace("/", "-"), "application/json",
                remembered["content"].encode("utf-8"))
    if relpath == MEMORY_DIR_NAME or relpath.startswith(f"{MEMORY_DIR_NAME}/"):
        return _download_memory(graph_id, relpath)

    root = workspace_root(graph_id)
    from . import dataset_mounts
    target = dataset_mounts.resolve(graph_id, relpath) or (_resolve_in_workspace(graph_id, relpath) if relpath else root)
    if target.is_file():
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return target.name, media, target.read_bytes()
    if not target.is_dir():
        raise WorkspaceError(f"No such workspace path: {relpath!r}", not_found=True)

    name = target.name if target != root else graph_id
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                archive.write(path, f"{name}/{path.relative_to(target)}")
    return f"{name}.zip", "application/zip", buffer.getvalue()
