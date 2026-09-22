"""Compatibility import. Implementation: backend/features/memory/mem0_api.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.memory import mem0_api as _implementation
sys.modules[__name__] = _implementation
