"""Compatibility import. Implementation: backend/features/data/source_apis.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.data import source_apis as _implementation
sys.modules[__name__] = _implementation
