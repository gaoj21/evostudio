# data 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `user_datasets.py` — 用户数据集的上传、持久化、预览、重命名、删除和字段映射；独立于领域数据集与 LLM。`parse()` 也是 Batch/Evolve 上传文件的唯一解析器。
- `sources.py` — Batch input sources: `parse_upload`（委托 `user_datasets.parse`）、`records_from_source_node`（DataLoader、My dataset、插件 Input 类型、API、custom source 的统一入口）、`input_sequence(graph)`（已连线 Input 声明的 `{group, order}`）、按名称映射到 workflow inputs。不含任何领域数据集。
- `source_apis.py` — API-backed canvas source nodes and the schema list (`all_source_types()`：内置类型 + 插件类型 + custom toolkit source；`local` 标记可直接读取的类型)。
- `source_collection.py` — Collect canvas inputs once, persist them, then run the saved records.
- `preprocess.py` — Input preprocessing for EvoAgentX Studio.

领域专用的 Input 类型（例如 `credit_risk`）不在本目录，由 `projects/<p>/studio_plugin.py` 的 `source_types()` 提供，经 `backend/features/plugins.py` 加载；契约见 [projects/README.md](../../../projects/README.md)。

- `data_resources.py` — immutable uploaded files/folders, versions and lifecycle.
- `dataloaders.py` — reader/transform contracts, prepared-data cache and provenance.
- `input_composition.py` — per-record data plus shared references, available before Loader transforms.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在仓库根目录的 `llm/adapters`，不要写在业务函数中；`backend/` 里不出现供应商名字或 SDK。框架模型类由 `backend/features/model_bridge.py` 提供。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）

用户数据集的使用方法及格式见 [custom-datasets.md](../../../docs/custom-datasets.md)。

- `torch_loader.py` — Shared PyTorch Dataset/DataLoader adapter and isolated Python dataset factory; dictionary collation, no shuffle, no dropped tail. Input code is stored with the graph.

- `dataset_interface.py` — Static factory-argument discovery and runtime form-value validation; no model-provider dependencies.

- `../user_code.py` — shared by Python Datasets and Python evaluators: an exception raised by user code becomes a `UserCodeError` whose message names the line in the user's code (`Dataset code line 7: KeyError: 'x'`), lists the user-code frames, and keeps up to 4000 characters of what the code printed before failing (`TailBuffer`).

Native runtime isolation: only worker processes import torch_loader/PyTorch. API preview returns prepared records directly; batch collation runs through dataset_batches workers. Never import torch_loader in the API preparation path: another dependency may already have loaded an incompatible OpenMP runtime. Workers start with one OpenMP thread and `KMP_DUPLICATE_LIB_OK=TRUE` (`chat_control.worker_env`), which is what lets torch, scikit-learn, faiss and spaCy load in one worker; an OMP Error #15 that happens anyway is surfaced as an error without terminating the server.

Worker environment (`chat_worker.isolate_path`): a Dataset runs with the data resource's files folder first on `sys.path` and as its working directory, so relative paths and modules uploaded with the data resolve there; the standard library and site-packages follow; Studio's repository goes last, so its top-level folders (`api`, `data`, `memory`, `features`, `llm`) never shadow an installed package. `PYTHONPATH`, `PYTHONHOME`, `DYLD_*` and `LD_PRELOAD` from the server's shell are not passed on, and library paths inside another conda installation are dropped. A module that still resolves inside the checkout, or a native extension built for another environment, is reported with the path it came from and the install command for Studio's own Python.

Interface preview for Python Inputs bypasses full preparation and caches. A literal
`OUTPUT_SCHEMA = [{"name": "text", "type": "str"}]` is read via AST without
executing imports or constructing the Dataset. Without this declaration, interface inspection returns an actionable error and never falls back to execution. Only the separate Sample action reads records, at most
one per requested sample using a PyTorch batch size of 1, within the Input's
"Sample time limit" (which also bounds Dataset initialization on a run); a
sample that is taking too long can be stopped (`POST /api/dataloaders/preview/{request_id}/stop`). Sample types are observations, not a full
schema guarantee; preview counts are not dataset totals. Eager Dataset constructors
can still allocate large amounts of memory: declare OUTPUT_SCHEMA to avoid running
them for interface inspection. Inputs requiring shared references must declare the
schema to avoid loading all references during preview. Every Python DataLoader read — batch Run, a single run's one record, a full
read for Evolve or an evaluator's labels — goes through `dataset_stream.py`:
one worker builds the Dataset once and hands over one batch at a time, so
initialization gets the sample time limit, each batch gets the per-batch time
limit however long the dataset is, and Stop ends the worker. Selection
(`offset`/`n`, a single run's record index, a resume) is passed to the
Dataset: a map-style Dataset is asked only for the selected indices, so
records before the offset are never read or preprocessed. Prepared data is
cached per code/configuration/resource version under `dataloader-cache/`,
bounded by `EVO_DATALOADER_CACHE_MB` (default 2048).

## API data, preprocessed in code, run by day / week / month

API sources are never fetched inside an HTTP request. The flow for any task:

1. **Collect** the API source (Run → Batch → collect). A worker walks the whole
   configured range (GDELT window by window, an HTTP API item by item) and can
   be stopped. A DataLoader that wraps an API source is collected the same way;
   a batch or preview never fetches it inline.
2. The collected records are **saved as a data resource** (`records.jsonl`,
   immutable, versioned; `create_from_records`). Later runs read the same bytes
   instead of fetching again, so dev/test comparisons use identical data.
3. A **PyTorch Dataset Input** chooses that resource and does the preprocessing
   in code. The editor's "Group by entity and period" example turns records into
   one item per entity per day/week/month; it names no business fields.
4. **Execution grouping** (by day / week / month) requires choosing the date
   field; there is no default field name.

Reliability:

- A failed record does not stop a streamed run. Records that must follow it
  (the same memory entity or DataLoader group) are marked blocked; others run.
- A stopped, failed or interrupted stream can be **resumed**: unfinished records
  run first, in order, then reading continues after the last record read.
- Workers exit by themselves when the server that started them is gone
  (`STUDIO_WORKER_PARENT`), so restarts leave no waiting processes behind.
- Preparation caches lock per cache key; one dataset never makes another wait.
- `batch_timeout` (10–3600 s, default 120) bounds how long a Dataset may take to
  produce one batch; a full non-streaming read gets 10× that.

## Parsing, API requests and collections

- **Uploads.** Batch and Evolve uploads go through `sources.parse_upload`, which
  calls `user_datasets.parse`: UTF-8 only, a header row of non-empty unique
  names, TSV read without quoting (a cell starting with `"` is data), and a
  CSV/TSV row whose column count differs from the header is rejected with its
  row number instead of producing a record keyed by `None`.
- **HTTP API placeholders.** `{name}` placeholders from `vars` are URL-quoted
  only in the URL; in `params` they are filled raw, and the query string is
  encoded once by `urlencode`; inside a JSON `body` they are filled in string
  keys and values without quoting.
- **Retries.** 429, 5xx and network errors are retried (5 attempts, 5/15/30/60 s
  backoff). Other HTTP statuses, a malformed URL or a bad argument fail at once.
- **Collection fingerprint.** A collection belongs to an Input by a fingerprint
  of its name and source settings (and its references'); canvas position and
  description are not part of it, so moving the node keeps the collection.
  Collections saved with the older whole-node fingerprint are still accepted.
- **My dataset in reference mode** emits one field holding every record;
  `reference_field` defaults to `reference`. (A DataLoader reference defaults to
  `reference_data`.)
- **Dataset code errors** name the line in the user's code and include what it
  printed (`backend/features/user_code.py`).

## Trajectories declared by an Input

An Input type may declare `sequence: {"group": field, "order": field}` (plugin
Input types do this; see `projects/README.md`). `sources.input_sequence(graph)`
returns it for the wired Input; the batch keeps both fields through mapping
(`source_collection.sequence_inputs`) and runs each group in order, blocking the
rest of a group after a failure. Records must already be in order: only a
DataLoader's `group_by` / `order_by` sorts. Nothing in this directory groups by
a field name it assumes.
