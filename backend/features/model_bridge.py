"""Every model call Studio makes goes through the standalone `llm` package.

The package is supplied per machine and is not in the repository; its
contract is text in, `LLMResult` out (`docs/llm-contract.md`). Two
consumers need something else:

- the workflow engine builds its agents from an `LLMConfig` and instantiates
  the model class that config names, so a config alone decides where the
  calls go. `StudioLLM` is registered as that class: agents built from
  `StudioLLMConfig` call the package, and no framework model class,
  provider SDK or response shape is involved.
- the agent harness wants a LangChain chat model. `StudioChatModel` is one,
  and reports usage the way LangChain does (`usage_metadata`).

Token usage is not read from provider responses anywhere in the backend:
every call reports its `LLMResult.usage` to the hook registered for it
(`usage_hook`), which is what the run, the batch and the digest show.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from typing import Any, Iterator

# Only what docs/llm-contract.md guarantees. A machine's package may offer
# more (an async single call, a default-provider helper); this asks for none
# of it, because importing a name the contract does not promise is how a
# perfectly good package fails at start-up.
from llm import (  # noqa: F401
    LLMResult,
    ProviderError,
    abatch_result,
    batch_result,
    chat_result,
    get_provider,
    list_providers,
)

# Optional extras, used when the local package happens to have them.
try:                                    # a real async single call
    from llm import achat_result as _achat_result
except ImportError:
    _achat_result = None
try:                                    # the package's own default choice
    from llm import default_provider as _default_provider
except ImportError:
    _default_provider = None


__all__ = [
    "ProviderError",
    "StudioChatModel",
    "StudioLLM",
    "StudioLLMConfig",
    "agent_model",
    "list_providers",
    "provider_name",
    "provider_model",
    "usage_hook",
    "workflow_model",
]

# The provider a run uses. A deployment picks it once, in the environment or
# the package's providers.json; nothing in Studio names one.
PROVIDER_ENV = "EAX_PROVIDER"

_hooks: dict[str, Any] = {}
_lock = threading.Lock()


def provider_name(provider: str | None = None) -> str | None:
    """Which provider to call: what the caller named, what the environment
    says, the package's own default, else the one configured provider that
    can run. Studio never hard-codes a provider name."""
    if provider:
        return provider
    chosen = os.getenv(PROVIDER_ENV) or os.getenv("LLM_PROVIDER")
    if chosen:
        return chosen
    if _default_provider is not None:
        try:
            chosen = _default_provider()
        except Exception:
            chosen = None
        if chosen:
            return chosen
    try:
        configured = list_providers() or {}
    except Exception:
        return None
    usable = [name for name, config in configured.items()
              if isinstance(config, dict) and config.get("default")]
    usable = usable or [name for name, config in configured.items()
                        if isinstance(config, dict) and config.get("available")]
    usable = usable or list(configured)
    return usable[0] if len(usable) >= 1 else None


def provider_model(provider: str | None = None) -> str:
    """The model id the provider is configured with, for display and logs."""
    try:
        return get_provider(provider_name(provider)).get("model") or "unknown"
    except ProviderError:
        return "unavailable"


def usage_hook(callback) -> str:
    """Register a callback for the usage of calls made with this key.

    Returns the key to put on a config. A config travels into agents and back
    out of a subprocess, so it carries a key rather than the callable.
    """
    key = uuid.uuid4().hex[:12]
    with _lock:
        _hooks[key] = callback
    return key


def release_usage_hook(key: str | None) -> None:
    if not key:
        return
    with _lock:
        _hooks.pop(key, None)


def _call(provider: str | None, messages: list, options: dict) -> LLMResult:
    """One completion. Sampling options are passed when the package accepts
    them; a package whose signature is just (provider, messages) is equally
    valid under the contract."""
    if not options:
        return chat_result(provider, messages)
    try:
        return chat_result(provider, messages, **options)
    except TypeError as error:
        if "unexpected keyword" not in str(error):
            raise
        return chat_result(provider, messages)


async def _acall(provider: str | None, messages: list, options: dict) -> LLMResult:
    """The same, awaited. The contract promises `abatch_result`, not an async
    single call, so a batch of one is the portable form."""
    if _achat_result is not None:
        try:
            return await (_achat_result(provider, messages, **options) if options
                          else _achat_result(provider, messages))
        except TypeError as error:
            if "unexpected keyword" not in str(error):
                raise
            return await _achat_result(provider, messages)
    try:
        results = await (abatch_result(provider, [messages], **options) if options
                         else abatch_result(provider, [messages]))
    except TypeError as error:
        if "unexpected keyword" not in str(error):
            raise
        results = await abatch_result(provider, [messages])
    if not results:
        raise ProviderError("The provider returned no result for one request.")
    return results[0]


def _batch(provider: str | None, items: list, options: dict) -> list:
    if not options:
        return batch_result(provider, items)
    try:
        return batch_result(provider, items, **options)
    except TypeError as error:
        if "unexpected keyword" not in str(error):
            raise
        return batch_result(provider, items)


def _report(key: str | None, result: LLMResult) -> LLMResult:
    """Tell the key's hook about every call, reported usage or not: a call
    with `usage=None` is counted as unreported, never passed over."""
    if not key:
        return result
    with _lock:
        callback = _hooks.get(key)
    if callback is not None:
        try:
            callback(result)
        except Exception:
            pass          # accounting must never fail a model call
    return result


# ---------------------------------------------------------------------------
# The workflow engine's model
# ---------------------------------------------------------------------------

def _config_class():
    from evoagentx.models.model_configs import LLMConfig
    from pydantic import Field

    class StudioLLMConfig(LLMConfig):
        """What an agent needs to reach the package: a provider and a key."""

        llm_type: str = "StudioLLM"
        model: str = "studio"
        provider: str | None = Field(default=None, description="llm package provider name")
        usage_key: str | None = Field(default=None, description="key of the usage hook to report to")

    return StudioLLMConfig


def _model_class():
    from evoagentx.core.registry import register_model
    from evoagentx.models.base_model import BaseLLM

    config_class = _config_class()

    @register_model(config_class)
    class StudioLLM(BaseLLM):
        """A framework model whose every call is a call into `llm`."""

        def init_model(self):
            self.provider = provider_name(getattr(self.config, "provider", None))
            self.usage_key = getattr(self.config, "usage_key", None)

        def formulate_messages(self, prompts, system_messages=None) -> list[list[dict]]:
            system_messages = system_messages or [None] * len(prompts)
            out = []
            for prompt, system in zip(prompts, system_messages):
                messages = [{"role": "system", "content": system}] if system else []
                out.append(messages + [{"role": "user", "content": prompt}])
            return out

        def _options(self, kwargs: dict) -> dict:
            # The framework passes its own bookkeeping; only sampling settings
            # mean anything to a provider.
            allowed = ("temperature", "top_p", "max_tokens", "stop", "timeout", "seed")
            return {key: value for key, value in kwargs.items()
                    if key in allowed and value is not None}

        def single_generate(self, messages: list[dict], **kwargs) -> str:
            result = _call(self.provider, messages, self._options(kwargs))
            return _report(self.usage_key, result).content

        def batch_generate(self, batch_messages: list[list[dict]], **kwargs) -> list[str]:
            results = _batch(self.provider, batch_messages, self._options(kwargs))
            return [_report(self.usage_key, result).content for result in results]

        async def single_generate_async(self, messages: list[dict], **kwargs) -> str:
            result = await _acall(self.provider, messages, self._options(kwargs))
            return _report(self.usage_key, result).content

        def get_completion_cost(self, *args, **kwargs) -> float:
            return 0.0       # priced by llm.UsageTracker, not per response

    return StudioLLM, config_class


_workflow_classes = None


def classes():
    """(StudioLLM, StudioLLMConfig), built and registered once."""
    global _workflow_classes
    if _workflow_classes is None:
        _workflow_classes = _model_class()
    return _workflow_classes


def workflow_model(provider: str | None = None, usage_key: str | None = None):
    """A framework model for a run; agents cloned from its config use it too."""
    model_class, config_class = classes()
    name = provider_name(provider)
    return model_class(config=config_class(
        model=provider_model(name), provider=name, usage_key=usage_key))


# ---------------------------------------------------------------------------
# The harness's LangChain model
# ---------------------------------------------------------------------------

def _chat_model_class():
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class StudioChatModel(BaseChatModel):
        """LangChain's interface over the package, usage included."""

        provider: str | None = None
        usage_key: str | None = None
        model_name: str = "studio"

        @property
        def _llm_type(self) -> str:
            return "studio-llm"

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            """Tools for an agent. The contract has no tool channel, so they
            travel as text (text_tool_calls) — never as provider options."""
            from langchain_core.utils.function_calling import convert_to_openai_tool
            kwargs["tools"] = [convert_to_openai_tool(tool) for tool in tools]
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            return self.bind(**kwargs)

        def _payload(self, messages, tools=None, tool_choice=None) -> list[dict]:
            from backend.features import text_tool_calls
            roles = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}
            out = []
            for message in messages:
                spoken = text_tool_calls.as_text(message)
                if spoken is not None:
                    out.append({"role": spoken[0], "content": spoken[1]})
                    continue
                role = roles.get(getattr(message, "type", "human"), "user")
                content = message.content
                if not isinstance(content, str):
                    content = "\n".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in (content or []))
                out.append({"role": role, "content": content})
            if tools:
                guide = text_tool_calls.instruction(tools, tool_choice)
                if out and out[0]["role"] == "system":
                    out[0] = {"role": "system", "content": f"{out[0]['content']}\n\n{guide}"}
                else:
                    out.insert(0, {"role": "system", "content": guide})
            return out

        def _as_result(self, result: LLMResult, tools=None) -> ChatResult:
            from backend.features import text_tool_calls
            from backend.features.execution.token_usage import reported_usage
            # Read through the tolerant parser: a provider that reports no
            # cache or reasoning count must not fail LangChain's validation.
            usage = reported_usage(getattr(result, "usage", None))
            metadata = None
            if usage:
                metadata = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")}
                if "cache_read_tokens" in usage:
                    metadata["input_token_details"] = {"cache_read": usage["cache_read_tokens"]}
                if "reasoning_tokens" in usage:
                    metadata["output_token_details"] = {"reasoning": usage["reasoning_tokens"]}
            content, calls = (text_tool_calls.parse(result.content, tools) if tools
                              else (result.content, []))
            message = AIMessage(
                content=content, tool_calls=calls, usage_metadata=metadata,
                response_metadata={"model_name": getattr(result, "model", None),
                                   "provider": getattr(result, "provider", None)},
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

        def _generate(self, messages, stop=None, run_manager=None, tools=None,
                      tool_choice=None, **kwargs) -> ChatResult:
            result = _call(provider_name(self.provider), self._payload(messages, tools, tool_choice),
                           {"stop": stop} if stop else {})
            return self._as_result(_report(self.usage_key, result), tools)

        async def _agenerate(self, messages, stop=None, run_manager=None, tools=None,
                             tool_choice=None, **kwargs) -> ChatResult:
            result = await _acall(provider_name(self.provider), self._payload(messages, tools, tool_choice),
                                  {"stop": stop} if stop else {})
            return self._as_result(_report(self.usage_key, result), tools)

        def _stream(self, messages, stop=None, run_manager=None, **kwargs) -> Iterator:
            # The package has no streaming contract; one chunk keeps callers
            # that ask for a stream working.
            from langchain_core.outputs import ChatGenerationChunk
            from langchain_core.messages import AIMessageChunk

            result = self._generate(messages, stop=stop, **kwargs)
            message = result.generations[0].message
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=message.content, usage_metadata=message.usage_metadata,
                tool_call_chunks=[{"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"],
                                   "index": i, "type": "tool_call_chunk"}
                                  for i, c in enumerate(message.tool_calls)]))

    return StudioChatModel


_chat_model = None


def agent_model(provider: str | None = None, usage_key: str | None = None):
    """A LangChain chat model for the agent harness."""
    global _chat_model
    if _chat_model is None:
        _chat_model = _chat_model_class()
    name = provider_name(provider)
    return _chat_model(provider=name, usage_key=usage_key, model_name=provider_model(name))


def run_async(coroutine):
    """Await a package coroutine from synchronous code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise RuntimeError("run_async called from a running event loop; await the coroutine instead")
