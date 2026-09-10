"""Reading a model's structured answer.

Both the workflow chat and the agent endpoint ask a model for JSON and have to
cope with whatever comes back: a fenced block, JSON with prose around it, or
prose alone. Neither can use a provider's native structured-output mode,
because `llm.chat()` returns a plain string for every provider in
llm/providers.json and only some of them support it.
"""

import json
import re


def extract_json(text: str) -> dict:
    """Pull the operation object out of a model reply.

    Models wrap JSON in prose or fences often enough that a bare json.loads is
    not usable; try the strict read first, then a fenced block, then the first
    balanced object in the text.
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

    start = text.find("{")
    while start != -1:
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
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    # No JSON at all: treat the whole thing as a plain answer with no edits.
    return {"reply": text, "operations": []}
