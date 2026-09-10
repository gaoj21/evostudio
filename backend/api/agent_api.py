"""Compatibility import. Implementation: backend/features/chat/agent_api.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.chat import agent_api as _implementation
sys.modules[__name__] = _implementation
