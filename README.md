# EvoStudio

A modular workflow studio with independent chat agents, memory, evaluation and prompt improvement.

- Code maintenance: [MAINTENANCE.md](MAINTENANCE.md)
- Model API adapters: [backend/llm/README.md](backend/llm/README.md)
- Backend setup: [backend/README.md](backend/README.md)
- Frontend: [frontend/README.md](frontend/README.md)
- Frozen credit-risk dataset: [dataset README](projects/credit_risk/dataset/README.md)

## Local configuration

Install Python 3.11+ (3.12 recommended) and Node.js 22 LTS, then run:

```bash
./start.sh
```

Open http://127.0.0.1:8000. The first launch creates `.venv`, installs Studio,
Deep Agents/LangGraph, Mem0 and optimizer dependencies, and builds the frontend.
Later launches reuse dependencies; press Ctrl+C to stop. No separate frontend server is needed.
The script creates local configuration templates only when missing. Set your model
host/model in `backend/llm/providers.json` and `LLM_API_KEY` in `.env`, then restart
before using model features. Neither file is committed.

```bash
./start.sh --port 8001          # use another port
./start.sh --no-build           # reuse the existing frontend build
./start.sh --install            # refresh dependencies
PYTHON=python3.12 ./start.sh    # choose Python when creating the environment
```

Optional RAG and additional tool integrations can be installed with
`EVO_EXTRAS=studio,harness,mem0,optimizers,rag,tools ./start.sh --install`.
Use `EVO_VENV=/path/to/venv` for a different Python environment and
`EAX_STUDIO_DATA_DIR=/path/to/data` for runtime storage. The default storage is
`backend/data/`. Dependencies and frontend builds require internet access on first launch.

For manual setup:

Copy `backend/llm/providers.example.json` to `backend/llm/providers.json`, set your host/model,
and place `LLM_API_KEY` in your local `.env`. Never commit either local configuration file.
Install the dependencies described in the backend documentation and build the frontend with
`npm --prefix frontend ci` and `npm --prefix frontend run build`.
Run `.venv/bin/python -m uvicorn backend.api.app:app --host 127.0.0.1 --port 8000`.

## Published dataset

Only `2026-09-10-random-dev-test-v1` is distributed: 100 companies, 107 trajectories,
2,152 observations; company-level random dev/test 60/40. See the release manifest for hashes.
Unverified outcomes are not negative labels; the dataset does not support overall accuracy
without a complete reviewed positive/negative ground truth.

## Saved workflows

Three saved workflows are included in `backend/data/graphs/`: credit-risk monitoring,
its before-evolve version, and supplier credit-risk monitoring. With default runtime
storage they appear after startup, with their prompts, connections and memory settings.
The required `supplier_distress_rubric` skill definition is also included.
Run outputs, memory contents and chat history are not included.
If using `EAX_STUDIO_DATA_DIR`, copy these graph JSON files to its `graphs/` directory;
also copy `backend/data/skills/supplier_distress_rubric/` into its `skills/` directory.
Keep your existing definitions when an ID already exists.

Custom input datasets and shared reference inputs are described in
[custom-datasets.md](docs/custom-datasets.md).

## Publication boundary

This snapshot excludes local credentials/provider configuration, runtime databases, chat sessions,
memory stores and backups, outputs, logs, virtual environments and dependency directories.
Original source document content is preserved in the frozen dataset. Public source contacts/URLs
inside those documents are not workstation credentials.
