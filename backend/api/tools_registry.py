"""Compatibility import. Implementation: backend/features/library/tools_registry.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.library import tools_registry as _implementation
sys.modules[__name__] = _implementation
