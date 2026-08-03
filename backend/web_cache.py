"""Small bounded SQLite cache for fetched and extracted web results."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any


class WebCache:
    """Compressed TTL/LRU cache that requires no external service."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._path = str(path)
        self._max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS web_cache (
                cache_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload BLOB NOT NULL,
                byte_size INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS web_cache_expiry ON web_cache(expires_at)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS web_cache_lru ON web_cache(last_accessed_at)"
        )
        self._connection.commit()

    def __enter__(self) -> WebCache:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get(self, cache_key: str) -> Any | None:
        """Return a decoded unexpired value and update its LRU timestamp."""

        now = self._clock()
        with self._lock:
            row = self._connection.execute(
                "SELECT payload, expires_at FROM web_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            payload, expires_at = row
            if expires_at <= now:
                self._connection.execute("DELETE FROM web_cache WHERE cache_key = ?", (cache_key,))
                self._connection.commit()
                return None
            self._connection.execute(
                "UPDATE web_cache SET last_accessed_at = ? WHERE cache_key = ?",
                (now, cache_key),
            )
            self._connection.commit()
        try:
            return json.loads(zlib.decompress(payload).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, zlib.error):
            with self._lock:
                self._connection.execute("DELETE FROM web_cache WHERE cache_key = ?", (cache_key,))
                self._connection.commit()
            return None

    def set(self, cache_key: str, value: Any, *, ttl_seconds: float, kind: str = "page") -> bool:
        """Cache a JSON-compatible value, returning false when it cannot fit."""

        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload = zlib.compress(encoded, level=6)
        byte_size = len(payload)
        if byte_size > self._max_bytes:
            return False

        now = self._clock()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO web_cache (
                    cache_key, kind, payload, byte_size, created_at, expires_at, last_accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    kind = excluded.kind,
                    payload = excluded.payload,
                    byte_size = excluded.byte_size,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    last_accessed_at = excluded.last_accessed_at
                """,
                (cache_key, kind, payload, byte_size, now, now + ttl_seconds, now),
            )
            self._prune_locked(now)
            self._connection.commit()
        return True

    def delete_expired(self) -> int:
        """Delete expired entries and return the number removed."""

        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM web_cache WHERE expires_at <= ?", (self._clock(),)
            )
            self._connection.commit()
            return max(0, cursor.rowcount)

    def _prune_locked(self, now: float) -> None:
        self._connection.execute("DELETE FROM web_cache WHERE expires_at <= ?", (now,))
        row = self._connection.execute("SELECT COALESCE(SUM(byte_size), 0) FROM web_cache").fetchone()
        total = int(row[0] if row else 0)
        while total > self._max_bytes:
            oldest = self._connection.execute(
                "SELECT cache_key, byte_size FROM web_cache ORDER BY last_accessed_at ASC LIMIT 1"
            ).fetchone()
            if oldest is None:
                break
            self._connection.execute("DELETE FROM web_cache WHERE cache_key = ?", (oldest[0],))
            total -= int(oldest[1])
