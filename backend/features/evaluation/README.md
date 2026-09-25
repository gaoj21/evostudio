# evaluation 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `evolve_api.py` — Evolve API: sources `canvas` / `saved_batch` / `saved_run` (JSON) and a labelled upload (multipart, MIPRO); `POST /api/evolve/{id}/stop`; task statuses `running|done|failed|stopped|interrupted`. dspy compatibility shims live here.
- `evaluation.py` — Scoring a batch of runs against expected answers. Built-in metrics `exact_match`, `contains`, `numeric`; `available_metrics()` adds every custom tool whose params are exactly `(prediction, label)`. Shared by `GET /api/metrics` and `GET /api/evolve/metrics`.
- `saved_result_evolution.py` — Evaluate saved predictions and propose prompts without replaying a workflow. The resolved source is `{type, batch_id, run_id, origin, ...}` (no dataset or split); the default metric is `exact_match`.

- `evaluator_tools.py` — the workflow's evaluators (`graph["evaluators"]`), their routes, reports, drafts and saved-result evaluation. `evaluate_runs` validates and runs each evaluator inside its own `try`: a misconfigured evaluator reports `status: failed` with its error and never fails the run or the other evaluators.
- `canvas_evolution.py` — replay candidates through the actual workflow using one of the workflow's evaluators as the objective (`canvas:<name>` or a bare `<name>`). Records run in the batch's order (`batch.assign_sequences` / `_group_key`: DataLoader group, then the Input's declared trajectory, then the memory key); a failed record blocks the rest of its sequence.
- `python_evaluator.py` — pasted/uploaded evaluator code in a stoppable worker; errors are explained by `backend/features/user_code.py` (line in the user's code plus printed output).

No domain metric or report lives here. A task-specific evaluator is ordinary evaluator code kept with its project (for example `projects/credit_risk/evaluators/monitoring_report.py`) and pasted into Evaluate & Evolve.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在仓库根目录的 `llm/adapters`，不要写在业务函数中；`backend/` 里不出现供应商名字或 SDK。框架模型类由 `backend/features/model_bridge.py` 提供。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）

## Evaluation is off the canvas

There is no Evaluator node. A workflow's evaluators live on its graph
document, and the Evaluate & Evolve panel owns them:

```json
graph["evaluators"] = [{
  "name": "monitoring_report",      // unique, non-empty
  "code": "def build_evaluator(...): ...",
  "config": {"decision_field": "decision"},   // the form's values
  "metric": "detection_rate",       // or null: the first metric returned
  "direction": "maximize",          // or "minimize"
  "timing": "manual" | "run" | "batch",
  "timeout": 120,                   // 10..3600 seconds
  "labels": "<data resource id>",   // or null; an old graph may store the
                                    // whole DataLoader config here
  "enabled": true
}]
```

`graphs.evaluator_list` validates the list on save exactly as
`graphs.review_rule` validates `review`: unique non-empty names, code that
passes interface discovery, timing/timeout/direction in range. Unknown keys
are dropped. A body that omits `evaluators` keeps the stored list, so saving
the canvas never clears them; `"evaluators": []` clears them. A switched-off
(`enabled: false`) or codeless evaluator is a draft: it may be saved and it
reports its own failure when something asks it to run.

`graphs.lift_evaluators` (inside `migrate_flow`, `FLOW_VERSION = 3`) moves an
old `kind: "evaluator"` node into the list on load: its `evaluator` config
becomes the entry, the old `node` timing becomes `run`, and the node and every
edge touching it are dropped. It is idempotent.

### Writing the code

```python
def build_evaluator(threshold: float = 0.5):
    class Evaluator:
        def evaluate(self, records):
            # records: complete saved inputs, node_outputs, result,
            # status/errors and execution_snapshot. No automatic aggregation.
            return {"metrics": {"score": 1.0}, "details": {"threshold": threshold}}
    return Evaluator()
```

Named typed parameters with defaults generate the form; `records`, `config`
and `label_records` are supplied by Studio. Optional external labels are
unjoined: code receives them in `label_records` and `config["label_records"]`
and owns matching, grouping and scoring. Without external labels,
`label_records` is `[]`. Legacy top-level `evaluate(records, ...)` remains
supported. Defining both entrypoints, duplicate definitions, async or
decorated entrypoints is rejected.

### Routes (all under `/api`)

- `GET /api/evaluators` — what an evaluator is: entrypoints, timings,
  defaults, example code.
- `POST /api/evaluators/interface` — `{code}` → `{kind: "factory"|"function",
  params: [{name, type, required, default, description}], provided, warnings,
  error}`. Nothing is executed; a syntax error comes back as `error` (200).
- `GET|PUT /api/graphs/{id}/evaluators` — `{"evaluators": [...]}`; PUT
  replaces the list (`{"evaluators": [...]}` or a bare list).
- `DELETE /api/graphs/{id}/evaluators/{name}`.
- `POST /api/graphs/{id}/evaluators/preview` — `{code, config, metric?,
  labels?, batch_id|run_id}` → `{report, execution_count, metrics: [{name,
  type, nullable, sample}]}`. Nothing is saved.
- `POST /api/graphs/{id}/evaluators/run` — `{name|names, or code+config,
  batch_id|run_id}` → `{"evaluations": {name: report}}`, persisted onto that
  batch (`batch.set_evaluations`) or run (`runner.set_evaluations`).
- `GET|PUT|DELETE /api/graphs/{id}/evaluators/draft` — `{"draft": {code,
  config, updated_at}}`, one per workflow under
  `studio-data/evaluator_drafts/`. Drafts are never validated and never run;
  they survive a restart and are cleared when the same code is saved as an
  evaluator.

### What the code can rely on

- **Objective**: without a chosen metric, a report uses the first metric the
  code returns and says so (`objective.chosen`). Evolve requires the metric to
  be chosen explicitly.
- **Timing**: `manual` (only when asked), `run` (after each run), `batch`
  (after the batch, and after each chunk of a streaming batch). One default,
  `run` (`evaluator_tools.DEFAULT_TIMING`).
- **Failed runs** reach "after each run" evaluators too, before the run's
  failed status is published.
- **Errors** name the line in the user's code (`line 4, in evaluate: ...`) and
  include what the code printed before failing. **Printed output** is kept in
  the report as `logs`.
- **Time limit**: `timeout`, 10–3600 seconds, default 120, in a stoppable
  worker process.
- **Labels** are a separate resource, frozen once per evaluation, never
  injected into an Agent and never kept in the saved report.
- **Isolation**: each evaluator is validated and run on its own; one broken
  evaluator yields a failed report, not a failed run.
- **Evolve** hides from its prompt proposer every record input named by a
  string value in the evaluator's `config`, so a label the code was pointed at
  does not reach the model unless `share_labels` is set.

`report()` still understands the legacy fixed scorers (`exact_match`,
`contains`, `required_fields`, `tool`) because it is also the
`evaluate_records` library tool; no evaluator can be created with them.

Focused tests: `tests/studio/test_evaluators_panel.py`,
`tests/studio/test_evaluator_code.py`,
`tests/studio/test_python_evaluator.py`,
`tests/studio/test_evaluator_evolve_audit.py`,
`tests/studio/test_credit_risk_evaluator.py`.

Domain evaluators live with their project, e.g.
`projects/credit_risk/evaluators/` (report code and release labels).
