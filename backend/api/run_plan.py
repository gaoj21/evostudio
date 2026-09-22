"""Compatibility import. Implementation: backend/features/workflow/run_plan.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.workflow import run_plan as _implementation
sys.modules[__name__] = _implementation
