"""Shared setup for the Studio backend tests.

Test files here carry a `studio_` prefix where the basename could clash with
one under `tests/src/`: pytest imports test modules by basename when there is
no package `__init__.py`, and two `test_skills.py` files abort collection.

Studio's backend is imported through the regular `backend.api` package.
Every test also runs against throwaway data directories: the module-level
paths point at the developer's real `studio-data/`, and a test that creates a
graph or saves a tool must never touch it.
"""

import os
import tempfile

# Set before anything under backend.api is imported: each module binds its
# data directory from studio_config at import time, so an environment variable
# that arrives later is too late. This is the floor; per-test patches below
# add isolation between tests on top of it.
os.environ.setdefault(
    "EAX_STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="eax-studio-tests-"))
import pytest

# Skipping is for a bare checkout without the studio extra. In CI the deps are
# installed, so a skip there means the dependency list drifted — and a silently
# skipped suite looks exactly like a passing one.
if os.environ.get("CI"):
    import fastapi  # noqa: F401  (fail loudly rather than skip)
else:
    pytest.importorskip(
        "fastapi",
        reason="Studio backend needs the `studio` extra: pip install -e '.[studio]'",
    )


@pytest.fixture
def studio_data(tmp_path, monkeypatch):
    """Point graphs, custom tools and skills at a temporary directory."""
    from backend.api import custom_tools
    from backend.api import graphs
    from backend.api import skills_api
    monkeypatch.setattr(graphs, "GRAPHS_DIR", tmp_path / "graphs")
    monkeypatch.setattr(custom_tools, "TOOLS_DIR", tmp_path / "tools")
    monkeypatch.setattr(skills_api, "SKILLS_DIR", tmp_path / "skills")
    return tmp_path


def make_task(name, inputs=(), outputs=(), **extra):
    """A minimal valid LLM task; prompts reference every declared input.

    The framework rejects a task whose input is not used in its prompt, so the
    helper wires that up rather than leaving each test to remember it.
    """
    prompt = extra.pop("prompt", None)
    if prompt is None:
        refs = " ".join("{" + n + "}" for n in inputs)
        prompt = f"Do something with {refs}".strip()
    return {
        "name": name,
        "description": extra.pop("description", f"task {name}"),
        "prompt": prompt,
        "system_prompt": extra.pop("system_prompt", ""),
        "parse_mode": extra.pop("parse_mode", "str"),
        "inputs": [{"name": n, "type": "str", "description": n, "required": True}
                   for n in inputs],
        "outputs": [{"name": n, "type": "str", "description": n, "required": True}
                    for n in outputs],
        **extra,
    }


def make_graph(tasks, edges=(), **extra):
    return {
        "id": extra.pop("id", "test-graph"),
        "name": extra.pop("name", "Test graph"),
        "goal": extra.pop("goal", "a goal for the test"),
        "output_dir": "runs",
        "tasks": list(tasks),
        "edges": [{"source": s, "target": t} for s, t in edges],
        **extra,
    }


# Every module-level data path in the backend, as (module, attribute). Each
# one defaults to the developer's real `studio-data/`.
_DATA_PATHS = [
    ("agent_api", "MEMORY_DIR"), ("batch", "BATCHES_DIR"),
    ("evolve_api", "EVOLVE_DIR"), ("custom_tools", "TOOLS_DIR"),
    ("memory_store", "MEMORY_DIR"), ("scheduler", "SCHEDULES_DIR"),
    ("runner", "RUNS_DIR"), ("graphs", "DATA_DIR"), ("graphs", "GRAPHS_DIR"),
    ("stm_store", "STM_DIR"), ("skills_api", "SKILLS_DIR"),
    ("review", "REVIEWS_DIR"), ("review", "RUNS_DIR"), ("review", "BATCHES_DIR"),
    ("workspace", "WORKSPACE_DIR"), ("watcher", "WATCH_DIR"),
    ("table_store", "TABLES_DIR"), ("memory_reset", "BACKUPS"),
    ("evaluator_tools", "DRAFTS_DIR"),
]


@pytest.fixture(autouse=True)
def _never_touch_real_data(tmp_path, monkeypatch):
    """Redirect every backend data directory into a throwaway one.

    Tests patched the one or two paths they meant to use, which left the rest
    pointing at the real `studio-data/`: a run started by a test wrote its
    workspace, memory and run record into the developer's own history. A test
    that patches its own path still wins — this only sets the floor.
    """
    import importlib

    # Only these may be absent, and only for want of an optional extra.
    optional = {"evolve_api"}
    for module_name, attr in _DATA_PATHS:
        try:
            module = importlib.import_module(f"backend.api.{module_name}")
        except ImportError:
            if module_name in optional:
                continue
            raise          # a redirect that quietly did not happen leaked once
        if not hasattr(module, attr):
            raise AttributeError(
                f"backend.api.{module_name} has no {attr}; _DATA_PATHS is stale")
        monkeypatch.setattr(module, attr, tmp_path / "data" / attr.lower())
