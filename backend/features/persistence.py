"""Replace JSON state only after the complete new document reaches disk."""
import json
import os
import re
import tempfile
import time
from pathlib import Path, PurePath

# On Windows a file another process has open without FILE_SHARE_DELETE (a
# OneDrive / Dropbox sync client, an antivirus scan, the search indexer)
# cannot be replaced for a moment: os.replace raises PermissionError. It
# clears by itself, so the replace is tried again a few times before failing.
REPLACE_ATTEMPTS = 5
REPLACE_DELAY = 0.05


def replace(source, destination):
    """os.replace, retried briefly while something else holds the file."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_DELAY * (attempt + 1))


def write_json(path, value, indent=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=indent, ensure_ascii=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Paths built from what a request carries.
# ---------------------------------------------------------------------------

def contains(root, path, allow_root=False):
    """Whether `path` is inside `root`, both already absolute and resolved.

    Compares path components, not strings: `str(path).startswith(str(root) +
    "/")` is wrong on Windows (backslashes, case-insensitive names) and lets
    `/data/ws-evil` pass for `/data/ws`. Works on PureWindowsPath as well.
    """
    # PurePath(x) would recast a PureWindowsPath to this platform's flavour.
    root = root if isinstance(root, PurePath) else PurePath(root)
    path = path if isinstance(path, PurePath) else PurePath(path)
    if not path.is_relative_to(root):
        return False
    return allow_root or path != root


def is_within(path, root, allow_root=False):
    """Whether `path` stays inside `root` once both are resolved (`..`,
    symlinks)."""
    return contains(Path(root).resolve(), Path(path).resolve(), allow_root)


class InvalidId(ValueError):
    """An id that cannot name one file or folder."""


_ID_PATTERN = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,199}')


def is_safe_id(value):
    """Whether `value` (a graph, batch, run, task id) can name one file or
    folder under a data directory: letters, digits, `.`, `_`, `-`, no `..`,
    no separator (`/` or `\\`), not empty, not ending in `.` (which Windows
    drops)."""
    return (isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None
            and '..' not in value and not value.endswith('.'))


def safe_id(value, kind='id'):
    """`value`, or InvalidId when it is not `is_safe_id`."""
    if not is_safe_id(value):
        raise InvalidId(f'Invalid {kind}: {value!r}')
    return value


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
