"""Tool calling over plain text, for models reached through the `llm` contract.

The contract is messages in, `LLMResult` (text + usage) out: there is no
channel for tool schemas or tool calls, and a provider behind it may not
support native function calling at all. So the tools travel in a system
instruction, the model answers with a JSON object when it wants one, and that
answer is read back into LangChain tool calls. Tool results go back to the
model as ordinary messages. Works the same with every provider.
"""

from __future__ import annotations

import ast
import json
import re
import uuid

MARKER = "tool_calls"


def _schema(tool: dict) -> dict:
    """{name, description, parameters} from an OpenAI-style tool schema."""
    function = tool.get("function", tool)
    return {"name": function.get("name"),
            "description": function.get("description") or "",
            "parameters": function.get("parameters") or {"type": "object", "properties": {}}}


def instruction(tools: list[dict], tool_choice=None) -> str:
    schemas = [_schema(t) for t in tools]
    must = ""
    if tool_choice in ("any", "required", True):
        must = "\nYou must call at least one tool in this reply."
    elif isinstance(tool_choice, str) and tool_choice not in ("auto", "none"):
        must = f"\nYou must call the tool `{tool_choice}` in this reply."
    elif isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or tool_choice).get("name")
        if name:
            must = f"\nYou must call the tool `{name}` in this reply."
    return (
        "You can call tools. To call one or more, reply with ONLY this JSON object and nothing else:\n"
        '{"tool_calls": [{"name": "<tool name>", "arguments": {<arguments matching its parameters>}}]}\n'
        "The results come back in the next message; then continue. "
        "When you need no tool, answer normally, without that JSON." + must +
        "\n\nTools:\n" + json.dumps(schemas, ensure_ascii=False))


def _balanced_end(text: str, start: int) -> int | None:
    """Index just past the object opening at `start`, or None if unclosed.
    Quotes of either kind delimit strings, so a Python-style dict counts."""
    depth, quote, escaped = 0, None, False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _load(fragment: str):
    """A JSON object as models write it: strict, then without trailing
    commas, then as a Python literal (single quotes, True/None)."""
    try:
        return json.loads(fragment)
    except ValueError:
        pass
    relaxed = re.sub(r",\s*([}\]])", r"\1", fragment)
    try:
        return json.loads(relaxed)
    except ValueError:
        pass
    try:
        return ast.literal_eval(relaxed)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def _objects(text: str):
    """(value, start, end) for each top-level object; value None if unreadable."""
    index = text.find("{")
    while index != -1:
        end = _balanced_end(text, index)
        if end is None:
            yield None, index, len(text)
            return
        yield _load(text[index:end]), index, end
        index = text.find("{", end)


def _call(entry) -> dict | None:
    """One LangChain tool call from a call entry in either shape:
    {"name", "arguments"} or OpenAI's {"function": {"name", "arguments"}}."""
    if not isinstance(entry, dict):
        return None
    function = entry.get("function") if isinstance(entry.get("function"), dict) else entry
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    args = function.get("arguments", function.get("args", entry.get("arguments", entry.get("args", {}))))
    if isinstance(args, str):
        loaded = _load(args) if args.strip() else {}
        args = loaded if loaded is not None else {"input": args}
    return {"name": name, "args": args if isinstance(args, dict) else {"input": args},
            "id": entry.get("id") or f"call_{uuid.uuid4().hex[:12]}", "type": "tool_call"}


def _entries(value) -> list[dict]:
    """The calls a `{"tool_calls": ...}` object holds, whatever their names."""
    if not isinstance(value, dict) or MARKER not in value:
        return []
    entries = value[MARKER]
    entries = [entries] if isinstance(entries, dict) else entries
    if not isinstance(entries, list):
        return []
    return [c for c in (_call(e) for e in entries) if c]


def parse(text: str, tools: list[dict]) -> tuple[str, list[dict]]:
    """(remaining text, LangChain tool calls) from a model's reply.

    Every `{"tool_calls": [...]}` object in the reply counts, merged in order.
    Only calls naming a tool it was given count; a reply that tried to call
    something else is what `unreadable_call` turns into a correction.
    """
    text = text or ""
    names = {_schema(t)["name"] for t in tools}
    calls, spans = [], []
    for value, start, end in _objects(text):
        found = [c for c in _entries(value) if c["name"] in names]
        if found:
            calls += found
            spans.append((start, end))
    if not calls:
        return text, []
    rest, last = [], 0
    for start, end in spans:
        rest.append(text[last:start])
        last = end
    rest.append(text[last:])
    # A fenced block around the JSON leaves its fences behind.
    rest = "".join(rest).replace("```json", "").replace("```", "").strip()
    return rest, calls


def unreadable_call(text: str, tools: list[dict]) -> str | None:
    """A correction for a reply that tried to call a tool but could not be
    read, or None when it is an ordinary answer (or a readable call)."""
    text = text or ""
    if MARKER not in text or parse(text, tools)[1]:
        return None
    names = [_schema(t)["name"] for t in tools]
    unknown = sorted({c["name"] for value, _, _ in _objects(text) for c in _entries(value)})
    problem = (f"it names tools that do not exist ({', '.join(unknown)})" if unknown else
               "it could not be read (malformed or incomplete JSON, or a call without a tool name)")
    return (f"Your last reply looks like a tool call, but {problem}, so no tool was run. "
            'To call a tool, reply with ONLY {"tool_calls": [{"name": "<tool name>", "arguments": {...}}]}. '
            f"Valid tools: {', '.join(names)}. If you need no tool, answer normally without that JSON.")


def as_text(message) -> tuple[str, str] | None:
    """(role, content) for the messages the contract has no role for:
    an assistant turn that called tools, and a tool's result."""
    kind = getattr(message, "type", "")
    if kind == "ai" and getattr(message, "tool_calls", None):
        calls = [{"name": c["name"], "arguments": c.get("args") or {}, "id": c.get("id")}
                 for c in message.tool_calls]
        text = message.content if isinstance(message.content, str) else ""
        return "assistant", (text + "\n" if text else "") + json.dumps({MARKER: calls}, ensure_ascii=False)
    if kind == "tool":
        content = message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False, default=str)
        name = getattr(message, "name", None) or "tool"
        return "user", f"Result of tool `{name}` (call {getattr(message, 'tool_call_id', '')}):\n{content}"
    return None
