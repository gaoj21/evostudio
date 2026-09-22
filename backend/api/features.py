"""Feature boundaries and runtime availability for Studio."""

from __future__ import annotations

from importlib.util import find_spec

from backend.features import plugins

CORE_FEATURES = (
    "graphs",
    "runs",
    "batch",
    "workspace",
    "tools",
    "skills",
    "memory",
)

EXPERIMENTAL_FEATURES = {
    "evolve": {
        "description": "MIPRO prompt optimization",
        "extra": "optimizers",
        "dependencies": ("dspy", "optuna"),
    },
}


def catalog(overrides: dict[str, tuple[bool, str | None]] | None = None) -> dict:
    """Describe stable features and whether experimental integrations can run."""
    overrides = overrides or {}
    experimental = []
    for name, metadata in EXPERIMENTAL_FEATURES.items():
        missing = [dep for dep in metadata["dependencies"] if find_spec(dep) is None]
        available = not missing
        reason = f"Missing packages: {', '.join(missing)}" if missing else None
        if name in overrides:
            available, reason = overrides[name]
        experimental.append({
            "name": name,
            "stability": "experimental",
            "available": available,
            "unavailable_reason": reason,
            "description": metadata["description"],
            "install_extra": metadata["extra"],
        })
    return {
        "core": [
            {"name": name, "stability": "stable", "available": True}
            for name in CORE_FEATURES
        ],
        "experimental": experimental,
        # Task-specific code Studio loaded from project folders.
        "projects": plugins.catalog(),
    }
