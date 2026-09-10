from __future__ import annotations

import importlib
import sys


def test_tools_package_does_not_eagerly_import_optional_integrations():
    package_name = "evoagentx.tools"
    optional_module = f"{package_name}.search_google_f"
    sys.modules.pop(optional_module, None)
    sys.modules.pop(package_name, None)

    module = importlib.import_module(package_name)

    assert module.Tool is not None
    assert module.Toolkit is not None
    assert optional_module not in sys.modules


def test_requested_toolkit_is_cached_on_the_package():
    package_name = "evoagentx.tools"
    module = importlib.import_module(package_name)

    toolkit = module.SkillToolkit

    assert module.SkillToolkit is toolkit
    assert f"{package_name}.skill_tool" in sys.modules
