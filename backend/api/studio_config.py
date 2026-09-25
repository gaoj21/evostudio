"""Central configuration for EvoAgentX Studio.

Runtime state used to be rooted independently in every backend module.  Keep
one source of truth and allow deployments to place mutable data outside the
source checkout with ``EAX_STUDIO_DATA_DIR``.

Everything a user builds or a run produces lives under one root — by default
``studio-data/`` beside the code, never inside ``backend/``.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Credentials live in a .env, either in the checkout or beside it — a shared
# file one level up is a common way to keep company credentials out of the
# repository. Loaded here because this module is imported before anything
# that needs them, and never over an already-exported value.
ENV_PATHS = (REPO_ROOT / ".env", REPO_ROOT.parent / ".env")


def load_env() -> list[str]:
    """Load the first .env found; returns the paths it read."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return []
    read = []
    for path in ENV_PATHS:
        if path.is_file() and load_dotenv(path, override=False):
            read.append(str(path))
            break
    return read


LOADED_ENV = load_env()

# The server process loads several native packages that each bundle an
# OpenMP runtime (torch, faiss, scikit-learn). Set before any of them is
# imported; a value the deployment exported wins. See
# backend/features/chat/chat_control.worker_env for the workers.
for _key, _value in (("OMP_NUM_THREADS", "1"), ("MKL_NUM_THREADS", "1"),
                     ("KMP_DUPLICATE_LIB_OK", "TRUE")):
    os.environ.setdefault(_key, _value)


# Open files: macOS starts a process at 256, and a batch run node by node
# keeps a whole DataLoader batch of runs open at once — each with its event
# loop and its memory stores. Raise the soft limit to what the system allows
# (capped: macOS refuses "unlimited"); worker processes inherit it.
OPEN_FILES_WANTED = 10240


def raise_open_file_limit(wanted: int = OPEN_FILES_WANTED) -> int | None:
    try:
        import resource
    except ImportError:            # not on this platform
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
    if soft != resource.RLIM_INFINITY and soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError):
            return soft
        return target
    return soft


raise_open_file_limit()


# What a user built and what their runs produced: workflows, runs, batches,
# memory, datasets, workspaces. It is their data, not part of the backend, so
# it sits beside the code rather than inside it. Deployments should point
# EAX_STUDIO_DATA_DIR at a durable application-data directory.
DEFAULT_DATA_DIR = REPO_ROOT / "studio-data"
LEGACY_DATA_DIR = REPO_ROOT / "backend" / "data"


def _configured_data_dir() -> Path:
    configured = os.environ.get("EAX_STUDIO_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_DATA_DIR


def _migrate_legacy_data(target: Path) -> None:
    """Move data left in the old backend/data/ to the new root, once.

    Nothing is merged and nothing is deleted: if both exist, the new root
    wins and the old one is left untouched for the user to look at.
    """
    if target.exists() or not LEGACY_DATA_DIR.is_dir():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_DATA_DIR.rename(target)
    except OSError:
        return
    note = LEGACY_DATA_DIR.parent / "DATA_MOVED.md"
    try:
        note.write_text(
            "Studio's runtime data (workflows, runs, batches, memory, datasets,\n"
            f"workspaces) now lives in `{target.name}/` at the repository root,\n"
            "not under `backend/`. It was moved there automatically.\n\n"
            "Point `EAX_STUDIO_DATA_DIR` somewhere else to override.\n",
            encoding="utf-8")
    except OSError:
        pass


DATA_DIR = _configured_data_dir()
if not os.environ.get("EAX_STUDIO_DATA_DIR"):
    _migrate_legacy_data(DATA_DIR)


def data_path(*parts: str) -> Path:
    """Return a path below Studio's configured runtime-data root."""
    return DATA_DIR.joinpath(*parts)

