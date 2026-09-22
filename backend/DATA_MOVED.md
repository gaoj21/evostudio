Studio's runtime data (workflows, runs, batches, memory, datasets,
workspaces) now lives in `studio-data/` at the repository root,
not under `backend/`. It was moved there automatically.

Point `EAX_STUDIO_DATA_DIR` somewhere else to override.
