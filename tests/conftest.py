"""Keep every test away from the developer's real `studio-data/`.

Studio binds its data directory (studio_config.DATA_DIR) once, at import.
tests/studio/conftest.py set it, but a full run collects tests/api first, and
those import the backend before that conftest loads: the harness store then
wrote test agents and sessions into the real studio-data/harness. Setting it
here, at the root, runs before any test module is imported.
"""

import os
import tempfile

os.environ.setdefault(
    "EAX_STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="eax-studio-tests-"))
