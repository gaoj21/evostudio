"""Tool calling over plain text, for models reached through the `llm` contract.

The contract is messages in, `LLMResult` (text + usage) out: there is no
channel for tool schemas or tool calls, and a provider behind it may not
support native function calling at all. So the tools travel in a system
instruction, the model answers with a JSON object when it wants one, and that
answer is read back into LangChain tool calls. Tool results go back to the
model as ordinary messages. Works the same with every provider.
"""

from __future__ import annotations

import json
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


def _objects(text: str):
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        yield value, index, end
        index = text.find("{", end)


def parse(text: str, tools: list[dict]) -> tuple[str, list[dict]]:
    """(remaining text, LangChain tool calls) from a model's reply.

    Only calls naming a tool it was given count; anything else is text.
    """
    names = {_schema(t)["name"] for t in tools}
    for value, start, end in _objects(text or ""):
        if not isinstance(value, dict) or not isinstance(value.get(MARKER), list):
            continue
        calls = []
        for call in value[MARKER]:
            if not isinstance(call, dict) or call.get("name") not in names:
                continue
            args = call.get("arguments", call.get("args", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"input": args}
            calls.append({"name": call["name"], "args": args if isinstance(args, dict) else {"input": args},
                          "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}", "type": "tool_call"})
        if calls:
            rest = (text[:start] + text[end:]).strip()
            # A fenced block around the JSON leaves its fences behind.
            rest = rest.replace("```json", "").replace("```", "").strip()
            return rest, calls
    return text, []


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
