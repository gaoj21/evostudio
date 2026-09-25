# The LLM transport contract (`llm-v12`)

**This package is not in the repository.** Every machine supplies its own
`llm/` at the repo root — DeepSeek-backed on one, SafeChain-backed on
another — and `backend`, `frontend` and the data travel unchanged between
them. What must match is the interface below. `.gitignore` excludes `/llm/`;
this file is the specification, and a package's own tests live inside it
(`llm/tests/`, run with `.venv/bin/python -m pytest llm/tests`).

Studio adapts these results to the workflow engine and the agent harness in
exactly one place, `backend/features/model_bridge.py`, which is tracked and
tested (`tests/llm/test_model_bridge.py`). Nothing else in the backend may
import a provider SDK, name a provider, or read a provider's response.

Every model call the project makes goes through this package. It is a
standalone package at the repository root: it imports nothing from `backend/`
or `frontend/`, and it is the only place a provider SDK, an endpoint URL or a
provider's response shape appears.

**The rule, in one line:** nothing outside `llm/` may name a provider, import
a provider SDK, or read a provider response. Business code says *what* to send
and whether it wants text, an `LLMResult` or usage — never *where* the call
goes.

That rule is what makes the same checkout run against a different transport
elsewhere. On the user's other machine this package is SafeChain-backed and its
provider is called `safechain`; the backend, the frontend, the tests and an
exported project are byte-for-byte the same, because none of them knows the
name.

## Public API

```python
from llm import (
    chat, chat_result,            # one completion: text, or text + usage
    batch, batch_result,          # many completions, in the order given
    achat, achat_result,          # the same, awaited
    abatch, abatch_result,
    LLMResult, LLMUsage,          # what a call returns
    UsageTracker,                 # totals and what they cost
    list_providers, get_provider, default_provider,
    ProviderError,                # every failure this package raises
)
```

Nothing else in the package is public. `llm.client`, `llm.registry`,
`llm.types`, `llm.usage` and `llm.adapters` are where it is implemented, and
importing from them from outside couples a caller to an implementation detail
that a SafeChain machine does not have.

### Calls

```python
text    = chat(None, [{"role": "user", "content": "hello"}])
result  = chat_result(None, messages, temperature=0.2)
results = batch_result(None, [messages_a, messages_b])      # order preserved
```

The first argument is the provider name, or `None` for the configured default.
Keyword options are sampling settings (`temperature`, `top_p`, `max_tokens`,
`stop`, `timeout`, `seed`); the provider's own `params` from `providers.json`
are the floor they override.

`batch` uses the provider's real batch endpoint when it has one, and otherwise
sends the requests in parallel, bounded by `client.MAX_PARALLEL`. Either way
the results come back in the order the items were given, and a native endpoint
that returns the wrong number of results is a `ProviderError` rather than a
silent misalignment.

Retries cover rate limits and 5xx only (`RETRY_BACKOFF`, two backoffs). A bad
request is raised as it is, so a malformed call does not wait out three
backoffs before telling you.

### Results and usage

```python
@dataclass
class LLMResult:
    content: str
    usage: LLMUsage | None       # None means the provider reported none
    provider: str | None
    model: str | None
```

`usage` is `None` when the provider reported nothing. It is never a row of
zeroes: "this call used no tokens" and "we were told nothing" price
differently, and a zero hides the second inside every total downstream.
`LLMUsage` carries `input_tokens`, `output_tokens`, `total_tokens`,
`cache_read_tokens`, `reasoning_tokens` and the derived
`uncached_input_tokens`, whatever names the provider used for them
(`prompt_tokens`/`completion_tokens` included).

`UsageTracker` adds results up and prices them. Prices are per million tokens
and come from the environment, so a deployment prices its own contract without
a code change: `EVO_TOKEN_INPUT_PRICE_PER_1M`,
`EVO_TOKEN_OUTPUT_PRICE_PER_1M`, `EVO_TOKEN_CACHED_INPUT_RATIO`,
`EVO_WEB_SEARCH_PRICE_PER_1K`. `add()` returns `False` for a result with no
usage, so an uncounted call is visible rather than counted as free.

## Providers

`llm/providers.json` is the whole configuration. Secrets never live in it —
each provider names the environment variables it needs, and those are read
from the process environment and the repository `.env`.

```json
{
  "default": "deepseek",
  "providers": {
    "deepseek": {
      "type": "litellm",
      "model": "deepseek/deepseek-v4-flash",
      "api_key_env": "DEEPSEEK_API_KEY",
      "params": {"timeout": 120}
    }
  }
}
```

- `type` selects a built-in transport: `litellm`, or `openai_compatible` for
  any OpenAI-shaped `/chat/completions` host (which also needs `base_url`).
- `adapter` names a module instead, for a transport of your own.
- `api_key_env`, or `requires: [...]` for a provider that needs several
  variables.
- `enabled: false` marks a documented example. It is refused, not attempted.

**To switch provider**, do one of two things — and nothing else:

1. Change `default` in `providers.json`, or
2. set `LLM_PROVIDER` in the environment. Studio also honours `EAX_PROVIDER`,
   which takes precedence.

`list_providers()` reports every configured provider with `available` and
`missing_env`, which is how the UI shows an operator what is not set up.
`available` means the variables it needs are present — not that the next
request will succeed.

### What changes on a SafeChain machine

Only `llm/providers.json` and that provider's environment variables. The
provider is named `safechain` and its transport lives in `llm/adapters/`. No
file under `backend/`, `frontend/`, `tests/` or `projects/` differs, and an
export produced on either machine runs on either machine.

## Failures

`ProviderError` is the one exception type this package raises for anything a
person can fix: an unknown provider (it names the ones that exist), a disabled
one, a missing environment variable (it names the variable), a call the
provider's transport does not support, and retries exhausted. Import it from
`llm`, never from `llm.registry`.

## Smoke test

```bash
set -a; . ./.env; set +a          # the provider's key
.venv/bin/python llm/examples_terminal.py
```

That makes real, billed calls against the configured provider and prints what
came back, including usage. It is the one thing to run after editing
`providers.json` or moving to a machine with a different transport. The test
suite never calls a provider: `tests/llm/` patches the transport.

## How Studio reaches it

`backend/features/model_bridge.py` is the only adapter between this package
and a framework, and it is a Studio file, not part of this package:

- `workflow_model()` — a framework `BaseLLM` (`StudioLLM`) registered so that
  an agent rebuilt from its config alone, in another process, still calls this
  package.
- `agent_model()` — a LangChain `BaseChatModel` with `usage_metadata` filled
  from `LLMResult.usage`, for the Deep Agents harness. It supports
  `bind_tools` without needing anything from the package: the contract has no
  tool channel, so tool schemas go into a system instruction and the model's
  `{"tool_calls": [...]}` reply is read back as LangChain tool calls
  (`backend/features/text_tool_calls.py`). Tools are never passed to
  `chat_result` as options.
- `usage_hook(callback) -> key` / `release_usage_hook(key)` — where a run's
  token accounting comes from. A config carries the key, not the callable, so
  it survives a trip through a subprocess.

If a consumer needs something this package does not offer, the bridge is where
it goes. Do not widen a caller's imports into `llm.*` internals to get it.
