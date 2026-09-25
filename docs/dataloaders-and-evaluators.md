# DataLoaders and canvas Evaluators

Implemented September 17, 2026. These components run outside the model-provider layer. The model layer (the standalone `llm/` package) and `backend/evoagentx/models/` are unchanged.

## Input → DataLoader → workflow

Add **DataLoader** from the canvas Input sources library. Upload files or a directory, choose a reader, and click **Preview and update output fields** before mapping its output to downstream nodes.

Uploaded resources preserve original bytes and relative directory structure. They are immutable, versioned by SHA-256, and stored in private runtime data. Uploading does not require a supported parser. The built-in structured reader supports CSV, TSV, JSON and JSONL; other file formats require a custom reader. Binary files can also be emitted as metadata by the file reader, without pretending their contents are parsed text.

New Inputs expose one loading interface: **PyTorch Dataset**. The inspector contains files/folder upload, a Python editor, batch size and preview. Reading and preprocessing belong in the Dataset code; separate reader/transform/mapping selectors have been removed. Optional configuration and ordering settings are collapsed under Advanced settings.

Previously saved readers remain compatible and are not silently rewritten. Their inspector offers an explicit **Replace with PyTorch Dataset** action, which clears old reader/transform/mapping settings and output fields so a fresh preview can establish the new schema. Back up or translate any custom preprocessing into the new code before replacement. The old modes are no longer advertised by the DataLoader catalog.

### Workspace paths and splits

Input uploads appear immediately under **Workspace → datasets**, preserving nested directories. Previously uploaded resources referenced by saved Inputs or evaluator label resources are also visible. Workspace exposes a read-only view of the original bytes, not a duplicate. Right-click a file/folder → **Copy path** copies its absolute server path; file previews also offer **Copy full path**. Input shows the resource root with its own copy button. Manage uploaded resource deletion from Input.

Declare `path` and `split` parameters in the factory (or read them from `config`); the inspector generates their form fields automatically. Paste the copied path and choose/fill split there. Named parameters are passed as keyword arguments; legacy config-based factories receive `config["path"]` and `config["split"]`. Custom code decides their meaning. The supplied JSON example accepts absolute paths or paths relative to `resource["root"]`, selects a matching split subfolder, or filters records by their `split` field. Existing code is not silently rewritten: update it or load the new example to consume these parameters. Paths refer to the server filesystem. For explicit paths, prepared-data caching is bypassed so edits to ordinary workspace files are not hidden by an old snapshot.

### Generated Input/Output interface

Pasting/uploading code automatically analyzes `build_dataset` without executing the module. Prefer explicit annotated arguments, e.g. `def build_dataset(resource, path: str, split: Literal["dev", "test"] = "dev", limit: int = 10)`. Names, types, literal defaults and Literal choices generate form controls. `resource` is supplied by the selected upload. Existing `build_dataset(resource, config)` code is supported by discovering constant `config["key"]` / `config.get("key", default)` accesses. Dynamic keys produce a warning: make them explicit parameters to get an accurate interface. Arbitrary Python behavior cannot be fully inferred statically.

Fill the generated form and run Preview. Studio executes the real PyTorch Dataset and inspects all prepared records, showing output names, types, nullability and bounded examples. Each dictionary is one workflow record, not a batch-shaped model argument. The inferred schema is applied to canvas output fields and shown on the Input node. A new/unpreviewed node is visibly a draft. Code or setting edits clear stale output fields; preview again before reconnecting. Input and output interfaces persist with the saved node; display schemas are excluded from the preparation cache key. No output type is claimed until actual data has been produced. Custom parameters no longer require a raw JSON configuration object; list/dict parameters use dedicated JSON fields in the generated form.

### PyTorch runtime and pasted code

Open the Input inspector; the PyTorch Dataset editor is shown directly. Paste code, drop a `.py` file onto the editor, or click **Upload .py**. **Load example** provides a runnable JSON reader. Code lives in the Input configuration and is saved/exported with the workflow; no separate Custom Tool registration is required.

```python
from torch.utils.data import Dataset

class Records(Dataset):
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        return self.rows[index]

def build_dataset(resource, config):
    # resource["root"]: local uploaded directory
    # resource["files"]: manifest entries with relative "path"
    # config: reader_config plus shared data in config["reference_inputs"]
    return Records([{"text": "example", "expected": None}])
```

Return `torch.utils.data.Dataset` or `IterableDataset`, not a pre-batched DataLoader. Every item must be a nonempty JSON-compatible dictionary; tensors must be explicitly converted to scalars/lists by the dataset code. User code runs in a separate process with a 120-second limit and reports syntax/import/runtime errors in the Input preview. Installed Python packages are available; this editor does not install dependencies automatically.

Studio constructs a real `torch.utils.data.DataLoader` for all DataLoader modes, including built-in readers and existing reader-tool adapters. It uses `read_batch_size`, `shuffle=False`, `drop_last=False`, `num_workers=0` and dictionary-preserving `collate_fn`. This keeps optional null values, avoids automatic tensor conversion and duplicate iterable-worker records, and retains the final partial batch. Input batch size is the single value used by PyTorch, workflow execution chunks and the native API request limit when enabled. Run displays this value rather than another size input. Workflow workers control concurrency independently; native request groups can be smaller when fewer requests are ready. Preview, Run, saved preparation and Evolve use this common adapter. Ordinary legacy Input nodes keep their old behavior until converted to DataLoader.

Changing Python code/configuration invalidates the prepared-data cache. Preparation still materializes the dataset, including IterableDataset output, to support complete preprocessing and reuse across candidates; infinite iterators are unsupported. Batch size is not a memory limit. Code runs with the existing local-user permissions, not a security sandbox.

Connected shared reference inputs are snapshotted before primary Loader preprocessing, so transforms can match news against an obligor list. The preview uses the current canvas references as well.

Loader configuration owns file selection, record offset/limit, field mapping, transform scope, reference-vs-record role, and trajectory grouping/order. Whole-dataset transforms execute before record selection and batch execution. Record transforms may return an object, a list of objects, or `None` to filter a record.

A group field together with an order field sorts records into trajectories. Use ISO dates for date ordering. Records of one group execute in order; a failed step blocks later observations of that group. Different groups may execute concurrently. Offset/limit applies to output records and can select a partial trajectory; use complete groups for trajectory evaluation.

Prepared file-resource data is cached using the resource manifest, configuration, and registered custom tool specification. Preview, Run and Evolve reuse it. Disable caching when a custom tool intentionally depends on changing external state. Live source adapters always reload. For DataLoader inputs, the Input batch size also controls workflow chunks and native model API batch size; configure it once in Input.

Current limitation: preparation materializes the selected dataset in memory and the cache is JSON-backed. `iter_batches` emits prepared records in chunks, including the final partial chunk; it is not a fully streaming arbitrary-format parser. Deployments remain bounded by memory and disk resources.

## Custom DataLoader tools

Create tools in Library → Custom. A reader returns a list of record objects:

```python
from pathlib import Path
import json


def read_company_files(resource: dict, config: dict) -> list:
    root = Path(resource['root'])
    rows = []
    for entry in resource['files']:
        if entry['path'].endswith('.json'):
            value = json.loads((root / entry['path']).read_text())
            rows.append({'company': value['name'], 'text': value['report']})
    return rows
```

Whole-dataset preprocessing:

```python
def deduplicate(records: list) -> list:
    seen = set()
    output = []
    for record in records:
        key = record['id']
        if key not in seen:
            seen.add(key)
            output.append(record)
    return output
```

A row-transform signature is `transform(record: dict)`. Preserve the downstream fields needed by the graph. Mapping uses source paths as keys and output names as values. `_dataloader` is reserved provenance, carrying the prepared snapshot, resource version, stable record identifier within that snapshot, and optional trajectory group/order.

## Evaluators

Evaluators are not canvas nodes. They belong to the workflow as a list
(`graph["evaluators"]`) and are written in the **Evaluation & Evolve** panel:
paste or upload Python, press **Check code**, and the parameters the code
declares become the form that feeds it.

The code takes either shape:

```python
def build_evaluator(decision_field: str = "decision", threshold: float = 0.5):
    """Typed parameters with defaults become the panel's form."""
    class Report:
        def evaluate(self, records: list) -> dict:
            successful = [r for r in records if r.get("status") == "success"]
            return {"metrics": {"completion_rate": len(successful) / len(records) if records else None}}
    return Report()
```

```python
def evaluate(records: list, threshold: float = 0.5) -> dict:
    return {"metrics": {"completion_rate": ...}}
```

It receives every saved execution of the selected run/batch: original inputs,
final `result`, all `nodes` with statuses/errors, complete `node_outputs`,
execution snapshot and provenance. Legacy runs may only have clipped
previews; missing full outputs cannot be reconstructed.

Timing says when an evaluator runs by itself:

- **manual:** only when run from the panel, on a saved run or batch.
- **run:** after each workflow execution finishes.
- **batch:** over all saved executions of the completed batch, including
  failed and blocked items. Collection-driven batches wait for the complete
  collection; a stopped batch still reports over what ran.

The platform does not group records into trajectories or impose a scoring
unit. The evaluator owns selection, label matching, preprocessing, grouping,
aggregation and scoring. A separate label resource is delivered as
`config.label_records` and is never injected into workflow agents; the
panel's parameter values arrive as the declared arguments.

Configure the objective metric (e.g. `completion_rate`) and
maximize/minimize direction for Evolve. Metrics must be finite numbers or
null. `records` and `details` are optional and may use any granularity; one
result per input is not required. Optional `coverage` must declare `unit`,
`total`, `scored`, `unscored` with consistent nonnegative counts. The
platform never infers scoring coverage from the number of executions.

Pasted code is kept as a per-workflow draft (server-side, mirrored in the
browser) so leaving the panel — to copy a run path, say — loses nothing.
Preview runs the code without saving anything; Run saves the report onto
that run or batch. Reports stay separate from workflow outputs. Evolve
consumes the selected metric and the report as feedback, requires unchanged
declared coverage and no fewer successful executions when comparing
candidates, and never shows the proposer a record field named by a string
value in the evaluator's config — a label must not reach the prompt writer.

## Run, results, Chat and Evolve

Run reads the canvas DataLoader settings; it does not present another copy of data selection/preprocessing controls. Run retains parallelism and the native API enable/disable choice; batch size comes from Input. Each run stores a graph/settings snapshot, actual input data, provenance and complete node outputs. Results show evaluator reports. The batch Evaluation tab runs a saved evaluator on saved results without replaying agents.

**Evaluation & Evolve** chooses one of the workflow's evaluators for saved runs/batches. Evaluation-only scores those outputs; prompt refinement uses the evaluator report as feedback and remains explicitly unvalidated until a new run.

The **Canvas Input + evaluator** source runs the real canvas on prepared inputs. Evaluation-only performs a baseline replay. Evolution runs a bounded number of prompt proposals, replays each candidate and compares the selected objective. Inputs and separate labels are prepared once; each candidate starts with empty isolated workflow memory. A candidate is accepted only if its objective improves without reduced scoring coverage or fewer successful executions. This is a bounded evaluator-driven search, separate from the existing MIPRO path. It uses the selected data as development data; final Test evaluation remains a separate experiment.

Shared Mem0 workflows are rejected for candidate replay because a copied graph ID cannot isolate shared memory. Saved-result evaluation remains available. Custom tools may have external side effects; fresh local memory does not undo those effects. Candidate histories preserve individual experiment runs.

`DataEvaluationToolkit` exposes `read_dataset(config)` and `evaluate_records(records, config)` to the existing tool registry and eligible Chat Agents. Workflow Chat's node contract includes DataLoader and Evaluator configurations. The components also work directly through the executor without an LLM choosing tool calls.

## Boundaries and compatibility

- Existing graph/source formats remain supported; no existing Credit Risk workflow or dataset was rewritten.
- JSON graph serialization preserves the new configurations. Standalone Python export is explicitly unsupported for these nodes; use Studio to execute them.
- Uploaded resources, prepared caches, labels and run artifacts stay under private runtime data and are not publication assets.
- Data resources cannot be deleted while referenced by a saved Input or label DataLoader.
- Cache storage is not currently automatically garbage-collected; historical runs preserve their actual input data if a detached resource is later deleted.

## Main files

- `backend/features/data/data_resources.py`: immutable file/folder storage.
- `backend/features/data/dataloaders.py`: reader, transform, provenance, cache and batch interface.
- `backend/features/evaluation/evaluator_tools.py`: shared evaluator contracts and saved-result API.
- `backend/features/evaluation/canvas_evolution.py`: evaluator-driven candidate replay.
- `frontend/src/features/evaluation/EvaluatePanel.jsx`: writing, checking, previewing and saving evaluators.
- `backend/features/library/data_evaluation_tools.py`: tool-registry exposure.
- `frontend/src/features/data/DataLoaderInput.jsx`: input configuration and preview.
