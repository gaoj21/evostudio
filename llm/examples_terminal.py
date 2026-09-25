"""Smoke test against the configured provider: python llm/examples_terminal.py

Makes a handful of real requests, so it costs a little. It prints usage for
every result API; `None` usage means the provider reported none.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm import (  # noqa: E402
    UsageTracker, abatch_result, batch, batch_result, chat, chat_result,
    default_provider, list_providers,
)

PROVIDER = sys.argv[1] if len(sys.argv) > 1 else default_provider()
ONE = [{"role": "user", "content": "Reply with exactly: hello"}]
MANY = [[{"role": "user", "content": "Reply with exactly: one"}],
        [{"role": "user", "content": "Reply with exactly: two"}]]


def main() -> int:
    print("=== 0) providers ===")
    for name, config in list_providers().items():
        print(f"{name}: available={config['available']} model={config.get('model')}"
              + (f" missing={config['missing_env']}" if config["missing_env"] else ""))
    print(f"using: {PROVIDER}\n")

    print("=== 1) chat() -> str ===")
    print("content:", chat(PROVIDER, ONE), "\n")

    print("=== 2) chat_result() -> LLMResult + usage ===")
    result = chat_result(PROVIDER, ONE)
    print("content:", result.content)
    print("usage:", result.usage.as_dict() if result.usage else None)
    print("provider/model:", result.provider, result.model, "\n")

    print("=== 3) batch() -> list[str] ===")
    print(batch(PROVIDER, MANY), "\n")

    print("=== 4) batch_result() + UsageTracker ===")
    tracker = UsageTracker()
    for item in batch_result(PROVIDER, MANY):
        print(item.content, "|", item.usage.as_dict() if item.usage else None)
        tracker.add(item)
    print("batch stats:", tracker.snapshot(), "\n")

    print("=== 5) abatch_result() ===")
    for item in asyncio.run(abatch_result(PROVIDER, MANY)):
        print(item.content, "|", item.usage.as_dict() if item.usage else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
