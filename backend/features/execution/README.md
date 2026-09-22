# execution 模块

先读仓库根目录 `MAINTENANCE.md`。本目录是真正实现；`backend/api` 对应同名文件仅用于旧导入兼容。

## 文件职责

- `batch.py` — Batch run engine. `assign_sequences` records on each item its `trajectory` (the group value of the Input's declared `sequence`) and its memory `sequence` key; `_group_key` picks DataLoader group → Input trajectory → memory key. There is no implicit grouping by `sample_id` / `as_of` or any other field name. Canvas Evolve reuses both functions.
- `batch_compare.py` — Comparing two evaluations of the same workflow. Records are labelled generically: the trajectory when there is one, otherwise the first short text value (or `field=value`).
- `batch_export.py` — Getting a batch's results out of the Studio.
- `scheduled_execution.py` — Dispatches each scheduled occurrence through the ordinary Run or streaming DataLoader Batch engine; preserves scheduled time, experiment session, and recovery identity.
- `loader_run.py` — Shares Dataset streaming and runtime inputs between manual and scheduled execution. Batch Resume uses the saved graph and reader configuration.
- `../persistence.py` — Atomic JSON checkpoints for Runs, Batches and archived input records.
- `scheduler.py` — Running a workflow on a timer. `next_local_time` recomputes a daily time on the local wall clock for each date, so daylight-saving changes do not shift it.
- `watcher.py` — Watchers: scheduled / live input-source polling. Plugin Input types are watchable and de-duplicated by their `watch_key` (a record hash without one). Records are marked seen only after the poll and every run start succeed. Daily times use `scheduler.next_local_time`.
- `review.py` — HITL web review. The rule is `graph["review"]` (`node`, `score_field`, `range`, `when{field,equals}`, `flag_field`, `show`, `approve_label`, `reject_label`), validated on save by `validate_rule`. Without a rule only `review_required: true` routes; a batch/run `review_zone` replaces `range`. Reviews store `node`, `score`, `fields`, the full `output`; `final_action` is the rule's label. Full node outputs are read, not display copies.

## 修改边界

- 只改本功能时先提供涉及文件；HTTP 合同变化再附 `frontend/src/api.js` 与对应前端 feature。
- 数据路径取自 `backend/api/studio_config.py`；不要移动运行数据。
- 模型 API/SDK 的差异放在 `backend/llm/adapters`，不要写在业务函数中。
- 功能间仍有依赖；缺少依赖文件时让对话 LLM 明确列出，不要让它猜测接口。

## 验证

`.venv/bin/python -m pytest tests/api tests/studio -q`（从仓库根目录）

## Incremental DataLoader runs

- `loader_run.py` handles Python canvas DataLoader Run requests immediately and
  creates one batch ID. No dataset scan occurs when opening the Run dialog.
- `dataset_stream.py` runs PyTorch in a separate process and hands over one file
  containing one batch; acknowledgement is delayed until execution finishes.
  This bounds queued input data. Dataset constructors must themselves be lazy.
- `stream_batch.py` executes those chunks, preserves the final partial batch,
  archives full input records under `<batch_id>-inputs/`, and keeps small input
  summaries in batch state. Full results remain in per-run files. CSV/JSON
  exports hydrate archived inputs on explicit export.
- The DataLoader owns `read_batch_size`, `n` and `offset`. `n=0` means all; finite
  selection is applied while reading rather than after collecting everything.
- Run owns workers/API mode and optional daily/weekly/monthly execution grouping
  by an ISO date field. Records must already be ordered. Grouping does not
  aggregate records or replace DataLoader batch size.
- Stop terminates the reader and active runs. An unsuccessful chunk halts later
  reading. An interrupted stream cannot use the old fully-collected resume
  mechanism: restart with explicit DataLoader offset/count; unread records were
  never collected. Existing collected-batch resume behavior is preserved.
- Batch evaluators still receive all saved execution results after completion;
  their memory usage is separate from incremental input loading. Full exports
  also materialize their output. Legacy readers and Evolve replay retain their
  existing preparation path; use the Python DataLoader run path for streaming.

## Streamed DataLoader runs

`loader_run.py` starts a run over a Python Dataset without reading it in the
request; `stream_batch.py` executes it chunk by chunk. Failures do not stop the
stream: `batch._execute_batch` carries blocked groups across chunks
(`state["blocked_groups"]`), and `resume_batch` re-runs unfinished records and
then continues reading from `state["records_read"]` (`loader_run.resume_chunks`).
Which records form an ordered group comes from the data (DataLoader group), the
Input type's declared `sequence` (`sources.input_sequence`, e.g. a project
plugin's Input), or the memory configuration
(`backend/features/memory/sequencing.py`), never from assumed field names.
