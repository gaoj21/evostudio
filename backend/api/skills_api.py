"""Compatibility import. Implementation: backend/features/library/skills_api.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.library import skills_api as _implementation
sys.modules[__name__] = _implementation
