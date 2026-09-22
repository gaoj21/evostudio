"""Compatibility import. Implementation: backend/features/workspace/projects.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.workspace import projects as _implementation
sys.modules[__name__] = _implementation
