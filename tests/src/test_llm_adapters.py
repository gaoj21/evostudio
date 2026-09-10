"""Provider protocol changes must not require edits in feature code."""
import sys
from types import ModuleType
from unittest.mock import Mock
import pytest
from llm import client, factory, agent
from llm.adapters import capability
from llm.registry import ProviderError


def test_custom_protocol_dispatches_all_three_entry_points(monkeypatch):
    plugin = ModuleType('test_vendor_adapter')
    plugin.chat = Mock(return_value='answer')
    workflow = object(); deep_agent = object()
    plugin.create_workflow_model = Mock(return_value=workflow)
    plugin.create_agent_model = Mock(return_value=deep_agent)
    monkeypatch.setitem(sys.modules, plugin.__name__, plugin)
    config = {'name':'custom', 'type':'custom', 'adapter':plugin.__name__, 'model':'private-model', 'api_key':'test-only'}
    for module in [client, factory, agent]:
        monkeypatch.setattr(module, 'get_provider', lambda name=None: config)
    assert client.chat(None, [{'role':'user','content':'hello'}], timeout=2) == 'answer'
    assert factory.get_evoagentx_llm() is workflow
    assert agent.get_agent_model() is deep_agent
    plugin.chat.assert_called_once_with(config, [{'role':'user','content':'hello'}], {'timeout':2})


def test_missing_protocol_capability_does_not_fallback(monkeypatch):
    plugin = ModuleType('incomplete_vendor')
    monkeypatch.setitem(sys.modules, plugin.__name__, plugin)
    with pytest.raises(ProviderError, match='create_agent_model'):
        capability({'adapter':plugin.__name__}, 'create_agent_model')


def test_legacy_import_is_same_module_state():
    from backend.api import saved_result_evolution as legacy
    from backend.features.evaluation import saved_result_evolution as feature
    assert legacy is feature
