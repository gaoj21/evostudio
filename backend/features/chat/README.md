# chat 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `chat_api.py` — Conversational workflow building for EvoAgentX Studio.
- `chat_engine.py` — Shared conversation/tool loop; pages supply context and operation adapters.
- `chat_control.py` — Cancellation shared by assistant requests, model workers and computation.
- `chat_worker.py` — Isolated blocking calls so Stop can terminate sockets and model retries.
- `result_chat.py` — Read-only result exploration with scoped, deterministic aggregation.
- `result_compute.py` — Run result-analysis Python in an OS sandbox, never in the API process.
- `model_json.py` — Reading a model's structured answer.
- `agent_api.py` — An OpenAI-compatible chat endpoint that turns Studio into a callable agent.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在仓库根目录的 `llm/adapters`，不要写在业务函数中；`backend/` 里不出现供应商名字或 SDK。框架模型类由 `backend/features/model_bridge.py` 提供。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）
