# workspace 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `projects.py` — Project organization for domain-independent tasks backed by executable graphs.
- `workspace.py` — The workspace: a workflow's project on disk.
- `workspace_api.py` — Workspace management API for EvoAgentX Studio.
- `project_import.py` — Import exported Studio projects back into canvas graphs.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在 `backend/llm/adapters`，不要写在业务函数中。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）

- `dataset_mounts.py` — Read-only Workspace views of uploaded Input/label resources, including absolute paths for Dataset code; associations include uploads before graph save.
