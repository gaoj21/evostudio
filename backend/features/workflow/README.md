# workflow 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `graphs.py` — Graph CRUD for EvoAgentX Studio. 保存时用 `review.validate_rule` 校验 `graph["review"]`（审核规则），无效规则返回 422。
- `runner.py` — Run engine for EvoAgentX Studio.
- `run_plan.py` — The execution plan: one immutable description of what a run will do. For an Input type that declares a `sequence`, `n` counts trajectories, so the plan reports the record count as unknown (`cardinality: null`).

平台级插件加载器在 `backend/features/plugins.py`（不属于本目录）：`projects/<p>/studio_plugin.py` 提供的 presets、templates、toolkits、Input 类型经它进入 palette、模板、工具表和 `/api/sources`。加载失败只在 `/api/features` 的 `projects` 中报告，不影响其余功能。契约见 [projects/README.md](../../../projects/README.md)。

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在仓库根目录的 `llm/adapters`，不要写在业务函数中；`backend/` 里不出现供应商名字或 SDK。框架模型类由 `backend/features/model_bridge.py` 提供。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）
