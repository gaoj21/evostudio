"""Compatibility import. Implementation: backend/features/data/preprocess.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.data import preprocess as _implementation
sys.modules[__name__] = _implementation
