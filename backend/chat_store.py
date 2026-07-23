"""SQLite persistence and legacy Ren conversation import."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from chat_config import DATA_DIR, PROJECT_ROOT


DB_PATH = DATA_DIR / "chat.db"
LEGACY_DB_PATH = PROJECT_ROOT / ".ren" / "ren.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class ChatStore:
    def __init__(self, path: Path = DB_PATH, legacy_path: Path = LEGACY_DB_PATH):
        self.path = path
        self.legacy_path = legacy_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.import_legacy()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_db(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    serialized_json TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS tool_steps (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT,
                    status TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                    ON conversations(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, created_at);
                """
            )

    def create_conversation(
        self,
        title: str = "New chat",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        identifier = conversation_id or str(uuid.uuid4())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations
                    (id, title, provider, model, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (identifier, title, provider, model, now, now),
            )
        return self.get_conversation(identifier) or {}

    def ensure_conversation(
        self,
        conversation_id: str,
        provider: Optional[str],
        model: Optional[str],
    ) -> Dict[str, Any]:
        existing = self.get_conversation(conversation_id)
        if existing:
            return existing
        return self.create_conversation(
            provider=provider,
            model=model,
            conversation_id=conversation_id,
        )

    def list_conversations(
        self,
        limit: int = 100,
        view: str = "active",
    ) -> List[Dict[str, Any]]:
        if view not in {"active", "archived"}:
            raise ValueError("Conversation view must be 'active' or 'archived'.")
        archive_filter = "archived_at IS NULL" if view == "active" else "archived_at IS NOT NULL"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM conversations
                WHERE {archive_filter}
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._conversation(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return self._conversation(row) if row else None

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        fields = ["updated_at = ?"]
        values: List[Any] = [utc_now()]
        for field, value in (("title", title), ("provider", provider), ("model", model)):
            if value is not None:
                fields.append(f"{field} = ?")
                values.append(value)
        if archived is not None:
            fields.append("archived_at = ?")
            values.append(utc_now() if archived else None)
        values.append(conversation_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE conversations SET {', '.join(fields)} WHERE id = ?",
                values,
            )
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conversation_id,),
            )
        return cursor.rowcount > 0

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        status: str = "complete",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        serialized: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
        message_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        identifier = message_id or str(uuid.uuid4())
        now = created_at or utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, conversation_id, role, content, status, provider, model,
                     serialized_json, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    conversation_id,
                    role,
                    content,
                    status,
                    provider,
                    model,
                    json.dumps(serialized, ensure_ascii=False) if serialized is not None else None,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        return {
            "id": identifier,
            "conversationId": conversation_id,
            "role": role,
            "content": content,
            "status": status,
            "provider": provider,
            "model": model,
            "createdAt": now,
            "metadata": metadata or {},
        }

    def list_messages(self, conversation_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM messages WHERE conversation_id = ?
                ORDER BY created_at ASC LIMIT ?
                """,
                (conversation_id, max(1, min(int(limit), 2000))),
            ).fetchall()
        return [self._message(row) for row in rows]

    def serialized_history(self, conversation_id: str) -> Optional[bytes]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT serialized_json FROM messages
                WHERE conversation_id = ? AND role = 'assistant'
                      AND serialized_json IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return str(row["serialized_json"]).encode("utf-8")

    def create_run(self, run_id: str, conversation_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO runs (id, conversation_id, status, created_at) VALUES (?, ?, 'running', ?)",
                (run_id, conversation_id, utc_now()),
            )

    def finish_run(self, run_id: str, status: str, error: Optional[str] = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, error = ?, completed_at = ? WHERE id = ?",
                (status, error, utc_now(), run_id),
            )

    def create_approval(
        self,
        approval_id: str,
        run_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals
                    (id, run_id, tool_name, arguments_json, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    approval_id,
                    run_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def resolve_approval(self, approval_id: str, approved: bool) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE approvals SET status = ?, resolved_at = ? WHERE id = ?",
                ("approved" if approved else "denied", utc_now(), approval_id),
            )

    def import_legacy(self) -> int:
        marker = "legacy_ren_import_v1"
        with self._lock, self._connect() as destination:
            if destination.execute("SELECT 1 FROM meta WHERE key = ?", (marker,)).fetchone():
                return 0
            if not self.legacy_path.exists() or self.legacy_path.resolve() == self.path.resolve():
                destination.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (marker, "no_source"),
                )
                return 0

            imported = 0
            source = sqlite3.connect(str(self.legacy_path))
            source.row_factory = sqlite3.Row
            try:
                tables = {
                    row[0]
                    for row in source.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if not {"conversations", "messages"}.issubset(tables):
                    result = "incompatible"
                else:
                    for row in source.execute("SELECT * FROM conversations").fetchall():
                        destination.execute(
                            """
                            INSERT OR IGNORE INTO conversations
                                (id, title, provider, model, created_at, updated_at, archived_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                row["id"],
                                row["title"],
                                row["provider"],
                                row["model"],
                                row["created_at"],
                                row["updated_at"],
                                row["archived_at"],
                            ),
                        )
                    for row in source.execute("SELECT * FROM messages").fetchall():
                        destination.execute(
                            """
                            INSERT OR IGNORE INTO messages
                                (id, conversation_id, role, content, status,
                                 metadata_json, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                row["id"],
                                row["conversation_id"],
                                row["role"],
                                row["content"],
                                row["status"],
                                json.dumps({"source": "legacy_ren"}),
                                row["created_at"],
                            ),
                        )
                        imported += 1
                    result = str(imported)
                destination.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (marker, result),
                )
            finally:
                source.close()
        return imported

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "provider": row["provider"],
            "model": row["model"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "archivedAt": row["archived_at"],
        }

    @staticmethod
    def _message(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "conversationId": row["conversation_id"],
            "role": row["role"],
            "content": row["content"],
            "status": row["status"],
            "provider": row["provider"],
            "model": row["model"],
            "createdAt": row["created_at"],
            "metadata": _loads(row["metadata_json"], {}),
        }


chat_store = ChatStore()
