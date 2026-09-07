"""Short-term memory for the memory layer.

Process-local, session-bucketed working memory. Plain dict-of-lists with
optional JSON persistence — deliberately simple.
"""

import json
import threading
from pathlib import Path


class ShortTermMemory:
    """In-memory message log bucketed by session_id.

    persist_to: optional JSON file path; every mutation rewrites it.
    """

    def __init__(self, persist_to: str | None = None):
        self._sessions: dict[str, list] = {}
        self._persist_to = Path(persist_to) if persist_to else None
        self._lock = threading.Lock()
        if self._persist_to and self._persist_to.is_file():
            try:
                with open(self._persist_to, encoding="utf-8") as f:
                    self._sessions = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._sessions = {}

    def append(self, session_id: str, message) -> None:
        with self._lock:
            self._sessions.setdefault(session_id, []).append(message)
            self._flush()

    def get(self, session_id: str, n: int | None = None) -> list:
        """All messages for a session (last n when given)."""
        messages = self._sessions.get(session_id, [])
        return messages[-n:] if n else list(messages)

    def clear(self, session_id: str | None = None) -> None:
        """Clear one session, or all when session_id is None."""
        with self._lock:
            if session_id is None:
                self._sessions.clear()
            else:
                self._sessions.pop(session_id, None)
            self._flush()

    def sessions(self) -> list[str]:
        return sorted(self._sessions)

    def _flush(self) -> None:
        if not self._persist_to:
            return
        self._persist_to.parent.mkdir(parents=True, exist_ok=True)
        with open(self._persist_to, "w", encoding="utf-8") as f:
            json.dump(self._sessions, f, ensure_ascii=False, default=str)
