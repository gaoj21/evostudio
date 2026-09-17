# library 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `skills_api.py` — Skills for EvoAgentX Studio.
- `custom_tools.py` — User-defined tools for EvoAgentX Studio.
- `tools_registry.py` — Tool registry for EvoAgentX Studio.

- `data_evaluation_tools.py` — DataLoader and Evaluator exposure through the ordinary tool registry.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在 `backend/llm/adapters`，不要写在业务函数中。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）

## Chat-generated Tool verification

`tool_verification.py` installs explicitly declared `requirements` into a versioned per-tool directory under the runtime tools directory. Studio's Python environment is not modified. Import names are not guessed as package names. Generated code should declare tests as `{"tool":"function_name","args":{...},"expected":...}` for every exported function. Tests execute through the same subprocess runtime as workflow calls; declared dependencies are also available during later execution.

Canvas Chat automatically verifies after creating a tool and receives the report for corrections. `verify_tool` or `POST /api/tools/custom/{name}/verify` reruns saved tests. Reports distinguish `verified` (all exported functions have passing expected-result examples), `failed`, and `unverified` (missing assertions or missing function coverage). Verification covers supplied examples, not all inputs. Chat Stop cancels its verification worker and child processes. Missing credentials/resources must be reported instead of fabricated. Custom code retains the existing local-user execution permissions.
