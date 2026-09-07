from __future__ import annotations

import importlib
import sys


def test_memory_package_does_not_eagerly_import_rag_components():
    package_name = "evoagentx.memory"
    long_term_module = f"{package_name}.long_term_memory"
    sys.modules.pop(long_term_module, None)
    sys.modules.pop(package_name, None)

    module = importlib.import_module(package_name)

    assert module.ShortTermMemory is not None
    assert long_term_module not in sys.modules
