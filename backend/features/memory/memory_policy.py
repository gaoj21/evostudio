"""What each node keeps in its long-term memory.

Enabling memory on a node used to mean storing everything it touched: every
input, every output, serialised together and cut off at a fixed budget. On a
node whose input is a page of news that leaves the memory full of the material
the node was given and none of the judgement it reached — the part worth
remembering.

A node now says which of its own fields are worth keeping. The budget is then
spent on those, and spent per field, so a large input can no longer crowd out
a short conclusion.
"""

import asyncio
import json

from . import bindings

# Whole-entry budget for content injected into a prompt.
DEFAULT_LIMIT = 4000
DEFAULT_RETRIEVE = 3
MAX_RETRIEVE = 10
# How much of the current session a node is reminded of. Only ever used when a
# run supplies a session, so a node set up before this existed is unaffected.
DEFAULT_SESSION_RECALL = 5
WHEN_CHOICES = ("success", "always")
# Coze keeps these apart as 数据库 and 知识库, and makes you pick. One
# abstraction with a dozen knobs trying to be both is how a node ends up
# looking like it remembers and quietly not doing so.
#
#   table   one row per subject per date. Exact key, point-in-time read,
#           a row that is either there or is not.
#   recall  similarity search over past runs, for nodes with no subject to
#           key on and a genuinely fuzzy question to ask.
KIND_CHOICES = ("table", "recall")


def policy(task: dict) -> dict:
    """A node's memory settings, with the defaults filled in.

    Three separate decisions, and a node makes each on its own: whether to
    remember at all (use_long_term_memory), what to keep (outputs/inputs,
    when), and what to read back (read_from, read, retrieve).

    `None` for a field list means "all of them" and is the default, so a node
    that was set up before any of this existed behaves exactly as it did.
    An empty list means "none", which is a real choice: a node may want to
    remember its conclusions without the material it drew them from.
    """
    raw = task.get("memory") or {}
    retrieve = raw.get("retrieve", DEFAULT_RETRIEVE)
    try:
        retrieve = max(0, min(int(retrieve), MAX_RETRIEVE))
    except (TypeError, ValueError):
        retrieve = DEFAULT_RETRIEVE
    when = raw.get("when") or "success"
    return {
        "version": raw.get("version", 1),
        "write_mode": raw.get("write_mode", "append" if raw.get("version") == 2 else "upsert"),
        "time_filter": raw.get("time_filter", bool(raw.get("at")) if raw.get("version") != 2 else False),
        "key": (raw.get("key") or "") if raw.get("write_mode") == "upsert" else "",
        "write_enabled": raw.get("write_enabled", True) is not False,
        "read_enabled": raw.get("read_enabled", True) is not False,
        "outputs": _names(raw.get("outputs")),
        "inputs": _names(raw.get("inputs")),
        "when": when if when in WHEN_CHOICES else "success",
        "retrieve": retrieve,
        "limit": _limit(raw.get("limit")),
        # Whose memory to search. None means its own, which is what every node
        # did before this existed; a list names nodes, so a node that decides
        # can read what the node that investigated remembered.
        # How many entries of the current session to put in front of the
        # node, in order. Long-term recall answers "when has this happened
        # before"; this answers "what did we just do".
        "session_recall": _bounded(raw.get("session_recall"), DEFAULT_SESSION_RECALL),
        # A field whose value identifies the thing being reasoned about — the
        # customer, the ticker, the device. When set, recall also returns
        # *that thing's* own past, in time order, which similarity search does
        # not reliably find: asking it about Sleep Number returns whatever is
        # semantically nearest, which may be three other companies.
        "match": None if raw.get("provider") == "mem0" else (raw.get("match") or "").strip() or None,
        # A node that names a subject is keeping a record, so it gets a table
        # unless it says otherwise. Declaring `match` used to buy an extra
        # block bolted onto a vector search; now it chooses the storage.
        "kind": _kind(raw),
        # Fields from the run that the node does not itself consume but the
        # entry is worth dating and placing by — the observation window, the
        # ticker, the event date. Declaring one as an input instead would force
        # it into the prompt, which the framework requires of every input.
        "context": _names(raw.get("context")) or [],
        # Which of those says *when the entry is about*. Without it the only
        # time an entry carries is when the run happened, and eight runs of one
        # batch all happened within a minute of each other.
        "at": (raw.get("at") or "").strip() or None,
        "read_from": None if raw.get("provider") == "mem0" else _names(raw.get("read_from")),
        # Which fields of a recalled entry to put in the prompt. None means all
        # of them: a node may keep the material it reasoned over without
        # wanting it read back at it every time.
        "read": _names(raw.get("read")),
    }


def _kind(raw: dict) -> str:
    if raw.get("version") == 2 and raw.get("provider") != "mem0":
        return raw.get("kind") or "table"
    if raw.get("provider") == "mem0":
        return "recall"
    wanted = (raw.get("kind") or "").strip()
    if wanted in KIND_CHOICES:
        return wanted
    return "table" if (raw.get("match") or "").strip() else "recall"


def _bounded(value, default: int) -> int:
    try:
        return max(0, min(int(value), MAX_RETRIEVE))
    except (TypeError, ValueError):
        return default


def _names(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value]


def _limit(value) -> int:
    try:
        return max(200, min(int(value), 20000))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _declared(task: dict, side: str) -> list[str]:
    return [p.get("name") for p in (task.get(side) or []) if p.get("name")]


def validate(task: dict, siblings: dict | None = None) -> list[str]:
    """Complaints about a node's memory settings, as human sentences.

    A field name that does not exist would otherwise store nothing and say
    nothing — the node would look like it was remembering and quietly not be.
    `siblings` maps every node name in the graph to its task, so a node that
    reads another's memory can be held to naming one that exists.
    """
    errors: list[str] = []
    raw = task.get("memory")
    if raw is None:
        return errors
    if not isinstance(raw, dict):
        return [f"Task '{task.get('name')}': memory settings must be an object."]

    name = task.get("name")
    if raw.get('store_id') is not None:
        value = raw['store_id']
        if not isinstance(value, str) or not value.strip() or value in ('.', '..') or any(c in value for c in ('/', '\\', '\x00')):
            errors.append(f"Task '{name}': invalid Memory store ID.")
    for flag in ("read_enabled", "write_enabled", "time_filter"):
        if flag in raw and not isinstance(raw[flag], bool):
            errors.append(f"Task '{name}': memory '{flag}' must be true or false.")
    for side in ("outputs", "inputs"):
        selected = _names(raw.get(side))
        if selected is None:
            continue
        declared = _declared(task, side)
        unknown = [s for s in selected if s not in declared]
        if unknown:
            errors.append(
                f"Task '{name}': memory keeps {side} {unknown} that the node "
                f"does not have (it has {declared or 'none'})."
            )

    when = raw.get("when")
    if when is not None and when not in WHEN_CHOICES:
        errors.append(
            f"Task '{name}': memory 'when' must be one of {list(WHEN_CHOICES)}, "
            f"got {when!r}."
        )

    name = task.get("name")

    # `context` and `at` may name anything the run produces, not just what this
    # node consumes — that is the point of them.
    if siblings is not None:
        # Additional context is optional content, not a workflow dependency.
        # Stale fields are visible in the inspector and reported when absent.
        # An unqualified field no node produces is a field of the data: it
        # becomes an optional workflow input (bindings.binding_inputs). Only
        # a qualified reference to a node field that does not exist is wrong.
        for label, reference in (("time", raw.get("at")), ("match", raw.get("match")),
                                 ("unique key", raw.get("key"))):
            if (reference and str(reference).startswith("nodes.")
                    and not bindings.declared(reference, task, siblings)):
                errors.append(f"Task '{name}': memory {label} binding '{reference}' names a node field that does not exist. Choose an available field.")
    if raw.get("kind") not in (None, "table", "recall"):
        errors.append(f"Task '{name}': invalid memory retrieval method.")
    if raw.get("write_mode") not in (None, "append", "upsert"):
        errors.append(f"Task '{name}': invalid memory write mode.")
    if raw.get("version") == 2:
        if raw.get("kind") == "recall" and raw.get("match"):
            errors.append(f"Task '{name}': exact matching requires structured records; semantic recall does not guarantee an exact match.")
        if raw.get("write_mode") == "upsert" and not raw.get("key"):
            errors.append(f"Task '{name}': updating memory requires an explicit unique key binding.")
        if raw.get("time_filter") and not raw.get("at"):
            errors.append(f"Task '{name}': time filtering requires a time binding.")
    if raw.get("provider") == "mem0" and any(raw.get(k) for k in ("match", "at", "key", "time_filter")):
        errors.append(f"Task '{name}': this Mem0 adapter supports semantic recall, not exact keys or time filtering. Clear those bindings explicitly before switching backend.")

    readable = _names(raw.get("read_from"))
    if readable is not None and siblings is not None:
        unknown = [r for r in readable if r not in siblings]
        if unknown:
            errors.append(
                f"Task '{name}': memory reads from {unknown}, which "
                f"{'is not a node' if len(unknown) == 1 else 'are not nodes'} "
                "in this workflow."
            )

    wanted = _names(raw.get("read"))
    if wanted and siblings is not None:
        sources = readable if readable is not None else [name]
        available = {
            field
            for source in sources
            for side in ("inputs", "outputs")
            for field in _declared(siblings.get(source) or {}, side)
        }
        if available:
            missing = [w for w in wanted if w not in available]
            if missing:
                errors.append(
                    f"Task '{name}': memory reads fields {missing} that "
                    f"{'; '.join(sources)} "
                    f"{'does' if len(sources) == 1 else 'do'} not have."
                )

    if not task.get("use_long_term_memory"):
        return errors

    # Enabled, but configured to keep nothing at all: the node would open a
    # store, retrieve from it, and never write to it.
    settings = policy(task)
    if settings["write_enabled"] and settings["outputs"] == [] and settings["inputs"] == []:
        errors.append(
            f"Task '{name}': long-term memory is on but nothing is selected to "
            "keep. Choose at least one input or output, or turn memory off."
        )
    return errors


def _pick(values: dict, selected) -> dict:
    """The chosen fields, in the node's declared order, skipping absent ones."""
    if selected is None:
        return dict(values)
    return {k: values[k] for k in selected if k in values}


def _text(value) -> str:
    """A field as it will be read back — text stays text, rather than becoming
    a quoted JSON string that every later reader has to unwrap."""
    return value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str)


def _fit(fields: dict, limit: int) -> dict:
    """The fields as text, trimmed to the budget, largest field first.

    Cutting the serialised whole would truncate whichever field happened to be
    last — usually the outputs, which are the reason the memory exists. Taking
    it off the biggest field instead keeps every short field intact.
    """
    text = {k: _text(v) for k, v in fields.items()}
    while text and sum(len(v) for v in text.values()) > limit:
        widest = max(text, key=lambda k: len(text[k]))
        room = limit - sum(len(v) for k, v in text.items() if k != widest)
        if room < 40:
            # No sensible share left for this field. Storing a few meaningless
            # characters of each helps nobody, so the smallest is dropped
            # whole and the budget re-spent on what is left.
            if len(text) == 1:
                text[widest] = text[widest][:max(0, room)]
                break
            del text[min(text, key=lambda k: len(text[k]))]
            continue
        text[widest] = text[widest][:room - 1] + "…"
    return text


def with_context(task: dict, inputs: dict, run_data: dict | None) -> dict:
    """A node's inputs, plus the context fields it does not itself consume.

    `as_of` is the obvious one: the node is dated by it but never takes it as
    an input, so without this the value is simply absent at recall time and
    every time-based decision silently falls back to "no date".
    """
    resolved = dict(inputs or {})
    settings = policy(task)
    for name in dict.fromkeys([*settings["context"], settings["at"], settings["match"], settings["key"]]):
        if not name:
            continue
        if name in resolved:
            continue
        value = bindings.resolve({**(run_data or {}), **resolved}, name)
        if value is not None:
            resolved[name] = value
    return resolved


def lookup(data: dict, name: str):
    """A field of the run, or a key inside one — `detection.source`.

    Nodes answer in JSON held as one string field; the thing worth recording
    (which source the detection came from, say) is usually a key inside it.
    A dotted name reaches in: the first segment is the field, the rest walk
    the parsed JSON. Absent at any step means absent — never a guess.
    """
    for key in sorted(data, key=len, reverse=True):
        if name.startswith(key + "."):
            return bindings.resolve({"value": data[key]}, "value" + name[len(key):])
    head, _, rest = name.partition(".")
    if head not in data:
        return None
    value = data[head]
    if not rest:
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    for key in rest.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return None
    return value


def select(task: dict, node_inputs: dict, node_outputs: dict, *,
           succeeded: bool = True, run_data: dict | None = None):
    """What to write to this node's memory, or None to write nothing.

    `run_data` is everything the run produced, so an entry can record the
    period it covers even when the node never consumed it.

    Returns the payload dict. A node with nothing selected present in this run
    stores nothing rather than an empty husk: an entry with no content still
    takes up one of the slots retrieved into a later prompt.
    """
    settings = policy(task)
    if not settings["write_enabled"]:
        return None
    if settings["when"] == "success" and not succeeded:
        return None

    original_inputs = set(node_inputs or {})
    node_inputs = with_context(task, node_inputs, run_data)
    content_inputs = {k: v for k, v in node_inputs.items() if k in original_inputs or k in settings["context"]}
    if settings["inputs"] is not None:
        settings = {**settings,
                    "inputs": list(settings["inputs"]) + [
                        n for n in settings["context"] if n in node_inputs]}

    kept_inputs = _pick(content_inputs, settings["inputs"])
    kept_outputs = _pick(node_outputs or {}, settings["outputs"])
    if not kept_inputs and not kept_outputs:
        return None

    fields = {f"in.{k}": v for k, v in kept_inputs.items()}
    fields.update({f"out.{k}": v for k, v in kept_outputs.items()})
    trimmed = _fit(fields, settings["limit"])

    metadata = {}
    unbound = []
    available = {**node_inputs, **(node_outputs or {})}
    for label, reference in (("subject", settings["match"]), ("event_time", settings["at"]), ("key", settings["key"])):
        if reference:
            value = available.get(reference)
            if value is None:
                value = bindings.resolve({**(run_data or {}), **available}, reference)
            if value is None or value == "":
                if label == "key":
                    # An update needs to know which record it updates.
                    raise bindings.BindingError(f"Memory '{task.get('name')}' cannot update: unique key '{reference}' is missing in this run.")
                # The record is still worth keeping; it is just not linked to
                # an entity or a date, so exact and time-filtered reads skip it.
                unbound.append({"label": label, "reference": reference})
                continue
            if label == "event_time":
                normalized = bindings.timestamp(value)
                metadata[label] = normalized if settings["version"] == 2 else str(value)
            else:
                metadata[label] = str(value)
    payload = {"task": task.get("name"), "inputs": {}, "outputs": {}, "metadata": metadata}
    if unbound:
        payload["unbound"] = unbound
    for key, text in trimmed.items():
        side, _, field = key.partition(".")
        payload["inputs" if side == "in" else "outputs"][field] = text
    if not payload["inputs"] and not payload["outputs"]:
        return None
    return payload


def describe(task: dict) -> str:
    """One line for the inspector and the exported project's README."""
    if not task.get("use_long_term_memory"):
        return "off"
    settings = policy(task)
    def part(label, chosen):
        if chosen is None:
            return f"all {label}"
        if not chosen:
            return None
        return f"{label} {', '.join(chosen)}"
    kept = [p for p in (part("outputs", settings["outputs"]),
                        part("inputs", settings["inputs"])) if p]
    suffix = "" if settings["when"] == "success" else ", including failed runs"
    return f"keeps {' and '.join(kept)}{suffix}"


def query_for(task: dict, inputs: dict, limit: int = 800) -> str:
    """What to look for in this node's memory, given the inputs it just got.

    Built from the same fields the node stores, so the query looks like the
    entries it is searching against. A node that keeps only `company` should
    not be searching with a page of news it never wrote down — that is noise
    against every entry equally.

    A node that stores no inputs at all has nothing to match on, so its own
    inputs are used anyway: the query is never written anywhere, it only has
    to describe the situation well enough to rank past entries.
    """
    settings = policy(task)
    kept = _pick(inputs or {}, settings["inputs"]) or dict(inputs or {})
    fields = _fit({k: v for k, v in kept.items()}, limit)
    described = "; ".join(f"{k}: {v}" for k, v in fields.items())
    return f"{task.get('description', '')}\n{described}".strip()


async def _search(memory, query: str, n: int):
    """Search a store from inside a running event loop.

    The framework backend's `search()` wraps its own implementation in
    `asyncio.run()`, which raises when there is already a loop running — and
    recall happens inside the workflow's loop. Its async entry point is used
    where there is one; the langchain backend has only the synchronous call,
    so that goes to a worker thread rather than blocking the loop.
    """
    search_async = getattr(memory, "search_async", None)
    if search_async is not None:
        return await search_async(query, n)
    return await asyncio.to_thread(memory.search, query, n)


def stores_read_by(task: dict) -> list[str] | None:
    """Which nodes' memory this one reads, or None for its own."""
    return policy(task)["read_from"]


def session_block(entries: list[dict], task: dict) -> str:
    """What happened earlier in this session, in the order it happened.

    A log, not a search: the point of it is the sequence, so it is not ranked
    and not deduplicated. Entries from this node are unattributed; others say
    which node they came from.
    """
    settings = policy(task)
    wanted = settings["session_recall"]
    if not wanted:
        return ""
    # Not `entries[-wanted:]`: a slice of -0 is the whole list, so asking for
    # none of the session would have given all of it.
    lines = []
    for entry in entries[-wanted:]:
        node = entry.get("node")
        text = render(
            json.dumps({"inputs": entry.get("inputs") or {},
                        "outputs": entry.get("outputs") or {}},
                       ensure_ascii=False, default=str),
            keep=settings["read"],
        )
        if not text:
            continue
        lines.append(f"- {text}" if node == task.get("name")
                     else f"- ({node}) {text}")
    if not lines:
        return ""
    return "\n\nEarlier in this session:\n" + "\n".join(lines)


# A subject's timeline is one entry that grows, so it needs a ceiling. Well
# past a year of daily steps; a subject seen more often than that keeps its
# most recent points.
MAX_TIMELINE = 500


def subject_of(task: dict, payload: dict) -> str | None:
    """What this entry is about — the customer, the ticker, the device.

    None when the node tracks no subject, and then memory stays one entry per
    run, which is all a node with nothing to accumulate against needs.
    """
    field = policy(task)["match"]
    if not field:
        return None
    if "subject" in payload.get("metadata", {}):
        return payload["metadata"]["subject"]
    if isinstance(payload.get("subject"), dict):
        value = payload["subject"].get(field)
    else:
        sides = {**(payload.get("inputs") or {}), **(payload.get("outputs") or {})}
        value = sides.get(field)
    return None if value in (None, "") else str(value)


def points_of(payload: dict, dated_by: str | None = None, seen=None) -> list[dict]:
    """The dated states inside a stored entry, whichever shape it is in.

    An entry written before timelines existed holds a single run; it is read
    as a one-point timeline so old and new stores answer the same question.

    Each point carries two times, and they are not interchangeable. `at` is
    what the state is *about* and is the only one a cutoff may compare, since
    it is the only one on the same clock as the run. `seen` is when it was
    written, used to order and label a state the node never dated — comparing
    that against an `as_of` would be comparing two different clocks.
    """
    written = str(seen or "")
    timeline = payload.get("timeline")
    if isinstance(timeline, list):
        return [{**p, "seen": str(p.get("at") or p.get("seen") or written)}
                for p in timeline if isinstance(p, dict)]
    sides = {**(payload.get("inputs") or {}), **(payload.get("outputs") or {})}
    when = payload.get("metadata", {}).get("event_time") or (str(sides.get(dated_by) or "") if dated_by else "")
    return [{"at": when,
             "seen": when or written,
             "inputs": payload.get("inputs") or {},
             "outputs": payload.get("outputs") or {}}]


def as_timeline(task: dict, payload: dict) -> dict:
    """This run's selection, shaped as the subject's timeline of one point."""
    settings = policy(task)
    dated_by = settings["at"]
    sides = {**(payload.get("inputs") or {}), **(payload.get("outputs") or {})}
    return {
        "task": payload.get("task"),
        "subject": {settings["match"]: subject_of(task, payload)},
        "dated_by": dated_by,
        "timeline": [{
            "at": payload.get("metadata", {}).get("event_time") or (str(sides.get(dated_by) or "") if dated_by else ""),
            "inputs": payload.get("inputs") or {},
            "outputs": payload.get("outputs") or {},
        }],
    }


def merge_timeline(existing: list[dict], addition: dict,
                   dated_by: str | None = None, limit: int = MAX_TIMELINE) -> dict:
    """Fold a new dated state into what is already known about the subject.

    Entries written per run are absorbed as they are found, so a store from
    before this existed becomes one entry per subject the next time that
    subject comes round. Re-running a date replaces that date rather than
    stacking a second copy of it.
    """
    dated: dict[str, dict] = {}
    loose: list[dict] = []
    for payload in list(existing or []) + [addition]:
        for point in points_of(payload, dated_by):
            if point.get("at"):
                dated[str(point["at"])] = point
            else:
                loose.append(point)
    ordered = sorted(loose + list(dated.values()),
                     key=lambda p: str(p.get("seen") or ""))
    return {**addition, "timeline": ordered[-limit:]}


def history_for(entries: list[dict], task: dict, inputs: dict) -> str:
    """This subject's own past, in the order it happened.

    Not a search. Two runs about the same company are about the same company
    whether or not their text is alike, and a monitoring pipeline asking "have
    we flagged this customer before" needs the answer to be exact.
    """
    settings = policy(task)
    field = settings["match"]
    if not field or not settings["retrieve"]:
        return ""
    wanted = (inputs or {}).get(field)
    if wanted in (None, ""):
        return ""

    dated_by = settings["at"]
    # What this run is about, in the node's own dating field. Everything
    # recalled has to predate it: walking a window month by month is only
    # worth doing if December cannot see May. Blank when the node is not
    # dated at all, and then there is nothing to be earlier than.
    cutoff = read_cutoff(task, inputs)
    dated: dict[str, dict] = {}
    loose: list[dict] = []
    for entry in entries or []:
        payload = _unwrap(entry.get("content"))
        if not isinstance(payload, dict):
            continue
        if subject_of(task, payload) != str(wanted):
            continue
        for point in points_of(payload, dated_by, entry.get("timestamp")):
            when = str(point.get("at") or "")
            if cutoff and not _earlier(when, cutoff):
                continue
            if when:
                # One entry per subject is the shape now, but a store part-way
                # through the change still holds per-run entries beside it.
                # Keyed by date, so the same state is not read out twice.
                dated[when] = point
            else:
                loose.append(point)
    points = sorted(loose + list(dated.values()),
                    key=lambda p: str(p.get("seen") or ""))
    if not points:
        return ""

    lines = []
    for point in points[-settings["retrieve"]:]:
        text = _render_payload(point, keep=settings["read"])
        if text:
            when = str(point.get("seen") or "")
            lines.append(f"- {when[:19]} {text}" if when else f"- {text}")
    if not lines:
        return ""
    covers = f" by {dated_by}" if dated_by else ""
    upto = f" {cutoff}" if cutoff else ""
    return (f"\n\nWhat this pipeline has seen for {field} = {wanted!r} before"
            f"{upto}, oldest first{covers}:\n" + "\n".join(lines))


def table_block(rows: list[dict], task: dict, wanted: str, cutoff: str) -> str:
    """Rows from the node's table, as the prompt sees them.

    Deliberately plain: the subject, then one line per date. A model reading
    "what did we conclude about this customer, and when" should not have to
    parse a wall of escaped JSON to find out.
    """
    settings = policy(task)
    lines = []
    for row in rows or []:
        text = _render_payload(row.get("payload") or {},
                               limit=settings["limit"], keep=settings["read"])
        if not text:
            continue
        at = str(row.get("at") or "")
        lines.append(f"- {at[:19]} {text}" if at else f"- {text}")
    if not lines:
        return ""
    field = settings["match"]
    upto = f" before {cutoff}" if cutoff else ""
    label = f"for {field} = {wanted!r}" if field else "across recent entries"
    return (f"\n\nWhat this pipeline has recorded {label}"
            f"{upto}, oldest first:\n" + "\n".join(lines))


def read_cutoff(task, inputs):
    settings = policy(task)
    if not settings["time_filter"]:
        return ""
    field = settings["at"]
    value = (inputs or {}).get(field)
    if not field or value is None or value == "":
        raise bindings.BindingError(
            f"Memory for '{task.get('name')}' is dated by '{field}', which this run did not provide, so "
            f"reading was skipped rather than risk returning later records. Provide '{field}' "
            f"with the run, or turn off 'Only read memories before the bound time'.")
    return bindings.timestamp(value)


def table_read(store, graph_id: str, task: dict, inputs: dict):
    """This subject's rows for the run being processed, or None.

    None rather than an empty list when the node keeps no table or the run
    says nothing about which subject it concerns — there is a difference
    between "no history" and "not asking".
    """
    settings = policy(task)
    field = settings["match"]
    if settings["kind"] != "table" or not settings["retrieve"]:
        return None
    wanted = (inputs or {}).get(field) if field else None
    if field and wanted in (None, ""):
        raise bindings.BindingError(f"Memory match binding '{field}' is missing; lookup skipped.")
    cutoff = read_cutoff(task, inputs)
    if not field:
        rows = store.latest(graph_id, task.get("name"), settings["retrieve"], cutoff)
        return {"rows": rows, "subject": "", "cutoff": cutoff}
    rows = store.before(graph_id, task.get("name"), str(wanted),
                        cutoff, settings["retrieve"])
    return {"rows": rows, "subject": str(wanted), "cutoff": cutoff}


def table_write(task: dict, payload: dict):
    """What one run contributes to the table, or None if it keeps none."""
    settings = policy(task)
    if settings["kind"] != "table":
        return None
    subject = subject_of(task, payload)
    if subject is None and settings["match"]:
        # Appending needs no identity, so the record is kept unlinked; the
        # legacy entity-and-date update cannot tell which row it would replace.
        if settings["write_mode"] != "append" and not payload.get("metadata", {}).get("key"):
            return None
    subject = subject or ""
    sides = {**(payload.get("inputs") or {}), **(payload.get("outputs") or {})}
    dated_by = settings["at"]
    return {
        "subject": subject,
        "at": payload.get("metadata", {}).get("event_time") or (str(sides.get(dated_by) or "") if dated_by else ""),
        "key": payload.get("metadata", {}).get("key"),
        "payload": payload,
    }


async def recall_async(stores, task: dict, inputs: dict, history=None,
                       run_data: dict | None = None, table=None,
                       graph_id: str = "graph") -> str:
    """Past runs, as a block to append to this node's prompt.

    `stores` maps a node name to its opened memory. Which of them are searched
    is the node's own setting: its own by default, or whichever it named.

    Empty when the node asked for no recall or the stores have nothing. Memory
    is an aid, never a precondition: a store that cannot be searched is the
    caller's problem to report, not a reason for the node not to run.
    """
    settings = policy(task)
    if not settings["read_enabled"] or not settings["retrieve"]:
        return ""

    # The node is dated by a field it does not consume, so the run's own data
    # has to supply it — otherwise the cutoff below is always blank.
    inputs = with_context(task, inputs, run_data)
    read_cutoff(task, inputs)  # Missing configured dates must never allow an unfiltered query.
    blocks = []

    # A table node reads its table and nothing else. The two kinds are
    # disjoint on purpose: one store per node, so there is never a question
    # of which one holds the truth.
    if settings["kind"] == "table":
        if table is None:
            return ""
        for name in (settings["read_from"] if settings["read_from"] is not None else [task.get("name")]):
            found = table_read(table, graph_id, {**task, "name": name}, inputs)
            if not found:
                continue
            block = table_block(found["rows"], task, found["subject"], found["cutoff"])
            if block:
                blocks.append(block if name == task.get("name")
                              else f"\n\n(from {name})" + block)
        return "".join(blocks)

    if settings["match"] and history is not None:
        for name in (settings["read_from"] if settings["read_from"] is not None else [task.get("name")]):
            block = history_for(history(name), task, inputs)
            if block:
                blocks.append(block)

    wanted = settings["read_from"]
    if wanted is None:
        wanted = [task.get("name")]
    query = query_for(task, inputs)
    dated_by = settings["at"]
    cutoff = read_cutoff(task, inputs)

    lines = []
    for name in wanted:
        memory = (stores or {}).get(name)
        if memory is None:
            continue
        for message, _score in await _search(memory, query, settings["retrieve"]):
            # Similarity search reaches the whole store, so on a dated run it
            # will happily return next quarter's conclusion about this very
            # subject. Held to the same cutoff as the history block.
            if cutoff:
                payload = _unwrap(message.content)
                if not isinstance(payload, dict):
                    continue
                sides = {**(payload.get("inputs") or {}),
                         **(payload.get("outputs") or {})}
                if not _earlier(payload.get("metadata", {}).get("event_time") or str(sides.get(settings["at"]) or ""), cutoff):
                    continue
            text = render(message.content, keep=settings["read"])
            if not text:
                continue
            # Say whose memory it is only when it is not this node's own,
            # where the attribution would be noise.
            lines.append(f"- {text}" if name == task.get("name")
                         else f"- ({name}) {text}")
    if lines:
        label = (f"Similar past runs from before {cutoff}" if cutoff
                 else "Similar past runs, whenever they happened")
        blocks.append(f"\n\n{label}:\n"
                      + "\n".join(lines))
    return "".join(blocks)


def _earlier(when: str, cutoff: str) -> bool:
    """Is a stored entry from strictly before the run being processed?

    An entry with no date cannot be placed in time, so it does not qualify:
    letting it through is how a later verdict reaches an earlier step. Strict
    rather than inclusive so re-running one date does not feed on its own
    previous answer.
    """
    return bool(when) and bindings.timestamp(when) < bindings.timestamp(cutoff)


def _unwrap(content):
    """The payload inside a stored entry, however many times it was encoded."""
    text = content if isinstance(content, str) else str(content)
    for _ in range(3):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, str):
            text = parsed
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def _render_payload(payload: dict, limit: int = 500, keep=None) -> str:
    """One unwrapped entry as prose: what went in, what came out."""
    def side(fields):
        chosen = _pick(fields or {}, keep)
        return "; ".join(f"{k}: {_text(v)}" for k, v in chosen.items())

    given, produced = side(payload.get("inputs")), side(payload.get("outputs"))
    line = f"{given} \u2192 {produced}" if given and produced else (given or produced or "")
    return line[:limit] if len(line) > limit else line


def render(content, limit: int = 500, keep=None) -> str:
    """One recalled memory, as prose a model can actually read.

    What comes back out of the store is the JSON that went in, sometimes
    encoded a second time on the way through. Handed to the model raw it is a
    wall of escaped quotes and braces, most of the budget spent on syntax
    rather than on what the node decided last time.
    """
    text = content if isinstance(content, str) else str(content)
    payload = None
    for _ in range(3):        # unwrap however many times it was encoded
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            break
        if isinstance(parsed, str):
            text = parsed
            continue
        payload = parsed if isinstance(parsed, dict) else None
        break
    if payload is None:
        return text[:limit]

    def side(fields):
        chosen = _pick(fields or {}, keep)
        return "; ".join(f"{k}: {_text(v)}" for k, v in chosen.items())

    given, produced = side(payload.get("inputs")), side(payload.get("outputs"))
    if given and produced:
        line = f"{given} → {produced}"
    else:
        line = given or produced or ""
    return line[:limit] if len(line) > limit else line
