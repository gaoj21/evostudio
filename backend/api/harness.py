"""Compatibility import. Implementation: backend/features/agents/harness.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.agents import harness as _implementation
sys.modules[__name__] = _implementation
