"""Compatibility import. Implementation: backend/features/evaluation/saved_result_evolution.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.evaluation import saved_result_evolution as _implementation
sys.modules[__name__] = _implementation
