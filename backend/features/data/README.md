# data 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `datasets.py` — Discover versioned credit-risk data and keep evaluation outcomes out of inputs.
- `user_datasets.py` — 用户数据集的上传、持久化、预览、重命名、删除和字段映射；独立于领域数据集与 LLM。
- `sources.py` — Batch input sources for EvoAgentX Studio.
- `source_apis.py` — API-backed canvas source nodes for EvoAgentX Studio.
- `source_collection.py` — Collect canvas inputs once, persist them, then run the saved records.
- `preprocess.py` — Input preprocessing for EvoAgentX Studio.

- `data_resources.py` — immutable uploaded files/folders, versions and lifecycle.
- `dataloaders.py` — reader/transform contracts, prepared-data cache and provenance.
- `input_composition.py` — per-record data plus shared references, available before Loader transforms.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在 `backend/llm/adapters`，不要写在业务函数中。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）

用户数据集的使用方法及格式见 [custom-datasets.md](../../../docs/custom-datasets.md)。

- `torch_loader.py` — Shared PyTorch Dataset/DataLoader adapter and isolated Python dataset factory; dictionary collation, no shuffle, no dropped tail. Input code is stored with the graph.

- `dataset_interface.py` — Static factory-argument discovery and runtime form-value validation; no model-provider dependencies.
