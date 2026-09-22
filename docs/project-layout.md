# 项目目录

更新：2026-09-09。项目按 backend、frontend、projects 三层组织。

```text
EvoAgentX/
├── backend/                       # 平台后端
│   ├── api/                       # API、工作流执行、调度、导出
│   ├── evoagentx/                 # 通用 agent / workflow 框架
│   ├── llm/                       # 模型服务适配
│   ├── memory/                    # memory 存储适配
│   └── data/                      # 平台图配置、工作区与运行状态
├── frontend/                      # React / Vite 平台前端
│   └── src/
├── projects/                      # 业务项目
│   └── credit_risk/
│       ├── agentic_pipeline/      # 领域逻辑
│       ├── skills/                # 领域技能
│       ├── docs/                  # 方法、统计和交接文档
│       ├── dataset/               # 数据、审核与版本
│       │   ├── builders/          # 数据构建脚本
│       │   ├── contemporary/      # 旧 live 数据与原始缓存
│       │   ├── expansion/         # 扩展来源与冻结发布
│       │   └── review/            # 审阅证据与审计发布
│       └── output/                # 业务实验产物
├── docs/                          # 平台文档
├── tests/                         # 测试，studio/ 保留原测试分组名称
├── examples/                      # 框架使用示例
├── assets/                        # 文档素材
└── pyproject.toml                 # Python 安装与启动配置
```

通用 data、logs、workplace 和工具缓存仍由原工具管理。虚拟环境和安装元数据不属于业务层。

## 启动

从仓库根目录执行：

```sh
uv sync --extra dev --extra studio
uv run evoagentx-studio
```

Python 后端入口为 `backend.api.app:main`，也可以使用 `uv run uvicorn backend.api.app:app --reload`。默认前端构建目录为 `frontend/dist`，默认平台运行数据为 `studio-data`；`EAX_STUDIO_DATA_DIR` 仍可覆盖运行数据位置。

前端开发：

```sh
cd frontend
npm install
npm run dev
```

Python 项目采用 editable 安装：`pip install -e .`。源码位置改变，但 `evoagentx`、`llm`、`memory`、`credit_risk` 公共导入名由 pyproject.toml 中的 package-dir 映射保留；平台 API 导入改为 `backend.api`。pytest 配置包含 backend 和 projects 搜索路径。

## 新路径

| 原路径 | 当前路径 |
|---|---|
| studio/backend/ | backend/api/ |
| studio/frontend/ | frontend/ |
| studio/data/ | studio-data/ |
| evoagentx/ | backend/evoagentx/ |
| llm/ | backend/llm/ |
| memory/ | backend/memory/ |
| credit_risk/ | projects/credit_risk/ |

业务脚本示例：`python projects/credit_risk/dataset/builders/build_r10_expand.py --help`。

冻结数据集只移动位置，不修改内容。历史 manifest 可能记录旧绝对路径，它是发布时的来源记录；读取当前版本应使用新 projects 路径。已经启动的旧 Python 服务需要重启才能加载新的模块路径。

## 文档入口

- [平台架构](architecture.md)
- [后端运行说明](../backend/README.md)
- [信用风险项目](../projects/credit_risk/README.md)
- [数据集构建方法与完整统计](../projects/credit_risk/docs/dataset/DATASET_CONSTRUCTION_AND_PROFILE.md)
- [信用风险 Agent 交接](../projects/credit_risk/docs/HANDOFF_AGENT.md)

新业务项目放 `projects/<project_name>/`；平台通用能力放 backend，页面放 frontend。业务数据、技能、实验结果和领域文档随项目保存。

- [通用任务平台改进与阶段范围](platform-redesign.md)
