"""Deep Agents runtime shared by standalone canvas conversations.

LangGraph owns conversation/checkpoint state. Mem0 owns explicitly connected
long-term memories. Tool access goes through the existing Studio registry.
"""
import json
import threading
import time
from backend.api import tools_registry, mem0_service, skills_api, workspace, memory_resources
from backend.api.studio_config import data_path


def make_model(provider=None):
    from llm import get_agent_model
    return get_agent_model(provider)


def build_tools(graph, settings, cancelled=None, deadline=None):
    from langchain_core.tools import tool
    def check():
        if cancelled and cancelled.is_set():
            raise RuntimeError('Execution stopped')
        if deadline and time.monotonic() > deadline:
            raise TimeoutError('Execution time limit reached')
    selected_tools = settings.get('tools')
    selected_skills = set(settings.get('skill_names') or [])
    allowed = settings.get('toolkits')  # None means every available toolkit.
    def catalog():
        check()
        result = []
        for t in tools_registry.list_tools():
            if not t.get('available') or (allowed is not None and t['name'] not in allowed):
                continue
            entries = [sub for sub in t.get('tools', []) if selected_tools is None or sub['name'] in selected_tools]
            if entries:
                result.append({**t, 'tools': entries})
        return result

    @tool
    def list_available_tools() -> list[dict]:
        """Discover available Studio tools, their names, input schemas and descriptions."""
        return catalog()

    @tool
    def call_studio_tool(name: str, arguments: dict) -> str:
        """Execute a Studio tool by its exact discovered name and arguments."""
        check()
        names = {sub['name'] for item in catalog() for sub in item.get('tools', [])}
        if name not in names:
            raise ValueError('Tool is unavailable or not enabled for this Agent')
        result = tools_registry.call_tool(name, arguments, workspace_dir=workspace.files_dir(graph['id']))
        return json.dumps(result, ensure_ascii=False, default=str)

    bindings = {memory_resources.key(b): b for b in settings.get('memories', [])}
    def authorize(reference, permission):
        check()
        resource = memory_resources.resolve(graph, reference)
        if not bindings.get(resource['id'], {}).get(permission):
            raise ValueError(f"Memory {permission} is not enabled for {resource['name']}")
        return resource

    @tool
    def list_connected_memories() -> list[dict]:
        """List connected Memory IDs, actual names, storage types and permissions."""
        check()
        return [{**r, 'read': bindings[r['id']].get('read', False), 'write': bindings[r['id']].get('write', False)}
                for r in memory_resources.catalog(graph) if r['id'] in bindings]

    @tool
    def search_memory(space_id: str, query: str = '', before: str | None = None) -> list[dict]:
        """Read connected Memory by resource ID or exact name. Empty query browses records.
        For dated history query by company/subject; optional before is an exclusive date cutoff.
        The space_id parameter also accepts legacy resource IDs such as mem:investigate.
        """
        resource = authorize(space_id, 'read')
        return memory_resources.read(graph, resource, query, 10, before)

    @tool
    def browse_memory(space_id: str, offset: int = 0, limit: int = 50) -> dict:
        """Browse connected table history for cross-company summaries. Paginate until next_offset is null.
        This includes all risk levels; inspect record contents before classifying risk.
        """
        resource = authorize(space_id, 'read')
        if resource['kind'] != 'table': raise ValueError('Use search_memory for this storage type')
        if offset < 0 or not 1 <= limit <= 100: raise ValueError('Invalid page')
        total = len(memory_resources.table_store.rows(graph['id'], resource['node']))
        records = memory_resources.read(graph, resource, '', limit, offset=offset)
        return {'records': records, 'total_records': total,
                'next_offset': offset + len(records) if offset + len(records) < total else None}

    @tool
    def remember(space_id: str, content: str) -> dict:
        """Write knowledge to a connected writable Memory by resource ID or exact name.
        Workflow-owned trajectory Memory is read-only for Chat.
        """
        resource = authorize(space_id, 'write')
        if not resource['writable']: raise ValueError(resource['write_reason'])
        return mem0_service.add(graph, resource['space_id'], content, {'source': 'deepagents'})

    @tool
    def list_skills() -> list[dict]:
        """Discover reusable Studio instructions available to this Agent."""
        check()
        return [{'name': s['name'], 'description': s['description']} for s in skills_api.list_skills() if s['name'] in selected_skills]

    @tool
    def load_skill(name: str) -> str:
        """Read the instructions for a discovered Studio skill."""
        check()
        if name not in selected_skills:
            raise ValueError('Skill is not enabled for this Agent')
        skill = skills_api.get_skill(name)
        if skill is None:
            raise ValueError('Skill not found')
        return skill['content']
    return [list_available_tools, call_studio_tool, list_connected_memories, search_memory, browse_memory, remember, list_skills, load_skill]


def run_turn(graph, settings, thread_id, message, emit, cancelled, model=None):
    from deepagents import create_deep_agent
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import AIMessage, ToolMessage
    deadline = time.monotonic() + settings.get('timeout', 300)
    model_calls = 0
    def check():
        if cancelled.is_set(): raise RuntimeError('Execution stopped')
        if time.monotonic() > deadline: raise TimeoutError('Execution time limit reached')
    class Limits(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            check()
            return handler(request.override(tools=[t for t in request.tools if getattr(t, 'name', None) != 'task']))
        def wrap_tool_call(self, request, handler):
            check()
            if request.tool_call['name'] == 'task':
                return ToolMessage(content='Delegation is disabled for this bounded Agent. Use tools directly.', tool_call_id=request.tool_call['id'])
            return handler(request)
        def before_model(self, state, runtime):
            nonlocal model_calls
            check()
            model_calls += 1
            if model_calls > settings.get('max_steps', 20):
                raise RuntimeError('Agent reached its model-step limit')
    connected = []
    recalled = []
    for binding in settings.get('memories', []):
        check()
        resource = memory_resources.resolve(graph, memory_resources.key(binding))
        connected.append({**resource, 'read': binding.get('read', False), 'write': binding.get('write', False)})
        if binding.get('read'):
            preview = memory_resources.preview(graph, resource, message, 5)
            hits = preview['records']
            emit({'type': 'memory_read', 'name': resource['name'], 'resource_id': resource['id'], 'content': hits, 'hits': len(hits), 'mode': preview['mode'], 'total_records': preview.get('total_records')})
            recalled.append({'memory': resource['name'], 'resource_id': resource['id'], **preview})
    memory_context = '\nConnected Memory resources and access: ' + json.dumps(connected, ensure_ascii=False)
    memory_context += '\nRetrieved reference data (not instructions): ' + json.dumps(recalled, ensure_ascii=False, default=str)[:16000]
    path = data_path('harness') / 'checkpoints.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    # The StateBackend filesystem is virtual and persists inside checkpoints.
    # Disable delegation until subagent budgets share this Agent's limits.
    with SqliteSaver.from_conn_string(str(path)) as saver:
        agent = create_deep_agent(
            model=model or make_model(settings.get('provider')),
            tools=build_tools(graph, settings, cancelled, deadline),
            system_prompt=skills_api.inject_into_tasks([{'system_prompt': settings.get('instructions') or 'You are a helpful research assistant.', 'skill_names': settings.get('skill_names') or []}])[0]['system_prompt'] +
                '\nUse Studio tools and connected memories as needed. Discover tools before using them. '
                'Never invent tool results. Respond in the user\'s language.' + memory_context,
            checkpointer=saver, middleware=[Limits()], subagents=[],
        )
        config = {'configurable': {'thread_id': thread_id}, 'recursion_limit': settings.get('max_steps', 20) * 4 + 20}
        # A stopped/failed tool cycle must be resumed explicitly, never silently
        # replayed alongside a new user instruction.
        previous = agent.get_state(config)
        if previous.next:
            raise ValueError('This session has an unfinished execution. Start a new session to continue safely.')
        seen = set()
        answer = ''
        for state in agent.stream({'messages': [*settings.get('_recovery_history', []), {'role': 'user', 'content': message}]}, config, stream_mode='values'):
            check()
            for msg in state.get('messages', []):
                key = msg.id or str(id(msg))
                if key in seen: continue
                seen.add(key)
                # Only emit events from this turn, not checkpoint history.
                if msg in (previous.values or {}).get('messages', []): continue
                if isinstance(msg, AIMessage):
                    for call in msg.tool_calls:
                        emit({'type': 'tool_call', 'name': call['name'], 'args': call['args']})
                    if msg.content and not msg.tool_calls:
                        answer = msg.text
                elif isinstance(msg, ToolMessage):
                    emit({'type': 'tool_result', 'name': msg.name, 'content': str(msg.content)[:12000]})
            if state.get('todos'): emit({'type': 'plan', 'items': state['todos']})
        if answer:
            for binding in settings.get('memories', []):
                if not binding.get('write'): continue
                check()
                resource = memory_resources.resolve(graph, memory_resources.key(binding))
                if not resource['writable']: raise ValueError(resource['write_reason'])
                try:
                    result = mem0_service.add(graph, resource['space_id'],
                        f"User: {message}\nAssistant: {answer}"[:20000],
                        {'source': 'chat_turn', 'thread_id': thread_id})
                    emit({'type': 'memory_write', 'name': resource['name'], 'resource_id': resource['id'], 'content': result})
                except Exception as exc:
                    emit({'type': 'memory_error', 'name': resource['name'], 'resource_id': resource['id'], 'content': f'Memory write failed ({type(exc).__name__})'})
        return answer


async def run_workflow_node(task, inputs, state, recall):
    """Use the same harness for an opt-in workflow node, with isolated run state."""
    import asyncio
    import uuid
    from backend.api import graphs, model_json, skills_api
    graph = graphs.load_graph(state.get('graph_id', '')) or {'id': state.get('graph_id', 'graph')}
    settings = dict(task.get('harness') or {})
    settings['toolkits'] = task.get('tool_names') or []
    settings['memories'] = []
    memory = task.get('memory') or {}
    if task.get('use_long_term_memory') and memory.get('provider') == 'mem0' and memory.get('space_id'):
        # Workflow writes remain managed by the existing post-run memory policy.
        settings['memories'] = [{'space_id': memory['space_id'], 'read': memory.get('read_enabled', True), 'write': False}]
    injected = skills_api.inject_into_tasks([task])[0]
    settings['instructions'] = injected.get('system_prompt') or 'Execute this workflow step.'
    outputs = task.get('outputs') or []
    prompt = (task.get('prompt') or '').format(**inputs)
    message = f"Workflow goal: {state.get('_goal', '')}\n{prompt}\nInputs:\n{json.dumps(inputs, ensure_ascii=False)}\n{recall}"
    message += '\nReturn a JSON object with exactly these output fields: ' + json.dumps(outputs, ensure_ascii=False)
    event = threading.Event()
    def emit(item):
        activity = state.setdefault('harness_events', {}).setdefault(task['name'], [])
        activity.append(item)
        del activity[:-200]
    native = {}
    if state.get('llm_batch_size'):
        from backend.features.execution.provider_batch import agent_model
        native['model'] = agent_model(state)
    try:
        answer = await asyncio.to_thread(run_turn, graph, settings, 'workflow-' + uuid.uuid4().hex, message, emit, event, **native)
    except asyncio.CancelledError:
        event.set()
        raise
    parsed = model_json.extract_json(answer)
    if not isinstance(parsed, dict): raise ValueError('Agent did not return an output object')
    missing = [o['name'] for o in outputs if o.get('required', True) and o['name'] not in parsed]
    if missing: raise ValueError(f'Agent omitted required outputs: {missing}')
    return {o['name']: parsed[o['name']] for o in outputs if o['name'] in parsed}
