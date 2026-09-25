"""A model call that timed out or was turned away is tried again, and a batch
record that still could not get through is re-run — whatever the provider,
and whatever status a gateway puts on its own timeout."""
from types import SimpleNamespace

import pytest

from backend.features import model_bridge

DEADLINE = "Error code: 400 - {'error': 'Model invocation failed. Reason: context deadline exceeded'}"


class ProviderError(Exception):
    pass


def test_what_counts_as_transient():
    assert model_bridge.is_transient(ProviderError(DEADLINE))
    wrapped = RuntimeError('action failed')
    wrapped.__cause__ = ProviderError(DEADLINE)
    assert model_bridge.is_transient(wrapped)                 # found down the chain
    Overloaded = type('Overloaded', (Exception,), {'status_code': 503})
    assert model_bridge.is_transient(Overloaded('no wording, just a status'))
    assert model_bridge.is_transient(ProviderError('Request timed out.'))
    assert not model_bridge.is_transient(ProviderError("Error code: 400 - {'error': 'invalid parameter: messages'}"))
    assert not model_bridge.is_transient(ValueError('Expected a JSON object'))
    assert not model_bridge.is_transient(None)


@pytest.fixture
def no_pause(monkeypatch):
    monkeypatch.setattr(model_bridge, 'TRANSIENT_RETRY_DELAYS', (0, 0))


def test_the_bridge_tries_a_timed_out_call_again(no_pause, monkeypatch):
    attempts = []

    def chat_result(provider, messages, **options):
        attempts.append(1)
        if len(attempts) < 3:
            raise ProviderError(DEADLINE)
        return SimpleNamespace(content='ok', usage=None, provider=provider, model='m')
    monkeypatch.setattr(model_bridge, 'chat_result', chat_result)

    assert model_bridge._call('p', [{'role': 'user', 'content': 'hi'}], {}).content == 'ok'
    assert len(attempts) == 3


def test_it_gives_up_after_its_tries_and_never_retries_a_bad_request(no_pause, monkeypatch):
    attempts = []

    def failing(message):
        def chat_result(provider, messages, **options):
            attempts.append(1)
            raise ProviderError(message)
        return chat_result
    monkeypatch.setattr(model_bridge, 'chat_result', failing(DEADLINE))
    with pytest.raises(ProviderError):
        model_bridge._call('p', [], {})
    assert len(attempts) == 1 + len(model_bridge.TRANSIENT_RETRY_DELAYS)

    attempts.clear()
    monkeypatch.setattr(model_bridge, 'chat_result', failing("Error code: 400 - {'error': 'bad messages'}"))
    with pytest.raises(ProviderError):
        model_bridge._call('p', [], {})
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_the_async_path_retries_too(no_pause, monkeypatch):
    attempts = []

    async def abatch_result(provider, items, **options):
        attempts.append(1)
        if len(attempts) < 2:
            raise ProviderError(DEADLINE)
        return [SimpleNamespace(content='ok', usage=None, provider=provider, model='m')]
    monkeypatch.setattr(model_bridge, '_achat_result', None)
    monkeypatch.setattr(model_bridge, 'abatch_result', abatch_result)
    assert (await model_bridge._acall('p', [], {})).content == 'ok'
    assert len(attempts) == 2


def test_a_node_that_timed_out_is_marked_for_the_batch_to_run_again():
    from backend.features.workflow import runner
    error = RuntimeError('agent failed')
    error.__cause__ = ProviderError(DEADLINE)
    assert 'deadline exceeded' in runner._unreachable(error)
    assert runner._unreachable(RuntimeError('Expected a JSON object')) is None
