# Deep Agents + LangGraph integration

Implemented 2026-09-09.

## Runtime and responsibilities

`backend/api/harness.py` builds a real `deepagents.create_deep_agent` with a
LangGraph SQLite checkpointer. It does not route execution through the old
JSON-prompt agent loop. Provider configuration comes from the existing
`llm.registry`: OpenAI-compatible endpoints use ChatOpenAI; LiteLLM providers
use ChatLiteLLM with native tool calls. Keys remain server-side.

Install with `pip install -e '.[studio,harness,mem0]'` in the project virtual
environment. The harness extra pins Deep Agents 0.7.13, LangGraph 1.2.11,
SQLite checkpointer 3.1.1 and provider adapters. Google adapter 4.3.7 and
Google GenAI <2 keep the existing browser-use dependency compatible.

The runtime provides planning and checkpoint-backed virtual working files,
context management, model-step limits, and a cooperative execution deadline.
Subagent delegation is currently filtered out of model tools and rejected at
the execution boundary, so it cannot bypass per-Agent limits.

## Existing workflow Agents

Inspector → Harness → Deep Agents + LangGraph opts a node into the new runtime.
Existing nodes retain their standard engine until changed. The harness
configuration survives canvas serialization, save and reload and contributes
to the existing run-plan revision hash.

The runner preserves upstream inputs, the configured prompt, attached Skills,
selected toolkits, and the existing Memory recall policy. Deep Agents receives
an isolated checkpoint thread for each node execution and returns JSON fields
matching the declared outputs. Missing required fields fail the node. Normal
post-run Memory writes remain handled by the workflow runner; the harness does
not independently write a duplicate workflow memory entry.

Shared Mem0 reads can also be requested as tools when the node has read access.
Legacy trajectory/table recall still uses the existing dated recall path.
Workflow stop signals are propagated to the harness at its next operation
boundary. A currently executing blocking tool or provider request may finish
first; stopping does not roll back completed tool side effects.

Standalone Python export of a Deep Agents workflow is explicitly rejected for
now, rather than silently exporting the standard engine. JSON graph data keeps
the harness settings. The previous OpenAI-compatible standalone agent endpoint
and workflow-editing Chat remain available with their previous behavior.

## Independent canvas Chat Agents

`+ Add Chat Agent` immediately creates a server-persisted Agent and shows its
node on the canvas. Click it to chat or open Harness settings. These resources
are stored separately from workflow tasks and edges: clicking workflow Run
never executes them, and workflow Save is not needed to save their settings.
Node positions are saved after dragging. Chat Agents are scoped to the task's
stable task ID, falling back to graph ID for legacy documents.

Each Agent has independent sessions, instructions, provider selection, maximum
model steps, execution deadline and Memory connections. A message starts a
background execution; the UI polls persisted messages, status and activity.
The UI shows tool calls/results and plans, supports new sessions and Stop, and
restores history after reload. It does not currently stream individual tokens.
The service allows up to four concurrent executions, rejects concurrent turns
in one session, and captures settings at the start of a turn.

Successful sessions accept follow-up messages with prior LangGraph state.
Stopped, failed, or server-interrupted sessions can accept another message in
the same conversation. Recovery starts a new checkpoint generation seeded with
completed conversation messages, excluding failed user attempts. Pending tools
are never replayed automatically. Subsequent successful turns reuse that new
checkpoint, preserving continuous conversation. Working files and partial tool
state from an interrupted generation are not restored.

## Tools, Skills and Memory

- `list_available_tools` discovers currently usable registered tools and schemas.
- `call_studio_tool` validates the current toolkit allowlist before dispatching
  through the existing tool registry. Canvas Chat Agents default to all
  available toolkits. Workflow Agents use their selected toolkits.
- `list_skills` and `load_skill` expose existing Studio Skills.
- `list_connected_memories`, `search_memory`, and `remember` operate only on
  explicitly connected spaces and enforce separate read/write capabilities.
- Mem0 additionally validates that each space belongs to the graph's project
  (or isolated task). Sharing a space does not share chat history.

Memory uses a unified resource catalog (`GET /memory-resources`) with stable
IDs and actual resource names. Existing Mem0 UUID bindings remain compatible;
workflow history uses `mem:<node>`. Chat can read legacy table/recall history,
including investigate and decide memories. These stores remain writable only
by their owning workflow Agent. Shared Mem0 supports separate read and write.

Drag Memory → Chat for read access, or Chat → shared Mem0 for write access.
Settings toggles save immediately and update the same persisted connections.
Saves are serialized per Agent; sending is disabled while settings are saving.
Deleting a connection removes that capability for subsequent turns.

Before each message, connected readable memories are searched and injected as
reference data, with resource names and hit counts recorded in execution
activity. Overview prompts fall back to a labelled table preview with the full record
count. The browse_memory tool paginates all table records for cross-subject
summaries; previews alone must not be treated as a complete population.
Table searches match company/subject names and support an exclusive
`before` cutoff through the search tool. After a successful turn, writable
Mem0 spaces receive the user/assistant exchange; failures are surfaced as
Memory activity errors without discarding the answer. Failed or cancelled
model turns do not perform this automatic write. Explicit memory tools enforce
the same connection permissions. Disconnecting prevents future retrieval, but
does not erase content already present in an existing conversation checkpoint.

Tool capability is provided by the existing toolkit implementation; the harness
is not an OS sandbox. Workspace-aware tools use the task workspace. File and
command tools retain their existing capabilities. API/tool failure messages
shown in session status do not include raw provider exceptions or credentials.

## Storage and endpoints

Under the configured Studio data root:

- `harness/studio.sqlite`: Agent settings, session messages, status and activity.
- `harness/checkpoints.sqlite`: LangGraph messages, working files and state.
- `mem0/`: existing independent shared long-term memory store.

API prefix: `/api/graphs/{graph_id}/agents`.

- GET/POST prefix: list/create Agents.
- PUT `/{agent_id}`: save Agent configuration and position.
- GET/POST `/{agent_id}/sessions`: list/create sessions.
- POST `/{agent_id}/sessions/{session_id}/messages`: submit a turn (202).
- POST `/{agent_id}/sessions/{session_id}/stop`: request cooperative stop.

## Validation

Tests run without paid model calls. Scripted native-tool-calling models execute
the actual Deep Agents/LangGraph graph, verifying tool dispatch, SQLite history
across runtime instances, session isolation and cancellation. API tests cover
scope checks, Memory permissions, busy sessions, stop and invalid providers.
Canvas tests cover node creation, independent sending, Harness navigation and
Memory connection/disconnection. The workflow adapter test checks prompt/input
and recalled Memory propagation, declared outputs and isolated thread IDs.

Real provider completion and external tool side effects have not been exercised
as part of this implementation. Native tool support must be available on the
selected provider/model.

References:
- https://github.com/langchain-ai/deepagents
- https://docs.langchain.com/oss/python/deepagents/customization
- https://docs.langchain.com/oss/python/langgraph/persistence

### Chat agent Tools and Skills

Harness settings now exposes individual platform tools. `tools: null` (the default)
uses every available tool, including tools added later; `tools: []` disables all
platform tools. An explicit list is enforced both in discovery and execution.
Legacy `toolkits` filters are still honored; saving a new individual selection
clears the legacy filter. Planning and connected-memory permissions remain separate.

`skill_names` attaches Library skills as standing instructions on each turn.
Skill discovery/loading is restricted to that selection. Settings are persisted
with the agent and take effect on the next message, without changing an active
turn's configuration. Skill instructions do not expand tool permissions.
