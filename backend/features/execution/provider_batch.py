"""Studio request coalescing around the company-owned ``llm.batch`` API."""
import asyncio
import inspect
import json
import threading
from concurrent.futures import Future

PROVIDER = 'safechain'
FLUSH_SECONDS = 0.05
_lock = threading.Lock()
_pending = {}


def batch_size(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 1024:
        raise ValueError('API batch size must be a whole number between 1 and 1024.')
    return int(value)


def batch_function():
    import llm
    function = getattr(llm, 'batch', None)
    if not callable(function):
        raise ValueError('Native API batching is not connected yet: llm.batch(provider, inputs) is required.')
    return function


def validate(graph, value):
    size = batch_size(value)
    if size is not None:
        batch_function()
    return size


def options(state):
    size = batch_size(state.get('llm_batch_size'))
    return {'batch_size': size, 'batch_id': state.get('batch_id')} if size is not None else {}


def _dispatch(key, bucket):
    with _lock:
        if _pending.get(key) is not bucket:
            return
        del _pending[key]
        bucket['timer'].cancel()
        requests = [(value, future) for value, future in bucket['requests']
                    if future.set_running_or_notify_cancel()]
    if not requests:
        return
    try:
        function = batch_function()
        kwargs = bucket['kwargs']
        # Never silently discard tool schemas or generation options.
        try:
            inspect.signature(function).bind(PROVIDER, [v for v, _ in requests], **kwargs)
        except TypeError as exc:
            raise ValueError('llm.batch must accept the requested model options: ' + ', '.join(sorted(kwargs))) from exc
        results = function(PROVIDER, [v for v, _ in requests], **kwargs)
        if inspect.isawaitable(results):
            results = asyncio.run(results)
        if not isinstance(results, (list, tuple)) or len(results) != len(requests):
            raise ValueError('llm.batch must return one result per input, in input order.')
        for (_, future), result in zip(requests, results):
            if isinstance(result, Exception):
                future.set_exception(result)
            else:
                future.set_result(result)
    except Exception as exc:
        for _, future in requests:
            if not future.done():
                future.set_exception(exc)


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
    if isinstance(result, str):
        return result
    from langchain_core.messages import AIMessage
    if isinstance(result, AIMessage):
        if isinstance(result.content, str):
            text = result.content
        else:
            text = ''.join(block['text'] for block in result.content
                           if isinstance(block, dict) and block.get('type') == 'text')
        if result.tool_calls:
            calls = [{'id': call.get('id'), 'function_name': call['name'], 'function_args': call['args']}
                     for call in result.tool_calls]
            text += '\n<tool_call>' + json.dumps(calls, ensure_ascii=False) + '</tool_call>'
        return text
    raise ValueError('Expected a string or LangChain AIMessage from llm.batch.')


def attach_workflow_model(model, state):
    """Keep the framework formatter/parser, replace only its transport methods."""
    validate({}, state.get('llm_batch_size'))
    def many(batch_messages, **kwargs):
        futures = [submit(messages, state, **kwargs) for messages in batch_messages]
        return [_text(f.result()) for f in futures]
    async def many_async(batch_messages, **kwargs):
        futures = [asyncio.wrap_future(submit(messages, state, **kwargs)) for messages in batch_messages]
        return [_text(value) for value in await asyncio.gather(*futures)]
    def single(messages, **kwargs):
        return many([messages], **kwargs)[0]
    async def single_async(messages, **kwargs):
        return (await many_async([messages], **kwargs))[0]
    model.single_generate = single
    model.single_generate_async = single_async
    model.batch_generate = many
    model.batch_generate_async = many_async
    return model


def agent_model(state):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.utils.function_calling import convert_to_openai_tool

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
            if isinstance(value, str):
                value = AIMessage(content=value)
            if not isinstance(value, AIMessage):
                raise ValueError('Deep Agents require AIMessage or string results from llm.batch.')
            return ChatResult(generations=[ChatGeneration(message=value)])

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if stop is not None:
                kwargs['stop'] = stop
            return self._result(submit(messages, state, **kwargs).result())

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if stop is not None:
                kwargs['stop'] = stop
            return self._result(await asyncio.wrap_future(submit(messages, state, **kwargs)))

    return NativeBatchChatModel()
