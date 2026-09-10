# EvoStudio

A modular workflow studio with independent chat agents, memory, evaluation and prompt improvement.

- Code maintenance: [MAINTENANCE.md](MAINTENANCE.md)
- Model API adapters: [backend/llm/README.md](backend/llm/README.md)
- Backend setup: [backend/README.md](backend/README.md)
- Frontend: [frontend/README.md](frontend/README.md)
- Frozen credit-risk dataset: [dataset README](projects/credit_risk/dataset/README.md)

## Local configuration

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

## Publication boundary

This snapshot excludes local credentials/provider configuration, runtime databases, chat sessions,
memory stores and backups, outputs, logs, virtual environments and dependency directories.
Original source document content is preserved in the frozen dataset. Public source contacts/URLs
inside those documents are not workstation credentials.
