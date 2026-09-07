# llm layer

Pluggable LLM providers. Multiple providers can be registered at once; each
may use a different calling convention.

## Files

- `providers.json` — provider registry. Keys live in the repo-root `.env`,
  never in this file (`api_key_env` names the env var). Entries with
  `"enabled": false` are documented examples.
- `registry.py` — loads/validates provider configs, resolves keys.
- `client.py` — `chat(provider, messages, **kwargs) -> str`, dispatching on
  provider `type` (`litellm` → litellm SDK; `openai_compatible` → HTTP
  /chat/completions). Shared 429/5xx retry + timeout.
- `factory.py` — `get_evoagentx_llm(provider=None)` builds a framework
  `LiteLLM` instance for EvoAgentX / Studio / credit_risk.

## Adding a provider

1. Add an entry to `providers.json`: name, `type` (`litellm` or
   `openai_compatible`), `model`, `base_url` (openai_compatible only),
   `api_key_env`, optional `params` (e.g. `{"timeout": 120}`).
2. Put the key in `.env` under that env var name.
3. For `litellm` type the key is passed as `<model_prefix>_key`
   (e.g. `deepseek/...` → `deepseek_key`), falling back to `api_key`.
   A brand-new calling convention = a new `type` plus a handler in
   `client.py`'s `_DISPATCH` and a branch in `factory.py`.
