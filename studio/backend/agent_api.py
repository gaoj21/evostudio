"""An OpenAI-compatible chat endpoint that turns Studio into a callable agent.

Everything else here is push: you trigger a workflow and it produces something.
This is the pull side — anything that speaks the OpenAI chat API (an SDK,
LangChain, Open WebUI, a phone client) can hold a conversation with an agent
that has this Studio's tools, skills and memory.

The agent is not bound to a workflow. Each message it decides for itself
whether to answer, call a tool, read a skill, or look something up in memory.

Tool calling is expressed as JSON rather than the provider's native protocol:
`llm.chat()` returns a string for every provider type in llm/providers.json and
only some of those providers support tool calls, so the loop stays the same
whichever model is configured.

Routes: POST /v1/chat/completions, GET /v1/models.
"""

import json
import os
import re
import time
import uuid
from pathlib import Path
from .studio_config import data_path

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import StreamingResponse

from . import model_json
from . import skills_api
from . import tools_registry
_REPO_ROOT = Path(__file__).resolve().parents[2]

MODEL_NAME = "evoagentx-agent"
MAX_STEPS = 8
MEMORY_DIR = data_path("agent-memory")
MEMORY_HITS = 4
MAX_OBSERVATION = 4000

router = APIRouter()

_env_loaded = False


class AgentError(Exception):
    """User-facing failure, returned as an OpenAI-shaped error."""


SYSTEM_PROMPT = """\
You are an agent running inside EvoAgentX Studio, reachable over its
OpenAI-compatible endpoint. You answer the user directly, and you may use the
capabilities below to find out what you need first.

Reply with ONE JSON object and nothing else.

To use a capability:
    {"thought": "why this is the next step", "tool": "<name>", "args": {...}}

To answer:
    {"reply": "what you want to say to the user"}

## Capabilities

- {"tool": "list_skills"} — the skills this Studio holds (name + description).
- {"tool": "load_skill", "args": {"name": "..."}} — a skill's full instructions.
  Skills are standing instructions written by this Studio's owner (a taxonomy,
  a rubric, a house style). Load one before doing work it governs.
- {"tool": "search_memory", "args": {"query": "..."}} — what earlier
  conversations recorded. Use it when the user refers to something from before,
  or when a preference may already be known.
- {"tool": "remember", "args": {"text": "..."}} — store something worth
  recalling in a later conversation. Use it for durable facts and preferences,
  not for chatter.

%(tools)s

## Rules

- Prefer answering from what you already know; reach for a capability when it
  genuinely decides the answer.
- One capability per message. You get its result and then continue.
- Never invent a tool result. If a call fails, say so plainly in your reply.
- After at most a few steps you must answer, so do not keep looking things up.
- Write `reply` in the user's language.
"""


def _tool_catalog() -> str:
    """Toolkits that can be used right now, as one line each."""
    lines = []
    for entry in tools_registry.list_tools():
        if not entry.get("available"):
            continue
        for sub in entry.get("tools") or []:
            args = ", ".join((sub.get("inputs") or {}).keys())
            lines.append(f'- {{"tool": "{sub["name"]}", "args": {{{args}}}}} — '
                         f'{sub.get("description", "")[:150]}')
    if not lines:
        return "No other tools are available."
    return "## Tools\n\n" + "\n".join(lines)


def _system_prompt() -> str:
    return SYSTEM_PROMPT % {"tools": _tool_catalog()}


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------

def _open_memory(session: str, create: bool):
    """Per-session long-term memory, through the layer's backend dispatch."""
    from memory import open_memory

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session or "default") or "default"
    return open_memory(MEMORY_DIR / safe, f"agent-{safe}", create=create)


def search_memory(session: str, query: str) -> list[str]:
    memory = _open_memory(session, create=False)
    if memory is None:
        return []
    try:
        from memory import unquote_content

        return [str(unquote_content(msg.content))[:800]
                for msg, _ in memory.search(query, n=MEMORY_HITS)]
    except Exception as e:
        raise AgentError(f"memory search failed: {e}")


def remember(session: str, text: str) -> str:
    from evoagentx.core.message import Message, MessageType

    memory = _open_memory(session, create=True)
    memory.add([Message(content=str(text)[:4000], msg_type=MessageType.RESPONSE,
                        agent="agent")])
    memory.save()
    return "stored"


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

def _ask(messages: list) -> str:
    from llm import chat as llm_chat
    from llm.registry import ProviderError

    try:
        return llm_chat(None, messages)
    except ProviderError as e:
        raise AgentError(f"no LLM provider configured: {e}")
    except Exception as e:
        raise AgentError(f"LLM call failed: {type(e).__name__}: {e}")


def _parse(text: str) -> dict:
    """The model's JSON answer, however it chose to wrap it."""
    return model_json.extract_json(text)


def _call_capability(name: str, args: dict, session: str) -> object:
    if name == "list_skills":
        return [{"name": s["name"], "description": s["description"]}
                for s in skills_api.list_skills()]
    if name == "load_skill":
        skill = skills_api.get_skill((args or {}).get("name") or "")
        if skill is None:
            raise AgentError(f"no skill named {(args or {}).get('name')!r}")
        return {"name": skill["name"], "instructions": skill["content"]}
    if name == "search_memory":
        hits = search_memory(session, str((args or {}).get("query") or ""))
        return hits or "nothing recorded about that"
    if name == "remember":
        return remember(session, (args or {}).get("text") or "")
    return tools_registry.call_tool(name, args or {})


def run_agent(messages: list, session: str) -> str:
    """Answer the conversation, using capabilities as needed."""
    history = [{"role": "system", "content": _system_prompt()}]
    history += [{"role": m.get("role", "user"), "content": str(m.get("content") or "")}
                for m in messages if m.get("role") != "system"]

    # What earlier conversations recorded, offered up front: the model should
    # not have to guess that asking is worthwhile.
    last_user = next((m["content"] for m in reversed(history)
                      if m["role"] == "user"), "")
    try:
        recalled = search_memory(session, last_user) if last_user else []
    except AgentError:
        recalled = []
    if recalled:
        history.insert(1, {"role": "system", "content":
                           "From earlier conversations:\n"
                           + "\n".join(f"- {r}" for r in recalled)})

    for step in range(MAX_STEPS):
        answer = _parse(_ask(history))
        if answer.get("reply") or not answer.get("tool"):
            return str(answer.get("reply") or "").strip() or "(no answer)"

        name = str(answer["tool"])
        try:
            result = _call_capability(name, answer.get("args") or {}, session)
            observation = json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            observation = json.dumps({"error": f"{type(e).__name__}: {e}"},
                                     ensure_ascii=False)
        if len(observation) > MAX_OBSERVATION:
            observation = observation[:MAX_OBSERVATION] + "… (truncated)"

        history.append({"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)})
        history.append({"role": "user", "content":
                        f"Result of {name}:\n{observation}\n\n"
                        + ("You have one step left — answer now."
                           if step == MAX_STEPS - 2 else
                           "Continue: use another capability or answer.")})

    return "I could not finish working that out within my step budget."


# ---------------------------------------------------------------------------
# OpenAI-compatible surface
# ---------------------------------------------------------------------------

def _load_env_once() -> None:
    """Read the repo .env so the key can live beside the other secrets.

    The llm layer loads it too, but only when a provider is first resolved —
    which happens *after* this check on the very first request.
    """
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        from dotenv import load_dotenv

        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass


def _check_key(authorization: str | None) -> None:
    """Optional shared secret.

    Off by default so a local setup just works; setting EAX_AGENT_KEY turns the
    endpoint into one that needs the key clients already send anyway. Worth
    setting whenever Studio is reachable beyond this machine: the agent can run
    tools, and some of those tools execute code.
    """
    _load_env_once()
    expected = os.environ.get("EAX_AGENT_KEY")
    if not expected:
        return
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


@router.get("/v1/models")
def list_models():
    return {"object": "list", "data": [
        {"id": MODEL_NAME, "object": "model", "created": int(time.time()),
         "owned_by": "evoagentx-studio"},
    ]}


# Deliberately sync: the framework's LongTermMemory.load() calls asyncio.run(),
# which explodes inside a running event loop. FastAPI hands a sync endpoint to
# a worker thread, which is also how runner and chat_api reach the framework.
@router.post("/v1/chat/completions")
def chat_completions(body: dict = Body(...),
                     authorization: str | None = Header(default=None)):
    _check_key(authorization)

    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages is required")
    # OpenAI's own field for identifying the end user; it keys this agent's
    # memory, so a client that sets it gets continuity across conversations.
    session = str(body.get("user") or "default")
    model = str(body.get("model") or MODEL_NAME)

    try:
        reply = run_agent(messages, session)
    except AgentError as e:
        raise HTTPException(status_code=502, detail=str(e))

    created = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    if body.get("stream"):
        # One chunk then [DONE]: enough for clients that only speak streaming
        # (Open WebUI and friends default to it) without pretending to produce
        # tokens the loop never emitted incrementally.
        def events():
            first = {"id": completion_id, "object": "chat.completion.chunk",
                     "created": created, "model": model,
                     "choices": [{"index": 0, "delta": {"role": "assistant",
                                                        "content": reply},
                                  "finish_reason": None}]}
            yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
            last = {"id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": reply}}],
        # Token accounting is not tracked through the loop; the fields are here
        # because clients read them, and zeros are honest about that.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
