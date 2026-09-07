"""The workspace: a workflow's project on disk.

Under studio/data/workspace/<graph_id>/ sits the project itself — the same
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
- runs/<run_id>/nodes/NN-<name>.json — one file per node, in execution order
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

_REPO_ROOT = Path(__file__).resolve().parents[2]

WORKSPACE_DIR = _REPO_ROOT / "studio" / "data" / "workspace"
MAX_READ_BYTES = 100 * 1024


def workspace_root(graph_id: str) -> Path:
    return WORKSPACE_DIR / graph_id


def files_dir(graph_id: str) -> Path:
    return workspace_root(graph_id) / "files"


# What the project is made of, as opposed to what its runs produce. Rewritten
# from the canvas on every save, so anything left over from an earlier shape of
# the workflow — a tool it no longer uses — is cleared out first.
PROJECT_DIRS = ("tools", "skills", "data")

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

    import export_api

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

    files, vendor = export_api.project_files(graph, include_vendor=not vendor_current)

    for name in PROJECT_DIRS:
        shutil.rmtree(root / name, ignore_errors=True)
    keep = set(files) | {VENDOR_STAMP}
    for stale in root.glob("*"):
        if stale.is_file() and stale.name not in keep:
            stale.unlink(missing_ok=True)
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
    return sorted(written)


def _safe_name(name: str) -> str:
    """A node's name as a filename, without letting it escape the folder."""
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(name or ""))
    return cleaned.strip("-") or "node"


def write_run_artifacts(graph_id: str, state: dict, output_dir: str = "runs") -> None:
    """Write input.json + output.json for a finished run (best effort).

    output_dir is the per-graph run-artifact folder under the workspace
    (default "runs"). Nodes whose task sets save_output=false are recorded
    with their output omitted.
    """
    try:
        save_flags = state.get("_save_output_flags") or {}
        run_dir = _resolve_in_workspace(graph_id, f"{output_dir}/{state['run_id']}")
        run_dir.mkdir(parents=True, exist_ok=True)
        with open(run_dir / "input.json", "w", encoding="utf-8") as f:
            json.dump(state.get("_effective_inputs", state.get("inputs") or {}),
                      f, indent=2, ensure_ascii=False, default=str)
        nodes = []
        source_records = state.get("_source_records") or {}
        for node in state.get("nodes", []) or []:
            output = node.get("output")
            if node.get("name") in source_records:  # source nodes: full record
                output = source_records[node["name"]]
            if save_flags.get(node.get("name")) is False:
                output = None  # node opted out of workspace persistence
            nodes.append({"name": node.get("name"), "status": node.get("status"),
                          "output": output})
        # One file per node, in execution order. The same outputs are inside
        # output.json, but a run of eight nodes reads there as one blob; here
        # each step is a thing you can open, diff against another run, or hand
        # to someone.
        nodes_dir = run_dir / "nodes"
        nodes_dir.mkdir(parents=True, exist_ok=True)
        for existing in nodes_dir.glob("*.json"):
            existing.unlink(missing_ok=True)
        for position, node in enumerate(nodes, start=1):
            path = nodes_dir / f"{position:02d}-{_safe_name(node['name'])}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(node, f, indent=2, ensure_ascii=False, default=str)

        with open(run_dir / "output.json", "w", encoding="utf-8") as f:
            json.dump({
                "run_id": state["run_id"],
                "graph_id": graph_id,
                "status": state.get("status"),
                "error": state.get("error"),
                "result": state.get("result"),
                "nodes": nodes,
                "created_at": state.get("created_at"),
                "written_at": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2, ensure_ascii=False, default=str)
    except OSError:
        pass


def _memory_entries(graph_id: str) -> list[dict]:
    """The graph's long-term memory, as a folder of readable files.

    One folder per node that remembers, one file per thing it remembered,
    newest first — so what a workflow has learned sits beside its code and its
    runs instead of behind a tab in the run drawer.
    """
    import memory_store

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
    import stm_store

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
    import memory_store

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
    import memory_policy

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
    entries = _memory_entries(graph_id)
    if not root.is_dir():
        return entries
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        # Bookkeeping, not part of the project: it says which version of the
        # framework is sitting in vendor/, and means nothing to a reader.
        if rel == VENDOR_STAMP or "__pycache__" in rel:
            continue
        if path.is_dir():
            entries.append({"path": rel, "dir": True})
            continue
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append({
            "path": rel,
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
    import table_store

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
    import table_store

    rows = table_store.rows(graph_id, node, subject)
    return json.dumps({"subject": subject, "node": node,
                       "rows": len(rows), "record": rows},
                      indent=2, ensure_ascii=False, default=str)


def _table_file(graph_id: str, relpath: str) -> dict | None:
    """A table row-set addressed as a path, or None if that is not one."""
    import table_store

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

    A node that tracks a subject keeps one entry per subject, so the obligor's
    name is what makes the file findable — `sleep-number.json` rather than
    `007.json`, which said only how recently it happened to be written. Kept
    in one function because the listing and the reader must agree on it.
    """
    import memory_policy

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
    import stm_store

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
    target = _resolve_in_workspace(graph_id, relpath)
    if not target.is_file():
        raise WorkspaceError(f"No such workspace file: {relpath!r}", not_found=True)
    raw = target.read_bytes()
    truncated = len(raw) > MAX_READ_BYTES
    text = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    return {"path": relpath, "size": target.stat().st_size,
            "truncated": truncated, "content": text}


MAX_WRITE_BYTES = 10 * 1024 * 1024


def _refuse_if_memory(relpath: str, verb: str) -> None:
    """Memory is a view of a vector store, not a folder of files."""
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


def make_dir(graph_id: str, relpath: str) -> dict:
    """Create a workspace directory (parents included)."""
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
    target = _resolve_in_workspace(graph_id, relpath) if relpath else root
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
