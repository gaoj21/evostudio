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
import os
import threading
import uuid
from typing import Any, Iterator

from llm import (
    LLMResult,
    ProviderError,
    achat_result,
    batch_result,
    chat_result,
    default_provider,
    get_provider,
    list_providers,
)

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
    return provider or os.getenv(PROVIDER_ENV) or default_provider()


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


def _report(key: str | None, result: LLMResult) -> LLMResult:
    if not key or result.usage is None:
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
            result = chat_result(self.provider, messages, **self._options(kwargs))
            return _report(self.usage_key, result).content

        def batch_generate(self, batch_messages: list[list[dict]], **kwargs) -> list[str]:
            results = batch_result(self.provider, batch_messages, **self._options(kwargs))
            return [_report(self.usage_key, result).content for result in results]

        async def single_generate_async(self, messages: list[dict], **kwargs) -> str:
            result = await achat_result(self.provider, messages, **self._options(kwargs))
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

        def _payload(self, messages) -> list[dict]:
            roles = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}
            out = []
            for message in messages:
                role = roles.get(getattr(message, "type", "human"), "user")
                content = message.content
                if not isinstance(content, str):
                    content = "\n".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in (content or []))
                out.append({"role": role, "content": content})
            return out

        def _as_result(self, result: LLMResult) -> ChatResult:
            usage = result.usage
            message = AIMessage(
                content=result.content,
                usage_metadata={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                    "input_token_details": {"cache_read": usage.cache_read_tokens},
                    "output_token_details": {"reasoning": usage.reasoning_tokens},
                } if usage else None,
                response_metadata={"model_name": result.model, "provider": result.provider},
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

        def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            result = chat_result(provider_name(self.provider), self._payload(messages),
                                 **({"stop": stop} if stop else {}))
            return self._as_result(_report(self.usage_key, result))

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            result = await achat_result(provider_name(self.provider), self._payload(messages),
                                        **({"stop": stop} if stop else {}))
            return self._as_result(_report(self.usage_key, result))

        def _stream(self, messages, stop=None, run_manager=None, **kwargs) -> Iterator:
            # The package has no streaming contract; one chunk keeps callers
            # that ask for a stream working.
            from langchain_core.outputs import ChatGenerationChunk
            from langchain_core.messages import AIMessageChunk

            result = self._generate(messages, stop=stop, **kwargs)
            message = result.generations[0].message
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=message.content, usage_metadata=message.usage_metadata))

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
