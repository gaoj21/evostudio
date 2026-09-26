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
async def test_the_async_path_retries_too_and_runs_off_the_event_loop(no_pause, monkeypatch):
    import threading
    attempts, threads = [], []

    def chat_result(provider, messages, **options):
        attempts.append(1)
        threads.append(threading.current_thread())
        if len(attempts) < 2:
            raise ProviderError(DEADLINE)
        return SimpleNamespace(content='ok', usage=None, provider=provider, model='m')
    monkeypatch.setattr(model_bridge, 'chat_result', chat_result)
    assert (await model_bridge._acall('p', [], {})).content == 'ok'
    assert len(attempts) == 2
    assert threading.main_thread() not in threads          # a blocking SDK never holds the loop


def test_a_node_that_timed_out_is_marked_for_the_batch_to_run_again():
    from backend.features.workflow import runner
    error = RuntimeError('agent failed')
    error.__cause__ = ProviderError(DEADLINE)
    assert 'deadline exceeded' in runner._unreachable(error)
    assert runner._unreachable(RuntimeError('Expected a JSON object')) is None


def test_a_stop_ends_the_retries_at_once(monkeypatch):
    """A stopped record makes no further model call, and does not wait out
    the pause: the stop reaches the retry through the run's control."""
    import threading
    import time
    from backend.features.chat import chat_control
    monkeypatch.setattr(model_bridge, 'TRANSIENT_RETRY_DELAYS', (30, 30))
    attempts = []

    def chat_result(provider, messages, **options):
        attempts.append(1)
        raise ProviderError(DEADLINE)
    monkeypatch.setattr(model_bridge, 'chat_result', chat_result)
    control = chat_control.Control()
    token = chat_control.current.set(control)
    try:
        threading.Timer(0.2, control.event.set).start()
        started = time.monotonic()
        with pytest.raises(chat_control.Cancelled):
            model_bridge._call('p', [], {})
        assert time.monotonic() - started < 5
    finally:
        chat_control.current.reset(token)
    assert len(attempts) == 1


def test_a_blocked_model_call_is_given_up_the_moment_it_is_stopped(monkeypatch):
    """An SDK that blocks for minutes: a Stop returns now, not when it answers."""
    import threading
    import time
    from backend.features.chat import chat_control
    release = threading.Event()

    def chat_result(provider, messages, **options):
        release.wait(30)
        return SimpleNamespace(content='late', usage=None, provider=provider, model='m')
    monkeypatch.setattr(model_bridge, 'chat_result', chat_result)
    control = chat_control.Control()
    token = chat_control.current.set(control)
    try:
        threading.Timer(0.3, control.event.set).start()
        started = time.monotonic()
        with pytest.raises(chat_control.Cancelled):
            model_bridge._call('p', [], {})
        assert time.monotonic() - started < 3
    finally:
        chat_control.current.reset(token)
        release.set()
