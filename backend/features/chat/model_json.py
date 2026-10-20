"""Reading a model's structured answer.

Both the workflow chat and the agent endpoint ask a model for JSON and have to
cope with whatever comes back: a fenced block, JSON with prose around it, or
prose alone. Neither can use a provider's native structured-output mode,
because `llm.chat()` returns a plain string for every provider in
llm/providers.json and only some of them support it.
"""

import json
import re


class MalformedJSON(ValueError):
    """The reply held a JSON object that was cut off or broken.

    Raised instead of falling back to an object nested inside it: from a
    truncated `{"operations": [{...}, {...` the inner `{...}` parses on its
    own, and acting on that fragment would run part of what the model meant.
    """


def _balanced_end(text: str, start: int) -> int | None:
    """Index just past the object opening at `start`, or None if unclosed."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def extract_json(text: str) -> dict:
    """Pull the operation object out of a model reply.

    Models wrap JSON in prose or fences often enough that a bare json.loads is
    not usable; try the strict read first, then a fenced block, then the first
    balanced top-level object in the text. Objects nested inside one that does
    not parse are never returned on their own: that is MalformedJSON.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("The model returned an empty reply.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    broken = None  # (start, end) of a top-level object that did not parse
    start = text.find("{")
    while start != -1:
        end = _balanced_end(text, start)
        if end is None:
            broken = (start, len(text))
            break
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            broken = broken or (start, end)
        start = text.find("{", end)

    if broken is not None and _has_inner_object(text[broken[0] + 1:broken[1]]):
        raise MalformedJSON("The model's reply was cut off or is not valid JSON.")
    # No JSON at all: treat the whole thing as a plain answer with no edits.
    return {"reply": text, "operations": []}


def _has_inner_object(text: str) -> bool:
    """Whether a broken object's body contains a complete JSON object."""
    start = text.find("{")
    while start != -1:
        end = _balanced_end(text, start)
        if end is not None:
            try:
                if isinstance(json.loads(text[start:end]), dict):
                    return True
            except json.JSONDecodeError:
                pass
        start = text.find("{", start + 1)
    return False
