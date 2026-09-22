"""Compatibility import. Implementation: backend/features/chat/chat_worker.py.
Edit the feature module, not this file. The alias preserves shared module state.
"""
import sys
from backend.features.chat import chat_worker as _implementation
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    _implementation.main()
