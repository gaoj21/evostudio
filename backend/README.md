# EvoAgentX Studio

维护与模块入口：[MAINTENANCE.md](../MAINTENANCE.md)。业务实现按功能放在 `features/`，模型调用在 `llm/`；`api/` 保留路由装配和旧路径兼容门面。

Studio is the visual application in this repository. Its stable core edits and
runs workflow graphs; domain presets and prompt optimization are integrations
around that core, not dependencies of it.

## Start it

From the repository root:

```bash
uv sync --extra dev --extra studio
uv run evoagentx-studio
```

For long-term vector memory, also install `--extra rag`. For workflow prompt
optimization, install `--extra optimizers`. The React development server lives
under `frontend/` and proxies `/api` to port 8000.

Mutable state defaults to `backend/data/`. Set
`EAX_STUDIO_DATA_DIR` to keep runtime data outside the checkout.

## Feature boundary

Stable core:

- graph editing, validation and execution
- batch execution and evaluation
- workspaces, run artifacts, tools and skills
- short- and long-term memory contracts

Experimental integrations:

- `evolve`: MIPRO prompt optimization; requires the `optimizers` extra
- `credit_risk`: repository-local presets, datasets and matching tools

`GET /api/features` exposes this boundary and current runtime availability.
When optimizer packages are absent, the rest of Studio starts normally and
Evolve endpoints return HTTP 503 with installation guidance.

## Backend map

The following modules are under `api/`.

- `app.py`: application assembly and the core HTTP surface
- `runner.py`, `batch.py`: execution orchestration
- `graphs.py`: graph persistence and validation
- `memory_*`, `stm_store.py`, `table_store.py`: Studio memory policy and stores
- `workspace.py`: per-graph project and artifact view
- `export_api.py`: exported-project assembly
- `export_templates.py`: generated standalone source and README templates
- `project_import.py`: safe parsing and importing of exported projects
- `evolve_api.py`: optional optimizer integration

The REST contract is documented in `API.md`; repository-wide dependency
direction is documented in `../docs/architecture.md`.
