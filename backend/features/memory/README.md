# memory 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `mem0_api.py` — UI-facing Mem0 operations, scoped using the saved graph's project.
- `mem0_service.py` — Project-scoped Mem0 OSS spaces. Raw writes; local embeddings by default.
- `memory_api.py` — Memory browsing API for EvoAgentX Studio.
- `memory_policy.py` — What each node keeps in its long-term memory.
- `memory_reset.py` — Back memory up, then empty it — the step before a measured run.
- `memory_resources.py` — Shared identity and access rules for canvas and chat Memory resources.
- `memory_store.py` — Studio layout over the memory layer (memory/ltm.py).
- `stm_store.py` — Short-term memory for EvoAgentX Studio: what happened earlier in a session.
- `table_store.py` — Table memory: one row per subject per date.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在 `backend/llm/adapters`，不要写在业务函数中。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）
