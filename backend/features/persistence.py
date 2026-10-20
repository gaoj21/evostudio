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
#
# A view asked to persist is also kept on disk (one small SQLite file per data
# directory), so the first listing after a restart reads what each file boiled
# down to last time instead of parsing every file again. The disk copy is only
# a cache: a missing, locked or corrupt one is ignored and the view computed.
# ---------------------------------------------------------------------------

import sqlite3
import threading

_views: dict = {}
_views_lock = threading.Lock()


def _default_view_cache():
    from backend.api.studio_config import data_path
    return data_path("cache", "views.sqlite")


VIEW_CACHE = _default_view_cache()
# A cache another process holds is skipped, not waited for.
VIEW_CACHE_TIMEOUT = 0.2


class _DiskViews:
    """Views of files, keyed by (file, view name), each with the version
    (modified time, size) of the file it was computed from."""

    def __init__(self, location):
        self.location = Path(location)
        self.root = self.location.parent.parent
        self.lock = threading.Lock()
        self.connection = None
        self.broken = False

    def _connect(self):
        if self.connection is not None or self.broken:
            return self.connection
        for attempt in range(2):
            try:
                self.location.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(str(self.location), timeout=VIEW_CACHE_TIMEOUT,
                                             check_same_thread=False, isolation_level=None)
                try:
                    # Losing the newest rows to a crash costs a recomputation;
                    # waiting on the disk for every row would cost every listing.
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=OFF")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS views (path TEXT NOT NULL, name TEXT NOT NULL,"
                        " mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, value TEXT NOT NULL,"
                        " PRIMARY KEY (path, name))")
                    connection.execute("SELECT 1 FROM views LIMIT 1").fetchall()
                except sqlite3.Error:
                    connection.close()
                    raise
                self.connection = connection
                return connection
            except sqlite3.DatabaseError:
                if attempt:
                    break
                # Not a database any more: it only ever held copies, start over.
                for suffix in ("", "-wal", "-shm"):
                    try:
                        Path(str(self.location) + suffix).unlink(missing_ok=True)
                    except OSError:
                        break
            except (sqlite3.Error, OSError):
                break
        self.broken = True
        return None

    def key(self, path):
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def get(self, path, name, version):
        with self.lock:
            try:
                connection = self._connect()
                if connection is None:
                    return None
                row = connection.execute(
                    "SELECT mtime_ns, size, value FROM views WHERE path = ? AND name = ?",
                    (self.key(path), name)).fetchone()
            except sqlite3.Error:
                return None
        if row is None or (row[0], row[1]) != version:
            return None
        try:
            return json.loads(row[2])
        except ValueError:
            return None

    def put(self, path, name, version, value):
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        with self.lock:
            try:
                connection = self._connect()
                if connection is not None:
                    connection.execute(
                        "INSERT OR REPLACE INTO views (path, name, mtime_ns, size, value)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (self.key(path), name, version[0], version[1], encoded))
            except sqlite3.Error:
                pass

    def forget(self, path):
        with self.lock:
            try:
                connection = self._connect()
                if connection is not None:
                    connection.execute("DELETE FROM views WHERE path = ?", (self.key(path),))
            except sqlite3.Error:
                pass

    def close(self):
        with self.lock:
            if self.connection is not None:
                try:
                    self.connection.close()
                except sqlite3.Error:
                    pass
                self.connection = None


_disk = None
_disk_lock = threading.Lock()


def _disk_views():
    """The on-disk cache at VIEW_CACHE. Tests point that elsewhere for each
    test: the one before is closed rather than left holding files open."""
    global _disk
    location = Path(VIEW_CACHE)
    with _disk_lock:
        if _disk is None or _disk.location != location:
            if _disk is not None:
                _disk.close()
            _disk = _DiskViews(location)
        return _disk


def _version(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def file_view(path, name, make, persist=False):
    """`make(document)` for the JSON file at `path`, computed once per version
    of the file (its modified time and size) and kept under `name`. None when
    the file is missing or unreadable. `persist`: also kept on disk, so it
    outlives the process; the value must then survive a JSON round trip."""
    path = Path(path)
    version = _version(path)
    if version is None:
        return None
    key = (name, str(path))
    with _views_lock:
        cached = _views.get(key)
    if cached is not None and cached[0] == version:
        return cached[1]
    if persist:
        value = _disk_views().get(path, name, version)
        if value is not None:
            with _views_lock:
                _views[key] = (version, value)
            return value
    try:
        with open(path, encoding='utf-8') as stream:
            value = make(json.load(stream))
    except (OSError, ValueError):
        return None
    with _views_lock:
        _views[key] = (version, value)
    if persist and value is not None:
        _disk_views().put(path, name, version, value)
    return value


def file_version(path):
    """What a cached view of `path` is valid for: (modified time ns, size),
    None when the file is missing."""
    return _version(Path(path))


def cached_view(path, name, version=None):
    """The view `name` of `path` if one is kept (in memory or on disk) for
    the file as it is now — `version` when the caller already has it — else
    None. Never reads the file."""
    path = Path(path)
    version = version or _version(path)
    if version is None:
        return None
    key = (name, str(path))
    with _views_lock:
        cached = _views.get(key)
    if cached is not None and cached[0] == version:
        return cached[1]
    value = _disk_views().get(path, name, version)
    if value is not None:
        with _views_lock:
            _views[key] = (version, value)
    return value


def remember_view(path, name, value, persist=False, version=None):
    """Record `value` as the view `name` of the file just written at `path`,
    so the next listing need not read it back. `version`: the file's version
    as it was when `value` was read from it — a file changed since then then
    simply misses."""
    path = Path(path)
    version = version or _version(path)
    if version is None or value is None:
        return
    with _views_lock:
        _views[(name, str(path))] = (version, value)
    if persist:
        _disk_views().put(path, name, version, value)


def forget_view(path):
    """Drop what is cached for `path` (a deleted file, say)."""
    path = Path(path)
    text = str(path)
    with _views_lock:
        for key in [k for k in _views if k[1] == text]:
            _views.pop(key, None)
    _disk_views().forget(path)


def clear_memory_views():
    """Forget the in-memory views (what a restart does); the disk cache stays."""
    with _views_lock:
        _views.clear()
