"""Compatibility import. Implementation: backend/features/data/datasets.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.data import datasets as _implementation
sys.modules[__name__] = _implementation
