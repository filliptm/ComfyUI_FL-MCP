"""SQLite-backed persistent conversations for Ren."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).parent.parent
REN_DIR = PROJECT_ROOT / ".ren"
DB_PATH = REN_DIR / "ren.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Optional[Dict[str, Any]]) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def _json_loads(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        return {}


class ConversationStore:
    """Small synchronous SQLite store.

    FastAPI routes call this from the event loop, but every operation is tiny and
    protected by a lock. This keeps Ren dependency-free and durable without
    bringing in an ORM or migration framework.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    claude_session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'complete',
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS chat_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    type TEXT NOT NULL,
                    path TEXT,
                    url TEXT,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                    ON conversations(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
                    ON messages(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_chat_events_conversation_created
                    ON chat_events(conversation_id, created_at);
                """
            )

    def ensure_conversation(
        self,
        *,
        conversation_id: Optional[str],
        session_id: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        cid = conversation_id or str(uuid.uuid4())
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (cid,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE conversations
                    SET session_id = ?, provider = COALESCE(?, provider),
                        model = COALESCE(?, model), updated_at = ?
                    WHERE id = ?
                    """,
                    (session_id, provider, model, now, cid),
                )
                row = conn.execute(
                    "SELECT * FROM conversations WHERE id = ?",
                    (cid,),
                ).fetchone()
                return self._conversation_from_row(row)

            conn.execute(
                """
                INSERT INTO conversations
                    (id, title, session_id, provider, model, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cid, title or "New chat", session_id, provider, model, now, now),
            )
            return {
                "id": cid,
                "title": title or "New chat",
                "sessionId": session_id,
                "provider": provider,
                "model": model,
                "claudeSessionId": None,
                "createdAt": now,
                "updatedAt": now,
                "archivedAt": None,
            }

    def list_conversations(self, *, include_archived: bool = False, limit: int = 100) -> List[Dict[str, Any]]:
        query = "SELECT * FROM conversations"
        params: List[Any] = []
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._lock, self._connect() as conn:
            return [self._conversation_from_row(row) for row in conn.execute(query, params).fetchall()]

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            return self._conversation_from_row(row) if row else None

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        claude_session_id: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_conversation(conversation_id)
        if not existing:
            return None

        now = utc_now()
        fields = ["updated_at = ?"]
        params: List[Any] = [now]
        updates = {
            "title": title,
            "provider": provider,
            "model": model,
            "claude_session_id": claude_session_id,
        }
        for column, value in updates.items():
            if value is not None:
                fields.append(f"{column} = ?")
                params.append(value)
        if archived is not None:
            fields.append("archived_at = ?")
            params.append(now if archived else None)
        params.append(conversation_id)

        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE conversations SET {', '.join(fields)} WHERE id = ?",
                params,
            )
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            return cursor.rowcount > 0

    def append_message(
        self,
        conversation_id: str,
        *,
        role: str,
        content: str,
        status: str = "complete",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        message_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (id, conversation_id, role, content, status, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, conversation_id, role, content, status, now, _json_dumps(metadata)),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        return {
            "id": message_id,
            "conversationId": conversation_id,
            "role": role,
            "content": content,
            "status": status,
            "createdAt": now,
            "metadata": metadata or {},
        }

    def list_messages(self, conversation_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (conversation_id, max(1, min(limit, 2000))),
            ).fetchall()
            return [self._message_from_row(row) for row in rows]

    def append_event(self, conversation_id: str, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "unknown")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_events (conversation_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (conversation_id, event_type, json.dumps(event, ensure_ascii=False), utc_now()),
            )

    def add_artifact(
        self,
        conversation_id: str,
        *,
        artifact_type: str,
        path: Optional[str] = None,
        url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        artifact_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts
                    (id, conversation_id, type, path, url, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, conversation_id, artifact_type, path, url, now, _json_dumps(metadata)),
            )
        return {
            "id": artifact_id,
            "conversationId": conversation_id,
            "type": artifact_type,
            "path": path,
            "url": url,
            "createdAt": now,
            "metadata": metadata or {},
        }

    def _conversation_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "sessionId": row["session_id"],
            "provider": row["provider"],
            "model": row["model"],
            "claudeSessionId": row["claude_session_id"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "archivedAt": row["archived_at"],
        }

    def _message_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "conversationId": row["conversation_id"],
            "role": row["role"],
            "content": row["content"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "metadata": _json_loads(row["metadata_json"]),
        }


conversation_store = ConversationStore()
