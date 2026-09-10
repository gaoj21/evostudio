"""Compatibility import. Implementation: backend/features/library/custom_tools.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.library import custom_tools as _implementation
sys.modules[__name__] = _implementation
