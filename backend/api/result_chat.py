"""Compatibility import. Implementation: backend/features/chat/result_chat.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.chat import result_chat as _implementation
sys.modules[__name__] = _implementation
