# 给以后维护者的入口

项目仍分为 `backend`、`frontend`、`projects`。先按功能找目录，再修改功能内的文件。
不要把整个项目一次性交给对话 LLM；优先提供本页、功能目录的 README，以及该功能需要改的源文件。

## 目录与职责

| 功能 | 后端实现 | 前端实现 |
| --- | --- | --- |
| 更换模型 API、鉴权、请求/返回格式 | `llm/`（仓库根目录的独立包，**不在版本控制内**，每台机器自备） | 通常不改 |
| workflow 图、字段映射、节点执行 | `backend/features/workflow/` | `frontend/src/features/canvas/` |
| Run、Batch、停止、调度、结果 | `backend/features/execution/` | `frontend/src/features/execution/` |
| 画布/结果聊天、会话、计算 | `backend/features/chat/` | `frontend/src/features/chat/` |
| 独立 Chat agent、Tools/Skills 挂载 | `backend/features/agents/` | `frontend/src/features/agents/` |
| Memory、Mem0、权限、读写与连接 | `backend/features/memory/` | `frontend/src/features/memory/` |
| Evaluation、Evolve、数据划分筛选 | `backend/features/evaluation/` | `frontend/src/features/evaluation/` |
| 数据来源、采集、清洗、数据集读取 | `backend/features/data/` | Feed 配置在 canvas，运行选择在 execution |
| Tools、Skills、Library | `backend/features/library/` | `frontend/src/features/library/` |
| Projects、任务文件、工作区 | `backend/features/workspace/` | `frontend/src/features/workspace/` |
| 任务专用的 Input、预设、模板、工具（项目插件） | `projects/<p>/studio_plugin.py`，加载器 `backend/features/plugins.py` | 不改；经 palette、`/api/sources` 等通用接口显示 |

`projects/credit_risk` 是项目资料、数据和领域代码，不是平台通用能力所在目录。冻结的数据集不应跟着代码重构改变。
`backend/evoagentx` 是框架源码，暂时保留原目录；通常不要为页面功能去修改它。

## 项目插件

Studio 是通用平台。只对某个任务有意义的代码（领域数据集 Input、预设节点、模板、审核规则、查询工具、评测代码）放在 `projects/<p>/`，通过 `projects/<p>/studio_plugin.py` 接入；加载器是 `backend/features/plugins.py`。契约（`presets()`、`templates()`、`toolkits()`、`source_types()` 及 `records` / `info` / `sequence` / `watch_key` 钩子）见 [projects/README.md](projects/README.md)，完整示例是 `projects/credit_risk/studio_plugin.py`。

- 任务专用代码不得写进 `backend/features/`、`backend/api/` 或 `frontend/src/` 的通用组件；平台代码和默认值不得依赖任务字段名（不按 company、as_of、sample_id 等名称分组、排序或推断含义；需要时由 Input 类型或用户配置声明）。
- 平台需要新能力时，先在 `plugins.py` 中以不含任务名的方式扩展契约，再由插件使用。
- 插件加载失败不影响平台，只在 `GET /api/features` 的 `projects` 中显示原因。`EAX_STUDIO_PROJECTS` 可替换插件搜索路径；测试中改完环境变量后调用 `plugins.reload()`。

## DataLoader 与 Evaluator

输入资源、Loader 预处理、画布评测节点及 evaluator-driven Evolve 的接口见 [dataloaders-and-evaluators.md](docs/dataloaders-and-evaluators.md)。不要在 Run 或 Evolve 中另写一套数据解析、评分逻辑。DataLoader 管数据准备，执行器管运行控制，Evaluator 管评分；新功能不需要修改模型适配层。

## 共享边界

- `backend/api/app.py`：启动与 HTTP 路由装配，也仍保留部分公共路由。功能逻辑在 features。
- `backend/api/studio_config.py`：数据路径配置。不要把运行数据迁入源代码目录。
- `backend/api/*.py` 中带 **Compatibility import** 的文件只是旧导入路径的兼容门面；真正实现路径写在文件顶部。不要在门面里加业务代码，也不要复制实现回去。它们共享同一个 Python 模块对象，不是两份状态。
- 为兼容现有插件和测试，后端功能之间目前仍可通过 `backend.api` 门面导入。功能并非完全解耦的微服务。
- `backend/api/export_api.py`、`export_templates.py`、`registry.py`：导出和平台装配。修改导出时要检查打包后的独立运行。
- `frontend/src/api.js`：前端 HTTP 客户端；修改接口时同时提供此文件和后端路由。
- `frontend/src/App.jsx`、`Platform.jsx`、`TaskDetail.jsx`：页面装配与路由状态。App 仍较大；只有新增入口/跨模块状态才提供它，不要为了局部组件修改让模型重写整个 App。
- `frontend/src/components`：共享控件及外壳组件。`styles.css`、`platform.css` 保持集中，以保留现有样式覆盖顺序。

## 更换 LLM API：最重要的一组文件

模型调用只有一个边界：仓库根目录的独立包 `llm/`。该包不随仓库分发（`.gitignore` 排除 `/llm/`）：本机是 DeepSeek 实现，另一台是 SafeChain 实现，两边只需接口一致，`backend`、`frontend` 和数据可以直接互换。契约见 [docs/llm-contract.md](docs/llm-contract.md)；包自带的测试放在包内 `llm/tests/`，用 `$PY -m pytest llm/tests` 跑（$PY 见「验证与启动」）。
**`backend/` 和 `frontend/` 里不允许出现供应商名字、供应商 SDK 的 import，或读取供应商返回结构的代码。**
旧的 `backend/llm/`（`get_evoagentx_llm` / `get_agent_model` / `llm.registry` / `llm.adapters`）已删除。

公开入口只有这些，全部从顶层 `llm` 导入：

1. `chat(provider, messages, **kwargs) -> str` / `chat_result(...) -> LLMResult`：普通对话请求，后者带 token usage。
2. `batch` / `batch_result`，以及 `achat` / `achat_result` / `abatch` / `abatch_result`：批量与异步，结果顺序与入参一致。
3. `list_providers()` / `get_provider(name)` / `default_provider()` / `ProviderError`：配置与错误。
4. `LLMResult` / `LLMUsage` / `UsageTracker`：返回值与用量计费。

框架侧需要的不是文本而是模型类，这一层适配集中在 `backend/features/model_bridge.py`，不要绕过它：

- `workflow_model(provider=None, usage_key=None)`：注册为 `StudioLLM` 的框架 `BaseLLM`。agent 在子进程里只靠 `llm_config` 重建，也仍然走 `llm` 包。
- `agent_model(provider=None, usage_key=None)`：Deep Agents / LangGraph 用的 LangChain `BaseChatModel`，`usage_metadata` 来自 `LLMResult.usage`。
- `usage_hook(callback) -> key` / `release_usage_hook(key)`：Run、Batch、digest 的 token 统计来源。配置里带的是 key 而不是 callable，所以能穿过子进程。

**换供应商**：只改 `llm/providers.json` 的 `default`，或设环境变量 `LLM_PROVIDER`（Studio 另认 `EAX_PROVIDER`，优先级更高）。新增供应商在 `llm/adapters/<vendor>.py` 实现 `complete`（可选 `acomplete`、`native_batch`），并在配置里用 `adapter` 指向该模块。业务模块不应新增供应商 SDK 初始化或 URL 拼接。

**在 SafeChain 机器上**：只有 `llm/providers.json` 和它需要的环境变量不同，供应商名叫 `safechain`。`backend/`、`frontend/`、`tests/`、`projects/` 以及导出的独立项目都不需要改。

改完 `providers.json` 或换机器后跑一次真实冒烟（会计费）：

```sh
set -a; . ./.env; set +a
$PY llm/examples_terminal.py
```

测试从不真的请求供应商：`tests/llm/` 把 transport 打桩。

底层框架已有模型类仍在 `backend/evoagentx/models/`，`model_bridge` 只是把 `llm` 包包装成框架认得的类，不改框架自身的重试、解析行为；计费统一由 `llm.UsageTracker` 按环境变量里的单价计算，不读供应商返回里的金额。

Mem0 当前使用本地 embedding / 配置对象（见 memory 模块）；它不是上述聊天请求。以后更换 embedding 模型，要另外检查向量维度和存量索引，不能仅换聊天 API。

## 交给对话 LLM 的操作方式

先列出文件：

```sh
python3 scripts/maintenance/context_bundle.py evaluation --list
python3 scripts/maintenance/context_bundle.py llm --list
```

把一个模块打成 Markdown（输出到仓库外）：

```sh
python3 scripts/maintenance/context_bundle.py llm --output /tmp/llm-context.md
python3 scripts/maintenance/context_bundle.py evaluation --part backend --output /tmp/evaluation-backend.md
python3 scripts/maintenance/context_bundle.py evaluation --part frontend --output /tmp/evaluation-frontend.md
```

脚本只收集代码和模块文档，不收集 `.env`、provider 配置、聊天记录、数据库或数据集。源代码/注释仍应在分享前自己检查。默认不附测试；按需要单独提供功能测试。

给模型的请求模板：

> 只修改下面这个功能。当前行为是……，期望行为是……。请先列出需要修改的文件，缺少依赖请向我索取。返回每个修改文件的完整内容和准确路径；保留未涉及的行为、API 兼容性和停止机制。不要改数据集或密钥。最后给出验证命令。

一次改一个模块；跨功能需求先改接口，再改调用方。若模型建议删除兼容文件、批量替换所有导入或重写 App，请先核实它是否掌握调用范围。

## 验证与启动

Python 环境按以下顺序选择，`./start.sh` 和下面的命令保持一致：
`EVO_VENV`（显式指定）> conda 环境 `evo`（名字可用 `EVO_CONDA_ENV` 改）> 仓库内 `.venv`。
本机没有 `evo` 环境时用 `.venv`；另一台机器有 `evo` 就自动用它，脚本不用改。
下面的 `$PY` 代表所选环境的 python（例如 `.venv/bin/python` 或
`~/anaconda3/envs/evo/bin/python`）。

从仓库根目录运行：

```sh
$PY -m pytest tests/api tests/studio tests/src tests/dataset tests/llm -q
$PY -m pytest llm/tests -q      # 该机器自备的 llm 包自身的测试
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

功能测试通常在 `tests/api/`、`tests/studio/`；前端测试与对应 feature 文件同目录。
更改纯前端代码后构建并刷新；后端服务入口仍为：

```sh
./start.sh                       # 选环境、装依赖、构建前端、起服务
$PY -m uvicorn backend.api.app:app --host 0.0.0.0 --port 8000   # 只起后端
```

重启前确认没有正在运行的 Batch、Evolve 或聊天。不要为了更新代码误停运行中的任务。
