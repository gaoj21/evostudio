"""Central configuration for EvoAgentX Studio.

Runtime state used to be rooted independently in every backend module.  Keep
one source of truth and allow deployments to place mutable data outside the
source checkout with ``EAX_STUDIO_DATA_DIR``.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _configured_data_dir() -> Path:
    configured = os.environ.get("EAX_STUDIO_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    # Repository layout default. Production and packaged installations
    # should set EAX_STUDIO_DATA_DIR to a durable application-data directory.
    return REPO_ROOT / "backend" / "data"


DATA_DIR = _configured_data_dir()


def data_path(*parts: str) -> Path:
    """Return a path below Studio's configured runtime-data root."""
    return DATA_DIR.joinpath(*parts)

