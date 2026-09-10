"""Compatibility import. Implementation: backend/features/workspace/workspace.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.workspace import workspace as _implementation
sys.modules[__name__] = _implementation
