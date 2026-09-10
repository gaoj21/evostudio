import threading
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from backend.api import harness

class ScriptedModel(BaseChatModel):
    replies: list
    calls: list = []
    @property
    def _llm_type(self): return 'scripted'
    def bind_tools(self, tools, **kwargs): return self
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])

@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, 'data_path', lambda *p: tmp_path.joinpath(*p))
    monkeypatch.setattr(harness.tools_registry, 'list_tools', lambda: [{'name': 'Test', 'available': True, 'tools': [{'name': 'double', 'inputs': {'n': {'type': 'int'}}}]}])
    monkeypatch.setattr(harness.tools_registry, 'call_tool', lambda name, args, **kw: args['n'] * 2)
    monkeypatch.setattr(harness.workspace, 'files_dir', lambda g: tmp_path)
    return {'id': 'test'}

def test_real_deepagents_tool_loop_and_sqlite_history(local):
    events = []
    model = ScriptedModel(replies=[AIMessage(content='', tool_calls=[{'name': 'call_studio_tool', 'args': {'name': 'double', 'arguments': {'n': 4}}, 'id': 'call-1'}]), AIMessage(content='8')])
    assert harness.run_turn(local, {}, 'session-1', 'double four', events.append, threading.Event(), model=model) == '8'
    assert any(e['type'] == 'tool_result' and '8' in e['content'] for e in events)
    second = ScriptedModel(replies=[AIMessage(content='You asked to double four')])
    harness.run_turn(local, {}, 'session-1', 'what did I ask?', events.append, threading.Event(), model=second)
    assert any(m.content == 'double four' for m in second.calls[0])
    other = ScriptedModel(replies=[AIMessage(content='new conversation')])
    harness.run_turn(local, {}, 'session-2', 'hello', events.append, threading.Event(), model=other)
    assert not any(m.content == 'double four' for m in other.calls[0])

def test_memory_permissions_and_tool_allowlist(local, monkeypatch):
    monkeypatch.setattr(harness.mem0_service, 'spaces', lambda g: [{'id': 'a' * 32, 'name': 'Facts'}])
    monkeypatch.setattr(harness.mem0_service, 'entries', lambda *a: [{'memory': 'fact'}])
    settings = {'toolkits': [], 'memories': [{'space_id': 'a' * 32, 'read': True, 'write': False}]}
    tools = {t.name: t for t in harness.build_tools(local, settings)}
    assert tools['search_memory'].invoke({'space_id': 'a' * 32, 'query': 'fact'}) == [{'memory': 'fact'}]
    with pytest.raises(ValueError): tools['remember'].invoke({'space_id': 'a' * 32, 'content': 'fact'})
    with pytest.raises(ValueError): tools['search_memory'].invoke({'space_id': 'b' * 32, 'query': 'fact'})
    with pytest.raises(ValueError): tools['call_studio_tool'].invoke({'name': 'double', 'arguments': {'n': 4}})

def test_cancel_prevents_model_call(local):
    event = threading.Event(); event.set()
    model = ScriptedModel(replies=[AIMessage(content='should not run')])
    with pytest.raises(RuntimeError, match='stopped'):
        harness.run_turn(local, {}, 'cancelled', 'hello', lambda e: None, event, model=model)
    assert not model.calls

@pytest.mark.asyncio
async def test_workflow_adapter_preserves_inputs_memory_and_declared_outputs(local, monkeypatch):
    from backend.api import graphs
    monkeypatch.setattr(graphs, 'load_graph', lambda id: local)
    captured = []
    def turn(graph, settings, thread_id, message, emit, cancelled):
        captured.append((settings, message, thread_id))
        return '{"result": "done", "extra": "discard"}'
    monkeypatch.setattr(harness, 'run_turn', turn)
    task = {'name': 'investigate', 'prompt': 'Research {company}', 'harness': {'engine': 'deepagents', 'max_steps': 5}, 'outputs': [{'name': 'result'}], 'tool_names': ['Test']}
    assert await harness.run_workflow_node(task, {'company': 'Example'}, {'graph_id': 'test'}, 'prior evidence') == {'result': 'done'}
    assert 'Research Example' in captured[0][1] and 'prior evidence' in captured[0][1]
    assert captured[0][0]['toolkits'] == ['Test']
    await harness.run_workflow_node(task, {'company': 'Other'}, {'graph_id': 'test'}, '')
    assert captured[0][2] != captured[1][2]

def test_actual_legacy_history_is_recalled_by_name_and_shared_turn_is_written(local, monkeypatch, tmp_path):
    from backend.api import table_store, memory_resources
    monkeypatch.setattr(table_store, 'TABLES_DIR', tmp_path / 'tables')
    graph = {**local, 'tasks': [{'name': 'investigate', 'use_long_term_memory': True, 'memory': {'match': 'company', 'at': 'as_of'}}]}
    table_store.upsert('test', 'investigate', 'Sleep Number', '2025-12-18', {'context': 'known risk evidence'}, '2025-12-19')
    monkeypatch.setattr(harness.mem0_service, 'spaces', lambda g: [{'id': 'a' * 32, 'name': 'Team findings'}])
    writes = []
    monkeypatch.setattr(harness.mem0_service, 'add', lambda *args: writes.append(args) or {'stored': True})
    settings = {'memories': [{'memory_id': 'mem:investigate', 'read': True, 'write': False}, {'space_id': 'a' * 32, 'read': False, 'write': True}]}
    tools = {t.name: t for t in harness.build_tools(graph, settings)}
    rows = tools['search_memory'].invoke({'space_id': 'investigate memory', 'query': 'Sleep Number'})
    assert rows[0]['content']['context'] == 'known risk evidence'
    assert tools['search_memory'].invoke({'space_id': 'mem:investigate', 'query': 'Sleep Number', 'before': '2025-12-18'}) == []
    model = ScriptedModel(replies=[AIMessage(content='Answer based on history')])
    events = []
    harness.run_turn(graph, settings, 'memory-chat', 'What happened to Sleep Number?', events.append, threading.Event(), model=model)
    assert 'known risk evidence' in str(model.calls[0][0].content)
    assert any(e['type'] == 'memory_read' and e['name'] == 'investigate memory' and e['hits'] == 1 for e in events)
    assert any(e['type'] == 'memory_write' and e['name'] == 'Team findings' for e in events)
    assert len(writes) == 1 and writes[0][1] == 'a' * 32
    assert table_store.rows('test', 'investigate')[0]['payload']['context'] == 'known risk evidence'
    disconnected = {'memories': []}
    tools = {t.name: t for t in harness.build_tools(graph, disconnected)}
    with pytest.raises(ValueError): tools['search_memory'].invoke({'space_id': 'mem:investigate', 'query': ''})

def test_overview_recall_and_paginated_table_access(local, monkeypatch, tmp_path):
    from backend.api import table_store, memory_resources
    monkeypatch.setattr(table_store, 'TABLES_DIR', tmp_path / 'tables')
    graph = {**local, 'tasks': [{'name': 'decide', 'use_long_term_memory': True, 'memory': {'match': 'company'}}]}
    for i in range(12):
        table_store.upsert('test', 'decide', f'Company {i}', '2026-01-01', {'risk_level': 'high'}, '2026-01-02')
    settings = {'memories': [{'memory_id': 'mem:decide', 'read': True}]}
    events = []
    model = ScriptedModel(replies=[AIMessage(content='Overview')])
    harness.run_turn(graph, settings, 'overview', '帮我看下有哪些公司出现了risk', events.append, threading.Event(), model=model)
    assert events[0]['hits'] == 5 and events[0]['total_records'] == 12
    assert events[0]['mode'] == 'browse'
    assert 'Preview only' in model.calls[0][0].content
    tool = {t.name: t for t in harness.build_tools(graph, settings)}['browse_memory']
    page = tool.invoke({'space_id': 'mem:decide', 'limit': 7})
    last = tool.invoke({'space_id': 'mem:decide', 'offset': page['next_offset'], 'limit': 7})
    assert len(page['records']) + len(last['records']) == 12
    assert last['next_offset'] is None
    assert memory_resources.read(graph, memory_resources.resolve(graph, 'mem:decide'), 'Unknown Company') == []


def test_recovered_conversation_keeps_history_without_replaying_failed_tools(local):
    broken = ScriptedModel(replies=[])
    with pytest.raises(IndexError):
        harness.run_turn(local, {}, 'broken', 'failed question', lambda e: None, threading.Event(), model=broken)
    model = ScriptedModel(replies=[AIMessage(content='Recovered')])
    history = [{'role': 'user', 'content': 'Earlier successful question'}, {'role': 'assistant', 'content': 'Earlier answer'}]
    harness.run_turn(local, {'_recovery_history': history}, 'recovered', 'Try again', lambda e: None, threading.Event(), model=model)
    assert any(m.content == 'Earlier answer' for m in model.calls[0])
    followup = ScriptedModel(replies=[AIMessage(content='Follow-up')])
    harness.run_turn(local, {}, 'recovered', 'Continue', lambda e: None, threading.Event(), model=followup)
    assert any(m.content == 'Recovered' for m in followup.calls[0])
    assert sum(m.content == 'Earlier answer' for m in followup.calls[0]) == 1


def test_individual_tools_and_skills_are_enforced(local, monkeypatch):
    monkeypatch.setattr(harness.skills_api, 'list_skills', lambda: [{'name':'analysis', 'description':'Analyze', 'content':'Check evidence'}, {'name':'other', 'description':'Other', 'content':'Other instructions'}])
    monkeypatch.setattr(harness.skills_api, 'get_skill', lambda name: {'name':name,'content':'Check evidence'})
    tools = {t.name:t for t in harness.build_tools(local, {'tools':[], 'skill_names':['analysis']})}
    assert tools['list_available_tools'].invoke({}) == []
    with pytest.raises(ValueError): tools['call_studio_tool'].invoke({'name':'double','arguments':{'n':2}})
    assert tools['list_skills'].invoke({}) == [{'name':'analysis','description':'Analyze'}]
    assert tools['load_skill'].invoke({'name':'analysis'}) == 'Check evidence'
    with pytest.raises(ValueError): tools['load_skill'].invoke({'name':'other'})
    enabled = {t.name:t for t in harness.build_tools(local, {'tools':['double']})}
    assert enabled['call_studio_tool'].invoke({'name':'double','arguments':{'n':2}}) == '4'
    default = {t.name:t for t in harness.build_tools(local, {})}
    assert default['call_studio_tool'].invoke({'name':'double','arguments':{'n':2}}) == '4'


def test_selected_skill_is_in_model_instructions(local, monkeypatch):
    monkeypatch.setattr(harness.skills_api, 'list_skills', lambda: [{'name':'analysis','description':'Analyze','content':'Always distinguish evidence from guesses.'}])
    model = ScriptedModel(replies=[AIMessage(content='done')])
    harness.run_turn(local, {'skill_names':['analysis']}, 'skill-test', 'hello', lambda e: None, threading.Event(), model=model)
    assert any('Always distinguish evidence from guesses.' in str(m.content) for m in model.calls[0])
