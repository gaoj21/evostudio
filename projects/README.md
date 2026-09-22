# Project plugins

Studio is a general workflow platform. Code that only makes sense for one
task — a domain dataset, its preset nodes, a lookup toolkit, a starter
template — lives in that task's folder under `projects/<project>/` and reaches
Studio through one module:

```
projects/<project>/studio_plugin.py
```

The loader is `backend/features/plugins.py`. The platform never imports a
project by name; it asks every plugin what it offers. `projects/credit_risk/`
is the working example.

## Contract

Every attribute is optional.

| Attribute | Returns | Used by |
| --- | --- | --- |
| `NAME`, `DESCRIPTION` | strings | the `projects` list of `GET /api/features`; `NAME` is also the default palette group |
| `presets()` | `list[dict]` of palette presets (`{type, label, description, defaults}`, like the built-in entries of `GET /api/palette`) | `GET /api/palette` → `templates`. Each preset gets `"group": NAME` unless it sets its own `group` (the palette section title). |
| `templates()` | `list[{"id", "name", "description", "graph"}]` | `GET /api/templates`, `GET /api/templates/{id}` |
| `toolkits()` | `dict[name, {"factory", "description", "requires", "tool_names"}]`, the same shape as the built-in entries in `backend/features/library/tools_registry.py` | `GET /api/tools`, `task.tool_names`, tool nodes. A plugin toolkit with a built-in's name replaces it. |
| `source_types()` | `dict[type, schema + hooks]` | canvas Input types (below) |

### Input types (`source_types()`)

Each entry is a source schema, the same shape as the built-in ones in
`backend/features/data/source_apis.py`:

- `label`, `description`
- `config`: form fields (`{name, label, type: text|number|select|textarea, default, options?, required?}`); the Inspector form is built from them
- `outputs`: the field names each record carries

plus hooks, which are never sent to the browser:

| Hook | Required | What it does |
| --- | --- | --- |
| `records` | yes | `fn(config) -> list[dict]`. `config` is the node's `source` object (form values plus `type`). Returns the records. Anything but a list of dicts is refused. Raise `backend.features.data.sources.SourceError` for a problem the user should see: it becomes an HTTP 422. |
| `info` | no | `fn(params: dict) -> dict`. Details the form can show (versions, splits, counts). Served at `GET /api/sources/{type}/info`, with the query parameters passed as `params`. A `SourceError` becomes a 422. |
| `sequence` | no | `{"group": field, "order": field}`: records that share `group` form one trajectory (below). |
| `watch_key` | no | The field that identifies a record when the Input is watched on a schedule. Without it a watcher uses a hash of the whole record. |

`GET /api/sources` lists a plugin type with `local: true` (it runs inline and
needs no collection step), `project: <folder name>`, `sequence` when declared,
and `has_info: true` when `info` exists.

The project folder's parent (`projects/`) is added to `sys.path`, so a plugin
imports its own package by name (`from support_tickets.lib import x`). Import
heavy code inside the hook functions, as `credit_risk` does, so loading the
plugin stays cheap.

### Failures

A plugin that fails to import, or a hook that raises while Studio collects
presets, templates, toolkits or source types, is logged and listed in
`GET /api/features` → `projects` with `available: false` and
`unavailable_reason`. The rest of Studio keeps working. Plugins are loaded once
per process; restart the server (or call `plugins.reload()`) after editing
one.

## What `sequence` changes

Without `sequence`, every record is independent: a batch runs them in parallel
(up to `workers`), and a failure affects only its own record.

With `sequence: {"group": "ticket_id", "order": "created_at"}`:

- **Batch.** `sources.input_sequence(graph)` finds the declared fields on the
  wired Input. `batch.assign_sequences` writes each record's group value to
  its batch item as `trajectory`. Records of one trajectory run one after
  another, in the order `records` returned them, and different trajectories
  still run in parallel. If one record fails, the rest of its trajectory is
  marked `blocked` instead of running. A resume re-runs the failed record and
  everything after it in that trajectory. Both declared fields are kept
  through input mapping, even when no node consumes them.
- **Ordering priority** (`batch._group_key`): a DataLoader group comes first,
  then the Input's trajectory, then the key the memory configuration needs
  (`backend/features/memory/sequencing.py`). Studio never groups by a field
  name it assumes (no implicit `sample_id` / `as_of`).
- **Evolve.** Canvas Evolve (`{"source": "canvas"}`) replays records with the
  same `assign_sequences` / `_group_key` ordering, and a failure blocks the
  rest of that trajectory there too.
- **Preview.** `POST /api/graphs/{id}/run-batch/preview` returns
  `sequence` (the declared fields), `samples` (number of trajectories),
  `steps` / `steps_min` (the longest and shortest trajectory) and `dates`
  (first and last `order` value).
- **Run plan.** An Input's `n` counts trajectories, not records, so the plan
  reports the record count as unknown (`cardinality: null`).
- **Batch compare** labels a record by its trajectory.

Studio does not sort by `order`. `records` must return each trajectory's
records in order.

## Minimal example

`projects/support_tickets/studio_plugin.py`, with the data in
`projects/support_tickets/data/tickets.jsonl` (one message per line):

```python
"""Support-ticket triage as a Studio project plugin."""
import json
from pathlib import Path

NAME = "Support tickets"
DESCRIPTION = "Ticket messages as an Input, a triage preset and a starter workflow."

DATA = Path(__file__).resolve().parent / "data" / "tickets.jsonl"


def _records(config):
    from backend.features.data.sources import SourceError
    if not DATA.is_file():
        raise SourceError(f"No ticket data at {DATA}")
    rows = [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]
    if config.get("queue"):
        rows = [r for r in rows if r.get("queue") == config["queue"]]
    rows.sort(key=lambda r: (r["ticket_id"], r["created_at"]))  # trajectory order
    n = int(config.get("n") or 0)
    if n:
        keep = sorted({r["ticket_id"] for r in rows})[:n]
        rows = [r for r in rows if r["ticket_id"] in keep]
    return rows


def _info(params):
    rows = [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]
    queues = sorted({r.get("queue", "") for r in rows})
    return {"queues": queues, "tickets": len({r["ticket_id"] for r in rows}), "messages": len(rows)}


def source_types():
    return {
        "support_tickets": {
            "label": "Support tickets",
            "description": "Ticket messages, one record per message, in conversation order.",
            "config": [
                {"name": "queue", "label": "Queue", "type": "text", "default": ""},
                {"name": "n", "label": "Tickets (0 = all)", "type": "number", "default": 5},
            ],
            "outputs": ["ticket_id", "message_id", "created_at", "customer", "body"],
            "records": _records,
            "info": _info,
            "sequence": {"group": "ticket_id", "order": "created_at"},
            "watch_key": "message_id",
        },
    }


def presets():
    return [{
        "type": "st_triage",
        "label": "Triage message",
        "description": "Classify a ticket message and suggest a priority.",
        "defaults": {
            "description": "Classify the message and suggest a priority.",
            "inputs": [{"name": "body", "type": "str", "description": "Message", "required": True}],
            "outputs": [{"name": "triage", "type": "str", "description": "JSON verdict", "required": True}],
            "prompt": "Classify this support message. Reply as JSON "
                      "{{\"category\": ..., \"priority\": 0-100, \"review_required\": bool}}.\n\n{body}",
            "parse_mode": "str",
        },
    }]


def templates():
    return [{
        "id": "support-triage",
        "name": "Support triage",
        "description": "Ticket Input feeding one triage node.",
        "graph": {
            "name": "Support triage",
            "goal": "Triage support messages.",
            "tasks": [
                {"name": "tickets", "kind": "source", "x": -200, "y": 100,
                 "source": {"type": "support_tickets", "queue": "", "n": 5},
                 "outputs": [{"name": f, "type": "str", "required": False}
                             for f in ("ticket_id", "message_id", "created_at", "customer", "body")]},
                {"name": "triage", "x": 100, "y": 100,
                 **{k: v for k, v in presets()[0]["defaults"].items() if k != "description"},
                 "description": "Classify the message and suggest a priority."},
            ],
            "edges": [{"source": "tickets", "target": "triage"}],
            # Optional: route uncertain verdicts to the Review panel.
            "review": {"node": "triage", "score_field": "priority", "range": [40, 60],
                       "approve_label": "escalate", "reject_label": "close"},
        },
    }]
```

Prompts are Python format strings at run time: literal braces are doubled.

## Testing a plugin

Point the loader at a folder that holds only the projects you want, then
forget what was loaded before:

```python
from backend.features import plugins

def test_ticket_input(tmp_path, monkeypatch):
    project = tmp_path / "support_tickets"
    project.mkdir()
    (project / "studio_plugin.py").write_text(PLUGIN_SOURCE)
    monkeypatch.setenv("EAX_STUDIO_PROJECTS", str(tmp_path))  # os.pathsep-separated list
    plugins.reload()
    try:
        assert "support_tickets" in plugins.source_schemas()
        assert plugins.errors() == {}
    finally:
        monkeypatch.delenv("EAX_STUDIO_PROJECTS")
        plugins.reload()
```

`EAX_STUDIO_PROJECTS` replaces the default search path (`<repo>/projects`);
it does not add to it. The same variable loads plugins kept outside the
repository when the server starts.

Check an Input through the API as well: `GET /api/sources` (schema),
`POST /api/sources/probe` with `{"source": {"type": "support_tickets", ...}}`
(fields of the first record), and `POST /api/graphs/{id}/run-batch/preview`
with `{"source": "canvas"}` (trajectory counts).

## Rule

Task-specific code stays in `projects/<project>/` behind `studio_plugin.py`.
Do not add it to `backend/features/`, `backend/api/` or the generic components
in `frontend/src/`. If a plugin needs something the contract above cannot
express, extend the contract in `backend/features/plugins.py` in terms that
name no task.
