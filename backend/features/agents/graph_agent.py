"""The Chat Agent's loop, as a LangGraph graph.

    START → agent ⇄ tools → END

`agent` calls the model with the tools bound; when it asks for tools, `tools`
(LangGraph's ToolNode) runs them and hands the results back; when it answers
without asking, the turn ends. The conversation lives in the graph's state —
messages, a scratch file system and a plan — saved by the checkpointer per
thread, so a session picks up where it left off.

Besides the Studio tools it is given, the agent always has:

- `write_todos`: its plan for a multi-step task (shown in the chat).
- `ls` / `read_file` / `write_file` / `edit_file`: scratch files kept in the
  conversation's state (not the disk), for notes and drafts across steps.

The state's keys are the ones Deep Agents used (`messages`, `files`,
`todos`), so conversations saved before still open.
"""
from __future__ import annotations

from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState, ToolNode
from langgraph.types import Command

READ_LIMIT = 2000
# Unreadable tool calls are pointed out to the model this many times in a row
# before its reply is taken as the answer.
MAX_CORRECTIONS = 2
CORRECTION = "tool_call_correction"


class TurnStopped(RuntimeError):
    """Stop, the deadline or the step limit: ends the turn, even from inside
    a tool. Any other tool failure goes back to the model as an error."""


class TurnTimedOut(TurnStopped, TimeoutError):
    """The turn's time limit; still a TimeoutError to whoever reports it."""

# The engine a node or Chat Agent names to run on this graph. "deepagents"
# is what configurations saved before say; it means the same.
ENGINE = "langgraph"
ENGINES = (ENGINE, "deepagents")


def is_agent_engine(config) -> bool:
    """A harness setting that runs a node on this agent graph."""
    return isinstance(config, dict) and config.get("engine") in ENGINES


def _merge_files(current: dict | None, update: dict | None) -> dict:
    """A file set to None is removed; anything else replaces what was there."""
    merged = dict(current or {})
    for path, content in (update or {}).items():
        if content is None:
            merged.pop(path, None)
        else:
            merged[path] = content
    return merged


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    files: Annotated[dict, _merge_files]
    todos: list


def _text(stored: Any) -> str:
    """A scratch file's text: plain, or the lines Deep Agents kept."""
    if isinstance(stored, dict):
        stored = stored.get("content", "")
    if isinstance(stored, list):
        return "\n".join(str(line) for line in stored)
    return str(stored or "")


@tool
def write_todos(todos: list[dict], tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Write your plan for a task with several steps: the whole list, each item
    {"content": "...", "status": "pending" | "in_progress" | "completed"}.
    Update it as you go; skip it for simple requests."""
    return Command(update={"todos": todos, "messages": [
        ToolMessage(f"Plan updated: {len(todos)} item(s).", tool_call_id=tool_call_id)]})


@tool
def ls(state: Annotated[dict, InjectedState]) -> list[str]:
    """List your scratch files (kept in this conversation, not on disk)."""
    return sorted((state.get("files") or {}).keys())


@tool
def read_file(file_path: str, state: Annotated[dict, InjectedState],
              offset: int = 0, limit: int = READ_LIMIT) -> str:
    """Read one of your scratch files, with line numbers; offset and limit
    select lines for a long one."""
    files = state.get("files") or {}
    if file_path not in files:
        return f"Error: File '{file_path}' not found"
    lines = _text(files[file_path]).splitlines()
    shown = lines[offset:offset + limit]
    if not shown:
        return "(empty)" if not lines else f"Error: offset {offset} is past the end ({len(lines)} lines)"
    return "\n".join(f"{number:6d}\t{line}" for number, line in enumerate(shown, start=offset + 1))


@tool
def write_file(file_path: str, content: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Create or overwrite a scratch file with the given content."""
    return Command(update={"files": {file_path: content}, "messages": [
        ToolMessage(f"Updated file {file_path}", tool_call_id=tool_call_id)]})


@tool
def edit_file(file_path: str, old_string: str, new_string: str,
              state: Annotated[dict, InjectedState], tool_call_id: Annotated[str, InjectedToolCallId],
              replace_all: bool = False) -> Command | str:
    """Replace old_string with new_string in a scratch file. old_string must
    occur exactly once unless replace_all is true."""
    files = state.get("files") or {}
    if file_path not in files:
        return f"Error: File '{file_path}' not found"
    text = _text(files[file_path])
    count = text.count(old_string)
    if count == 0:
        return f"Error: the text to replace was not found in {file_path}"
    if count > 1 and not replace_all:
        return f"Error: the text to replace occurs {count} times in {file_path}; pass replace_all or give more context"
    updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    return Command(update={"files": {file_path: updated}, "messages": [
        ToolMessage(f"Edited {file_path} ({count if replace_all else 1} replacement(s))", tool_call_id=tool_call_id)]})


BUILT_IN = [write_todos, ls, read_file, write_file, edit_file]
BUILT_IN_PROMPT = (
    "\n\nYou also have write_todos, to plan a task with several steps, and scratch files "
    "(ls, read_file, write_file, edit_file) kept in this conversation for notes and drafts.")


def tool_error(error: Exception) -> str:
    """What the model sees when a tool fails, so it can fix the call or try
    another way. A stop, a deadline or a step limit is not the tool's
    failure: it propagates and ends the turn."""
    if isinstance(error, TurnStopped):
        raise error
    return f"Error: {type(error).__name__}: {error}. Fix the arguments or try another approach."


def _corrections(messages: list) -> int:
    """Corrections sent since the model last answered anything readable."""
    count = 0
    for message in reversed(messages):
        if getattr(message, "type", "") == "ai":
            continue
        if getattr(message, "name", None) == CORRECTION:
            count += 1
            continue
        break
    return count


def build_agent(model, tools: list, system_prompt: str, checkpointer=None,
                before_model: Callable[[], None] | None = None):
    """The compiled graph. `before_model` runs before every model call: the
    place for a stop, a deadline and a step limit to take effect."""
    from backend.features import text_tool_calls

    every_tool = [*tools, *BUILT_IN]
    bound = model.bind_tools(every_tool)
    prompt = SystemMessage(content=system_prompt + BUILT_IN_PROMPT)
    schemas = [{"name": t.name} for t in every_tool]

    def agent(state: AgentState) -> dict:
        if before_model is not None:
            before_model()
        return {"messages": [bound.invoke([prompt, *state.get("messages", [])])]}

    def unreadable(state: AgentState) -> str | None:
        last = (state.get("messages") or [None])[-1]
        text = last.content if isinstance(getattr(last, "content", None), str) else ""
        return text_tool_calls.unreadable_call(text, schemas)

    def next_step(state: AgentState) -> str:
        last = (state.get("messages") or [None])[-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        # A reply that meant to call a tool but could not be read goes back
        # with the reason, rather than ending the turn as raw JSON.
        if _corrections(state.get("messages") or []) < MAX_CORRECTIONS and unreadable(state):
            return "correct"
        return END

    def correct(state: AgentState) -> dict:
        return {"messages": [HumanMessage(content=unreadable(state) or "", name=CORRECTION)]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(every_tool, handle_tool_errors=tool_error))
    graph.add_node("correct", correct)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", next_step, ["tools", "correct", END])
    graph.add_edge("tools", "agent")
    graph.add_edge("correct", "agent")
    return graph.compile(checkpointer=checkpointer)
