"""Compatibility import. Implementation: backend/features/execution/review.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.execution import review as _implementation
sys.modules[__name__] = _implementation
