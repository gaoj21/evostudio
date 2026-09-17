# evaluation 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `evolve_api.py` — Evolve (MIPRO prompt optimization) API for EvoAgentX Studio.
- `evaluation.py` — Scoring a batch of runs against expected answers.
- `evaluation_report.py` — Evaluate a batch after it ran, from what it already holds.
- `saved_result_evolution.py` — Evaluate saved predictions and propose prompts without replaying a workflow.

- `evaluator_tools.py` — canvas evaluator contract, reports and saved-result evaluation.
- `canvas_evolution.py` — replay candidates through the actual workflow using a fixed evaluator objective.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在 `backend/llm/adapters`，不要写在业务函数中。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）

## Uploaded Python evaluator

New canvas Evaluators accept pasted or uploaded `.py` code. `python_evaluator.py`
uses the shared static parameter analyzer and executes code in a stoppable worker
process. Existing built-in and registered-tool evaluators remain compatible.

```python
def evaluate(records, threshold: float = 0.5):
    # records: complete saved inputs, node_outputs, result, status/errors,
    # execution_snapshot and incoming-edge focus. No automatic aggregation.
    return {"metrics": {"score": 1.0}, "details": {"threshold": threshold}}
```

Named typed parameters generate the input form; `records`, `config`, and
`label_records` are supplied by Studio. Optional external labels are unjoined:
code receives them in `label_records` and `config["label_records"]` and owns
matching, grouping and scoring. Without external labels, `label_records` is `[]`.

- `POST /api/evaluators/interface` analyzes `{code}` without executing it.
- `POST /api/evaluators/graphs/{id}/preview` accepts the draft `graph`, evaluator
  node name in `evaluator`, and either `run_id` or `batch_id`. It runs only the
  evaluator on completed saved results belonging to that workflow.
- A preview identifies metric names and shows the complete report. The selected
  metric and maximize/minimize direction become the Evolve objective.
- Return `metrics` with finite numeric values or null. Optional `records`,
  `details`, and `coverage` use the existing evaluator contract; code chooses
  record granularity. No trajectory aggregation is imposed.
- Parameters and source code are saved in the node. Use installed Python
  packages; upload does not install dependencies. The worker has a 120s timeout.

Focused tests: `tests/studio/test_python_evaluator.py` and
`frontend/src/features/evaluation/EvaluatorInspector.test.jsx`.

### Class factory contract

Preferred new entrypoint: `build_evaluator(threshold: float = 0.5)` returning an
instance with a synchronous `evaluate(self, records)` method. Factory parameters
create the form; helper functions and class constructors do not. The factory can
also accept supplied `config` and `label_records`. The method receives records
at evaluation time. It returns the same report dictionary described above.
Legacy top-level `evaluate(records, ...)` remains supported. Defining both
entrypoints, duplicate definitions, async entrypoints or decorated entrypoints
is rejected. Multiple internal classes and helper functions are allowed.

DataLoader uses the equivalent `build_dataset(...)` factory returning a PyTorch
Dataset/IterableDataset. There is no required class name or platform base class
for evaluators. Additional required method parameters are not supported; pass
settings to the factory and store them on the instance instead.
