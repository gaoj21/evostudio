"""Compatibility import. Implementation: backend/features/chat/chat_engine.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.chat import chat_engine as _implementation
sys.modules[__name__] = _implementation
