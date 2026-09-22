"""Compatibility import. Implementation: backend/features/execution/watcher.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.execution import watcher as _implementation
sys.modules[__name__] = _implementation
