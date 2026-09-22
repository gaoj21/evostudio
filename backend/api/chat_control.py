"""Compatibility import. Implementation: backend/features/chat/chat_control.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.chat import chat_control as _implementation
sys.modules[__name__] = _implementation
