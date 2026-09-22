"""Compatibility import. Implementation: backend/features/workspace/workspace_api.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.workspace import workspace_api as _implementation
sys.modules[__name__] = _implementation
