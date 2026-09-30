"""EAX_MEMORY_BACKEND=langgraph: long-term memory in a LangGraph SqliteStore
with a vector index — the same surface Studio uses for the framework store."""
import pytest

pytest.importorskip('langgraph.store.sqlite')
pytest.importorskip('sqlite_vec')


@pytest.fixture
def langgraph_backend(studio_data, monkeypatch):
    monkeypatch.setenv('EAX_MEMORY_BACKEND', 'langgraph')
    from backend.features.memory import memory_store
    return memory_store


def test_add_search_delete_and_list_through_studio(langgraph_backend):
    from evoagentx.core.message import Message
    store = langgraph_backend.open_memory('g', 'notes', create=True)
    ids = store.add([Message(content='Acme exchanged its senior notes in March', agent='notes'),
                     Message(content='Globex opened a new plant', agent='notes')])
    assert len(ids) == 2
    assert store.add([Message(content='Globex opened a new plant', agent='notes')]) == []   # deduplicated
    hits = langgraph_backend.search(store, 'notes exchange', n=1)
    assert 'Acme' in hits[0][0].content and hits[0][1] in ids                     # (Message, memory_id)
    assert store.delete([ids[1]]) == [True]
    store.save()
    langgraph_backend.close_memory(store)

    assert [e['content'][:4] for e in langgraph_backend.list_entries('g', 'notes')] == ['Acme']
    assert langgraph_backend.open_memory('g', 'absent') is None


def test_the_old_langchain_setting_says_what_replaced_it(studio_data, monkeypatch):
    monkeypatch.setenv('EAX_MEMORY_BACKEND', 'langchain')
    from backend.features.memory import memory_store
    with pytest.raises(ValueError, match='replaced by EAX_MEMORY_BACKEND=langgraph'):
        memory_store.open_memory('g', 'notes', create=True)
