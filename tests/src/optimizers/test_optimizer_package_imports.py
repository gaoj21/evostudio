"""The optimizers package imports without optuna; MiproOptimizer says why not.

Run in a fresh interpreter: simulating "optuna is not installed" inside this
process cannot work once another test has already imported it — optuna's own
submodules are then in sys.modules, and the test was passing or failing
depending on what ran before it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

PROBE = """
import builtins, importlib, sys
real_import = builtins.__import__

def guarded(name, *args, **kwargs):
    if name == "optuna" or name.startswith("optuna."):
        raise ModuleNotFoundError("No module named 'optuna'", name="optuna")
    return real_import(name, *args, **kwargs)

for module in [name for name in sys.modules if name == "optuna" or name.startswith("optuna.")]:
    del sys.modules[module]
builtins.__import__ = guarded

package = importlib.import_module("evoagentx.optimizers")
assert "MapElitesOptimizer" in package.__all__, package.__all__
try:
    package.MiproOptimizer
except ModuleNotFoundError as error:
    assert "optional dependency 'optuna'" in str(error), str(error)
else:
    raise AssertionError("MiproOptimizer should refuse to load without optuna")
print("ok")
"""


def test_optimizers_package_imports_without_optuna():
    finished = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{REPO_ROOT}:{REPO_ROOT / 'backend'}"},
    )

    assert finished.returncode == 0, finished.stderr[-2000:]
    assert "ok" in finished.stdout
