"""Compatibility import. Implementation: backend/features/workflow/runner.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.workflow import runner as _implementation
sys.modules[__name__] = _implementation
