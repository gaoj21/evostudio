"""Replace JSON state only after the complete new document reaches disk."""
import json
import os
import tempfile
from pathlib import Path


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Reading many saved documents: what each one boils down to, kept in memory
# until the file changes. Listing runs and batches parsed every file on every
# call — hundreds of MB, every three seconds from each open page.
# ---------------------------------------------------------------------------

import threading

_views: dict = {}
_views_lock = threading.Lock()


def file_view(path, name, make):
    """`make(document)` for the JSON file at `path`, computed once per version
    of the file (its modified time and size) and kept under `name`. None when
    the file is missing or unreadable."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    version = (stat.st_mtime_ns, stat.st_size)
    key = (name, str(path))
    with _views_lock:
        cached = _views.get(key)
    if cached is not None and cached[0] == version:
        return cached[1]
    try:
        with open(path, encoding='utf-8') as stream:
            value = make(json.load(stream))
    except (OSError, ValueError):
        return None
    with _views_lock:
        _views[key] = (version, value)
    return value


def forget_view(path):
    """Drop what is cached for `path` (a deleted file, say)."""
    path = str(Path(path))
    with _views_lock:
        for key in [k for k in _views if k[1] == path]:
            _views.pop(key, None)
