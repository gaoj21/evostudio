"""Studio request coalescing in front of the `llm` package's batch call.

Requests are coalesced into `llm.batch_result`, which returns one
`LLMResult` per input in input order. Studio reads `.content` and `.usage`
off those results and nothing else: no provider is named here (the bridge
says which one a deployment uses) and no provider or framework response
shape is parsed.
"""
import asyncio
import inspect
import json
import threading
from concurrent.futures import Future

FLUSH_SECONDS = 0.05
_lock = threading.Lock()
_pending = {}
# Buckets handed to ``llm.batch`` and not yet answered, per batch id. The API
# takes a list and returns its results, so there is no job to call back; what
# a stop can do is stop waiting for them, and say what that costs.
_in_flight: dict[str, list] = {}
# Batches the user stopped. Prompts waiting to be coalesced are the last place
# new model calls can come from after a Stop: the flush timer fired regardless
# of the batch's state, and records still winding down were allowed to queue
# more. Both are refused for an id in here.
_cancelled = set()


def _provider():
    """The provider this deployment uses, as the bridge reports it."""
    from backend.features import model_bridge
    return model_bridge.provider_name()


class BatchCancelled(Exception):
    """Raised for a request the user's Stop reached before the provider did."""


def cancel(batch_id):
    """Stop coalescing for a batch: drop what is queued, accept nothing new,
    and stop waiting on what is already with the provider.

    Requests already handed to ``llm.batch`` cannot be recalled by Studio —
    the API takes a list of inputs and returns their results, with no job
    handle — unless the layer offers a cancel of its own (``llm.cancel_batch``),
    which is used when it is there. Either way the records waiting on them are
    released at once rather than holding the batch in `cancelling` until the
    provider answers, and the result says how many were already sent, because
    those may still be billed.
    """
    if not batch_id:
        return {"dropped": 0, "in_flight": 0}
    dropped = 0
    with _lock:
        _cancelled.add(batch_id)
        # Only buckets still waiting for their flush timer. A detached one is
        # already on its way to the provider and is owned by its dispatch
        # thread; its futures are released below instead.
        waiting = [(key, bucket) for key, bucket in _pending.items()
                   if isinstance(key[0], str) and key[0] == batch_id]
        for key, bucket in waiting:
            del _pending[key]
            bucket['timer'].cancel()
            for _value, future in bucket['requests']:
                if future.cancel():
                    dropped += 1
        sent = list(_in_flight.get(batch_id) or ())
    in_flight = 0
    for bucket in sent:
        for _value, future in bucket['requests']:
            if not future.done():
                in_flight += 1
                # The dispatch thread checks `done()` before it answers, so a
                # result that does arrive is discarded rather than clashing.
                future.set_exception(BatchCancelled(
                    'This batch was stopped while this request was with the provider; '
                    'its answer is not waited for.'))
    outcome = {"dropped": dropped, "in_flight": in_flight}
    if in_flight:
        outcome["note"] = (f'{in_flight} request(s) were already sent to the provider and '
                           f'may still be billed; nothing waits for their answers.')
        cancel_at_provider = _provider_cancel()
        if cancel_at_provider is not None:
            try:
                cancel_at_provider(_provider())
                outcome["provider_cancelled"] = True
            except Exception as exc:
                outcome["provider_cancelled"] = False
                outcome["note"] += f' The provider refused the cancel: {exc}.'
        else:
            outcome["provider_cancelled"] = False
    return outcome


def _provider_cancel():
    """The llm layer's own batch cancel, if it has one.

    ``llm.batch_result`` returns results rather than a job, so there is
    nothing to cancel through it. A layer that does support cancelling exposes
    ``cancel_batch(provider)``; Studio uses it when it is there and says
    plainly that it is not when it is not.
    """
    import llm
    function = getattr(llm, 'cancel_batch', None)
    return function if callable(function) else None


def uncancel(batch_id):
    """Accept this batch's requests again — it was resumed."""
    with _lock:
        _cancelled.discard(batch_id)


def batch_size(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 1024:
        raise ValueError('API batch size must be a whole number between 1 and 1024.')
    return int(value)


def batch_function():
    import llm
    function = getattr(llm, 'batch_result', None)
    if not callable(function):
        raise ValueError('Native API batching is not connected yet: llm.batch_result(provider, inputs) is required.')
    return function


def _accepts_tools(function) -> bool:
    """Whether the package's batch call takes tool schemas at all.

    The contract is text in, `LLMResult` out — no tool-call channel — so a
    package that has not declared a `tools` parameter cannot run a workflow
    whose nodes call tools, and saying so beats sending the prompts without
    their tools.
    """
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return 'tools' in parameters


def validate(graph, value):
    size = batch_size(value)
    if size is not None:
        function = batch_function()
        if any((t.get('harness') or {}).get('engine') == 'deepagents' or t.get('tool_names')
               for t in graph.get('tasks', [])) and not _accepts_tools(function):
            raise ValueError('This workflow uses tools. Native batching requires llm.batch_result to '
                             'accept tools; use Standard execution with the current adapter.')
    return size


def options(state):
    size = batch_size(state.get('llm_batch_size'))
    return {'batch_size': size, 'batch_id': state.get('batch_id')} if size is not None else {}


def _dispatch(key, bucket):
    batch_id = key[0][0] if isinstance(key[0], tuple) else key[0]
    with _lock:
        if _pending.get(key) is not bucket:
            return
        del _pending[key]
        bucket['timer'].cancel()
        requests = [(value, future) for value, future in bucket['requests']
                    if future.set_running_or_notify_cancel()]
        if requests:
            # Reachable by a stop for as long as the provider has them.
            _in_flight.setdefault(batch_id, []).append(bucket)
    if not requests:
        return
    try:
        function = batch_function()
        kwargs = bucket['kwargs']
        provider = _provider()
        # Never silently discard tool schemas or generation options.
        try:
            inspect.signature(function).bind(provider, [v for v, _ in requests], **kwargs)
        except TypeError as exc:
            raise ValueError('llm.batch_result must accept the requested model options: '
                             + ', '.join(sorted(kwargs))) from exc
        results = function(provider, [v for v, _ in requests], **kwargs)
        if inspect.isawaitable(results):
            results = asyncio.run(results)
        if not isinstance(results, (list, tuple)) or len(results) != len(requests):
            raise ValueError('llm.batch_result must return one result per input, in input order.')
        for (_, future), result in zip(requests, results):
            # A stop may already have released this request: the answer came
            # too late to be waited for, and is dropped rather than set.
            if future.done():
                continue
            if isinstance(result, Exception):
                future.set_exception(result)
            else:
                future.set_result(result)
    except Exception as exc:
        for _, future in requests:
            if not future.done():
                future.set_exception(exc)
    finally:
        with _lock:
            waiting = [held for held in (_in_flight.get(batch_id) or []) if held is not bucket]
            if waiting:
                _in_flight[batch_id] = waiting
            else:
                _in_flight.pop(batch_id, None)


def submit(messages, state, **kwargs):
    # Framework-only cache hint is not a SafeChain generation option.
    kwargs.pop('enable_prompt_caching', None)
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    if kwargs.get('tools') == []:
        kwargs.pop('tools')
    size = batch_size(state.get('llm_batch_size'))
    if size is None or not state.get('batch_id'):
        raise ValueError('Native batching requires a batch size and batch ID.')
    batch_function()
    key = (state['batch_id'], size, json.dumps(kwargs, sort_keys=True))
    future = Future()
    with _lock:
        if state['batch_id'] in _cancelled:
            raise BatchCancelled('This batch was stopped; no further requests are sent.')
        bucket = _pending.get(key)
        if bucket is None:
            bucket = {'requests': [], 'kwargs': kwargs}
            timer = threading.Timer(FLUSH_SECONDS, _dispatch, args=(key, bucket))
            timer.daemon = True
            bucket['timer'] = timer
            _pending[key] = bucket
            timer.start()
        bucket['requests'].append((messages, future))
        if len(bucket['requests']) >= size:
            # Detach immediately: new arrivals cannot exceed this batch's limit.
            del _pending[key]
            bucket['timer'].cancel()
            dispatch_key = (key, id(bucket))
            _pending[dispatch_key] = bucket
            worker = threading.Thread(target=_dispatch, args=(dispatch_key, bucket), daemon=True)
            worker.start()
    return future


def _text(result):
    """The completion's text, from an `LLMResult` or a plain string."""
    if isinstance(result, str):
        return result
    content = getattr(result, 'content', None)
    if isinstance(content, str):
        return content
    raise ValueError('Expected a string or an llm.LLMResult from llm.batch_result.')


def _usage(result):
    """What the package reported for this call, if it reported anything."""
    from .token_usage import reported_usage
    usage = getattr(result, 'usage', None)
    return reported_usage(usage) if usage is not None else None


def attach_workflow_model(model, state):
    """Keep the framework formatter/parser, replace only its transport methods."""
    validate({}, state.get('llm_batch_size'))
    from .token_usage import record
    def counted(value):
        # Coalesced calls bypass the model's own path, so the run's usage hook
        # never sees them: count what each result reports as it arrives.
        record(state, _usage(value))
        return _text(value)
    def many(batch_messages, **kwargs):
        futures = [submit(messages, state, **kwargs) for messages in batch_messages]
        return [counted(f.result()) for f in futures]
    async def many_async(batch_messages, **kwargs):
        futures = [asyncio.wrap_future(submit(messages, state, **kwargs)) for messages in batch_messages]
        return [counted(value) for value in await asyncio.gather(*futures)]
    def single(messages, **kwargs):
        return many([messages], **kwargs)[0]
    async def single_async(messages, **kwargs):
        return (await many_async([messages], **kwargs))[0]
    model.single_generate = single
    model.single_generate_async = single_async
    model.batch_generate = many
    model.batch_generate_async = many_async
    return model


def _payload(messages):
    """LangChain messages as the package's `{'role', 'content'}` dicts."""
    roles = {'human': 'user', 'ai': 'assistant', 'system': 'system', 'tool': 'tool'}
    payload = []
    for message in messages:
        if isinstance(message, dict):
            payload.append(message)
            continue
        content = message.content
        if not isinstance(content, str):
            content = '\n'.join(part.get('text', '') if isinstance(part, dict) else str(part)
                                for part in (content or []))
        payload.append({'role': roles.get(getattr(message, 'type', 'human'), 'user'),
                        'content': content})
    return payload


def agent_model(state):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from .token_usage import record

    class NativeBatchChatModel(BaseChatModel):
        @property
        def _llm_type(self):
            return 'studio-safechain-batch'

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            kwargs['tools'] = [convert_to_openai_tool(tool) for tool in tools]
            if tool_choice is not None:
                kwargs['tool_choice'] = tool_choice
            return self.bind(**kwargs)

        def _result(self, value):
            """A LangChain reply built from what the package returned."""
            usage = _usage(value)
            record(state, usage)
            message = AIMessage(
                content=_text(value),
                usage_metadata={'input_tokens': usage['input_tokens'],
                                'output_tokens': usage['output_tokens'],
                                'total_tokens': usage['total_tokens']} if usage else None)
            return ChatResult(generations=[ChatGeneration(message=message)])

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if stop is not None:
                kwargs['stop'] = stop
            return self._result(submit(_payload(messages), state, **kwargs).result())

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if stop is not None:
                kwargs['stop'] = stop
            return self._result(await asyncio.wrap_future(submit(_payload(messages), state, **kwargs)))

    return NativeBatchChatModel()
