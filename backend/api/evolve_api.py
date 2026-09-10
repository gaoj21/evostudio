"""Compatibility import. Implementation: backend/features/evaluation/evolve_api.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.evaluation import evolve_api as _implementation
sys.modules[__name__] = _implementation
