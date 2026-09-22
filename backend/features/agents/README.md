# agents 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `harness.py` — Deep Agents runtime shared by standalone canvas conversations.
- `harness_api.py` — Durable, independently runnable canvas Agents and chat sessions.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在 `backend/llm/adapters`，不要写在业务函数中。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）

## Usage and adapter diagnostics

Canvas Chat requires `llm.get_agent_model(provider)` to return a LangChain chat model supporting tool calling; a workflow-only or `batch()` adapter is not sufficient. Missing imports are reported with the backend Python path, while arbitrary provider exceptions remain redacted.

Provider usage from new AI messages is emitted as `token_usage` events and persisted as session totals and per-turn totals. Restored checkpoint history is excluded. Missing usage is unknown, not zero. Workflow harness nodes reuse these events. Standard workflow models are observed through their existing `_update_cost` response hook by `features/execution/token_usage.py`; adapters without this hook or usage metadata cannot provide counts. No model adapter source is modified. This is token reporting, not a price/billing estimate, and does not account for hidden provider retries or embedding calls without reported usage.
