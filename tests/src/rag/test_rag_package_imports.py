from __future__ import annotations

import importlib
import sys


def test_rag_package_does_not_eagerly_import_provider_integrations():
    package_name = "evoagentx.rag"
    azure_module = f"{package_name}.embeddings.azure_openai_embedding"
    sys.modules.pop(azure_module, None)
    sys.modules.pop(package_name, None)

    module = importlib.import_module(package_name)

    assert "RAGEngine" in module.__all__
    assert azure_module not in sys.modules
