"""Compatibility import. Implementation: backend/features/memory/table_store.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.memory import table_store as _implementation
sys.modules[__name__] = _implementation
