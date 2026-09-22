# library 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `skills_api.py` — Skills for EvoAgentX Studio.
- `custom_tools.py` — User-defined tools for EvoAgentX Studio: what counts as a toolkit, its schemas, storage and routes.
- `tool_runtime.py` — How a toolkit's module is loaded, its class built and one tool called. Standard library only; exported projects vendor it as `vendor/tool_runtime.py`.
- `tool_worker.py` — The isolated process a toolkit runs in (one JSON request per line).
- `tool_sessions.py` — Those processes: one per call, or one per run for a class toolkit (`scope()`, opened by `runner._execute_run_scoped`).
- `tools_registry.py` — Tool registry for EvoAgentX Studio. `toolkits()` = `BUILTIN_TOOLKITS`（通用框架工具）+ 项目插件 `toolkits()` 提供的工具（同名时插件覆盖内置）。领域工具（例如 credit_risk 的 `ObligorMatchToolkit`）不写进内置表，放在 `projects/<p>/studio_plugin.py`，见 [projects/README.md](../../../projects/README.md)。

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

## A toolkit is a module, a class, or an uploaded folder

Written the way a DataLoader is: a whole module with its imports, helpers and
module-level setup. How it exposes its tools is read out of the code
(`custom_tools.interface`), never declared twice:

| kind | what it is | tools | configuration |
| --- | --- | --- | --- |
| `function` | public top-level functions | each function | none |
| `class` | one class: the only public one, `TOOL_CLASS = <class>`, or what `build_tool(...)` returns | each public method | the constructor's (or `build_tool`'s) typed parameters |
| `factory` | the older `build_tool` + `INPUT_SCHEMA` / `run(inputs)` | one | `build_tool`'s parameters |

```python
"""A keyword index over the texts it has been given."""
import re


class KeywordIndex:
    def __init__(self, min_length: int = 3):     # the configuration form
        self.min_length, self.index = min_length, {}

    def add(self, text: str) -> dict:
        """Index a text.

        Args:
            text: the text to index
        """
        ...
```

- A class toolkit keeps **one instance per run**: `tool_sessions.scope()` (opened
  by the runner for each run, and by `/preview` for a `calls` list) holds one
  worker process per toolkit, so state set in one call is there in the next.
  Outside a run every call gets a fresh process, as it always has.
- A method's `self` is not a parameter; properties and `_private` methods are
  not tools; methods inherited from an installed library are not tools (only
  code in the toolkit's own files counts).
- A class the entry **imports** (`TOOL_CLASS = Client`, or a subclass of a
  sibling module's class) cannot be read statically: the module is then
  imported in an isolated worker to read it (`discovery: "runtime"`).
  `POST /api/tools/custom/interface` does that only with `execute: true`.
- Uploaded folders: a `.zip`, the folder itself or loose `.py` files
  (`POST /api/tools/custom/upload`, `check: true` reports what it would save
  and keeps nothing). The folder goes first on `sys.path` and the entry runs
  under its dotted name, so sibling and **relative** imports work. Editing the
  entry file in the UI is allowed: `POST /api/tools/custom` with
  `edit_entry: true` writes the code back into the entry file in the folder.
  Without that flag a code edit of a folder toolkit is still refused.
- Errors name the line of the author's own code and carry what it printed
  (`backend/features/user_code.py`): `Tool code line 6: ZeroDivisionError ...`,
  or `Tool code mylib/text.py line 2: ...` for a sibling module.
- The editor offers an example of each form (Library -> Custom tool -> Example).

## Python factory tools

`python_tool.py` implements the shared schema contract for ordinary tools.
Upload/paste in Library → Custom tool, confirm code to statically inspect it,
configure factory arguments, optionally run a JSON example, then save.

```python
"""Multiply numeric values."""
INPUT_SCHEMA = [{"name": "value", "type": "float"}]
OUTPUT_SCHEMA = [{"name": "total", "type": "float"}]

class Multiply:
    def __init__(self, factor):
        self.factor = factor

    def run(self, inputs):
        return {"total": inputs["value"] * self.factor}

def build_tool(factor: float = 2.0):
    return Multiply(factor)
```

- `build_tool` parameters are saved configuration. In this older contract each
  call constructs a fresh instance; instance state is not persisted between
  calls. A class toolkit (above) keeps one instance per run instead.
- `INPUT_SCHEMA` defines runtime arguments used by canvas mappings, Agent and
  Chat. Input fields need concrete types; OUTPUT_SCHEMA also accepts `any`.
- Both schemas are literal lists; empty INPUT_SCHEMA is allowed. Field keys are
  `name`, `type`, optional `required` and `nullable`. Types use Python spellings.
- Only one factory tool is exported under the saved toolkit name; helper names
  do not become tools. Use a module docstring as its description.
- All callers use the custom tool registry/subprocess path. Input/output types
  are checked at runtime. Canvas outputs use declared dictionary fields, even
  for a single output. No dependency installation occurs on confirmation.
- Configuration belongs to the saved tool and is shared by all callers. Save
  another named tool to use different configurations concurrently.
- Legacy function toolkits, built-in tools, Dataset factories and Evaluator
  factories retain their existing entrypoints and execution lifecycles.
- `POST /api/tools/custom/interface` inspects without execution; `/preview`
  explicitly executes a draft with `args` and optional `tool` for legacy
  multi-function toolkits, without persisting the draft. Successful examples
  are not a claim that the tool works for all possible inputs.
