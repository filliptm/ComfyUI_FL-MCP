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
                    workflow_id TEXT,
                    workflow_path TEXT,
                    workflow_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT,
                    active_leaf_message_id TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    parent_message_id TEXT,
                    revision_root_id TEXT,
                    revision_index INTEGER NOT NULL DEFAULT 1,
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
            self._ensure_column(
                connection,
                "conversations",
                "active_leaf_message_id",
                "TEXT",
            )
            self._ensure_column(connection, "conversations", "workflow_id", "TEXT")
            self._ensure_column(connection, "conversations", "workflow_path", "TEXT")
            self._ensure_column(connection, "conversations", "workflow_name", "TEXT")
            self._ensure_column(connection, "messages", "parent_message_id", "TEXT")
            self._ensure_column(connection, "messages", "revision_root_id", "TEXT")
            self._ensure_column(
                connection,
                "messages",
                "revision_index",
                "INTEGER NOT NULL DEFAULT 1",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_revision
                ON messages(conversation_id, revision_root_id, revision_index)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_workflow_updated
                ON conversations(workflow_id, updated_at DESC)
                """
            )
            self._migrate_linear_message_history(connection)

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _migrate_linear_message_history(connection: sqlite3.Connection) -> None:
        conversations = connection.execute(
            """
            SELECT id FROM conversations
            WHERE active_leaf_message_id IS NULL
              AND EXISTS (
                  SELECT 1 FROM messages WHERE conversation_id = conversations.id
              )
            """
        ).fetchall()
        for conversation in conversations:
            previous_id = None
            rows = connection.execute(
                """
                SELECT id, parent_message_id, revision_root_id
                FROM messages WHERE conversation_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (conversation["id"],),
            ).fetchall()
            for row in rows:
                parent_id = row["parent_message_id"] or previous_id
                revision_root_id = row["revision_root_id"] or row["id"]
                connection.execute(
                    """
                    UPDATE messages
                    SET parent_message_id = ?, revision_root_id = ?, revision_index = 1
                    WHERE id = ?
                    """,
                    (parent_id, revision_root_id, row["id"]),
                )
                previous_id = row["id"]
            connection.execute(
                "UPDATE conversations SET active_leaf_message_id = ? WHERE id = ?",
                (previous_id, conversation["id"]),
            )

    def create_conversation(
        self,
        title: str = "New chat",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        conversation_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        workflow_path: Optional[str] = None,
        workflow_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        identifier = conversation_id or str(uuid.uuid4())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations
                    (id, title, provider, model, workflow_id, workflow_path,
                     workflow_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    title,
                    provider,
                    model,
                    workflow_id,
                    workflow_path,
                    workflow_name,
                    now,
                    now,
                ),
            )
        return self.get_conversation(identifier) or {}

    def ensure_conversation(
        self,
        conversation_id: str,
        provider: Optional[str],
        model: Optional[str],
        workflow_id: Optional[str] = None,
        workflow_path: Optional[str] = None,
        workflow_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        existing = self.get_conversation(conversation_id)
        if existing:
            existing_workflow = existing["workflow"]
            if existing_workflow and not workflow_id:
                raise ValueError("This conversation requires its workflow to be active.")
            if workflow_id and not existing_workflow:
                raise ValueError("Attach this unassigned conversation to the workflow before continuing.")
            if workflow_id and existing_workflow["id"] != workflow_id:
                raise ValueError("This conversation belongs to a different workflow.")
            return existing
        return self.create_conversation(
            provider=provider,
            model=model,
            conversation_id=conversation_id,
            workflow_id=workflow_id,
            workflow_path=workflow_path,
            workflow_name=workflow_name,
        )

    def list_conversations(
        self,
        limit: int = 100,
        view: str = "active",
        workflow_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if view not in {"active", "archived"}:
            raise ValueError("Conversation view must be 'active' or 'archived'.")
        archive_filter = "archived_at IS NULL" if view == "active" else "archived_at IS NOT NULL"
        workflow_filter = " AND workflow_id = ?" if workflow_id else ""
        values: List[Any] = []
        if workflow_id:
            values.append(workflow_id)
        values.append(max(1, min(int(limit), 500)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM conversations
                WHERE {archive_filter}{workflow_filter}
                ORDER BY updated_at DESC LIMIT ?
                """,
                values,
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
        workflow_path: Optional[str] = None,
        workflow_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        fields = ["updated_at = ?"]
        values: List[Any] = [utc_now()]
        for field, value in (
            ("title", title),
            ("provider", provider),
            ("model", model),
            ("workflow_path", workflow_path),
            ("workflow_name", workflow_name),
        ):
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

    def bind_conversation(
        self,
        conversation_id: str,
        workflow_id: str,
        workflow_path: Optional[str] = None,
        workflow_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        conversation = self.get_conversation(conversation_id)
        if not conversation:
            return None
        current = conversation["workflow"]
        if current and current["id"] != workflow_id:
            raise ValueError("This conversation already belongs to a different workflow.")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE conversations
                SET workflow_id = ?, workflow_path = ?, workflow_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (workflow_id, workflow_path, workflow_name, utc_now(), conversation_id),
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
        parent_message_id: Optional[str] = None,
        revision_root_id: Optional[str] = None,
        revision_index: int = 1,
        branch_from_active: bool = True,
    ) -> Dict[str, Any]:
        identifier = message_id or str(uuid.uuid4())
        now = created_at or utc_now()
        with self._lock, self._connect() as connection:
            if branch_from_active:
                conversation = connection.execute(
                    "SELECT active_leaf_message_id FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if conversation:
                    parent_message_id = conversation["active_leaf_message_id"]
            revision_root_id = revision_root_id or identifier
            connection.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, conversation_id, parent_message_id, revision_root_id,
                     revision_index, role, content, status, provider, model,
                     serialized_json, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    conversation_id,
                    parent_message_id,
                    revision_root_id,
                    revision_index,
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
                """
                UPDATE conversations
                SET updated_at = ?, active_leaf_message_id = ?
                WHERE id = ?
                """,
                (now, identifier, conversation_id),
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
            "parentMessageId": parent_message_id,
            "revision": {
                "rootId": revision_root_id,
                "index": revision_index,
                "count": revision_index,
            },
        }

    def list_messages(self, conversation_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            conversation = connection.execute(
                "SELECT active_leaf_message_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conversation or not conversation["active_leaf_message_id"]:
                return []
            rows = connection.execute(
                """
                WITH RECURSIVE active_path AS (
                    SELECT messages.*, 0 AS path_depth
                    FROM messages WHERE id = ? AND conversation_id = ?
                    UNION ALL
                    SELECT parent.*, active_path.path_depth + 1
                    FROM messages AS parent
                    JOIN active_path ON active_path.parent_message_id = parent.id
                )
                SELECT * FROM active_path
                ORDER BY path_depth DESC LIMIT ?
                """,
                (
                    conversation["active_leaf_message_id"],
                    conversation_id,
                    max(1, min(int(limit), 2000)),
                ),
            ).fetchall()
            roots = {
                str(row["revision_root_id"])
                for row in rows
                if row["role"] == "user" and row["revision_root_id"]
            }
            counts = {}
            for root_id in roots:
                count_row = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM messages
                    WHERE conversation_id = ? AND revision_root_id = ?
                          AND role = 'user'
                    """,
                    (conversation_id, root_id),
                ).fetchone()
                counts[root_id] = int(count_row["count"])
        return [
            self._message(
                row,
                revision_count=counts.get(str(row["revision_root_id"]), 1),
            )
            for row in rows
        ]

    def list_model_messages(
        self,
        conversation_id: str,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Read the active branch without loading serialized runs or full tool results."""
        with self._lock, self._connect() as connection:
            conversation = connection.execute(
                "SELECT active_leaf_message_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conversation or not conversation["active_leaf_message_id"]:
                return []
            path_rows = connection.execute(
                """
                WITH RECURSIVE active_path AS (
                    SELECT id, parent_message_id, 0 AS path_depth
                    FROM messages WHERE id = ? AND conversation_id = ?
                    UNION ALL
                    SELECT parent.id, parent.parent_message_id, active_path.path_depth + 1
                    FROM messages AS parent
                    JOIN active_path ON active_path.parent_message_id = parent.id
                )
                SELECT id FROM active_path
                ORDER BY path_depth DESC LIMIT ?
                """,
                (
                    conversation["active_leaf_message_id"],
                    conversation_id,
                    max(1, min(int(limit), 2000)),
                ),
            ).fetchall()
            message_ids = [str(row["id"]) for row in path_rows]
            if not message_ids:
                return []
            placeholders = ",".join("?" for _ in message_ids)
            rows = connection.execute(
                f"""
                SELECT
                    messages.id,
                    messages.conversation_id,
                    messages.role,
                    messages.content,
                    messages.status,
                    messages.provider,
                    messages.model,
                    messages.created_at,
                    messages.parent_message_id,
                    json_extract(messages.metadata_json, '$.usage') AS usage_json,
                    json_extract(messages.metadata_json, '$.attachments') AS attachments_json,
                    json_extract(messages.metadata_json, '$.claudeSessionId') AS claude_session_id,
                    json_extract(messages.metadata_json, '$.codexThreadId') AS codex_thread_id,
                    (
                        SELECT json_group_array(json_object(
                            'name', json_extract(tool.value, '$.name'),
                            'status', json_extract(tool.value, '$.status')
                        ))
                        FROM json_each(messages.metadata_json, '$.toolSteps') AS tool
                    ) AS tool_steps_json
                FROM messages
                WHERE messages.id IN ({placeholders})
                """,
                message_ids,
            ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        messages = []
        for message_id in message_ids:
            row = by_id[message_id]
            metadata = {}
            for key, column in (
                ("usage", "usage_json"),
                ("attachments", "attachments_json"),
                ("toolSteps", "tool_steps_json"),
            ):
                value = _loads(row[column], None)
                if value:
                    metadata[key] = value
            if row["claude_session_id"]:
                metadata["claudeSessionId"] = row["claude_session_id"]
            if row["codex_thread_id"]:
                metadata["codexThreadId"] = row["codex_thread_id"]
            messages.append({
                "id": row["id"],
                "conversationId": row["conversation_id"],
                "role": row["role"],
                "content": row["content"],
                "status": row["status"],
                "provider": row["provider"],
                "model": row["model"],
                "createdAt": row["created_at"],
                "metadata": metadata,
                "parentMessageId": row["parent_message_id"],
            })
        return messages

    def list_messages_page(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        before_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the newest bounded slice of the active branch before a cursor."""
        page_limit = max(1, min(int(limit), 100))
        with self._lock, self._connect() as connection:
            conversation = connection.execute(
                "SELECT active_leaf_message_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conversation or not conversation["active_leaf_message_id"]:
                return {"messages": [], "hasMore": False, "nextBefore": None}
            if before_message_id:
                cursor = connection.execute(
                    """
                    SELECT parent_message_id FROM messages
                    WHERE id = ? AND conversation_id = ?
                    """,
                    (before_message_id, conversation_id),
                ).fetchone()
                if not cursor:
                    raise ValueError("Message cursor was not found in this conversation.")
                anchor_id = cursor["parent_message_id"]
            else:
                anchor_id = conversation["active_leaf_message_id"]
            if not anchor_id:
                return {"messages": [], "hasMore": False, "nextBefore": None}
            rows = connection.execute(
                """
                WITH RECURSIVE active_path AS (
                    SELECT messages.*, 0 AS path_depth
                    FROM messages WHERE id = ? AND conversation_id = ?
                    UNION ALL
                    SELECT parent.*, active_path.path_depth + 1
                    FROM messages AS parent
                    JOIN active_path ON active_path.parent_message_id = parent.id
                ), page AS (
                    SELECT * FROM active_path
                    ORDER BY path_depth ASC LIMIT ?
                )
                SELECT * FROM page ORDER BY path_depth DESC
                """,
                (anchor_id, conversation_id, page_limit),
            ).fetchall()
            roots = {
                str(row["revision_root_id"])
                for row in rows
                if row["role"] == "user" and row["revision_root_id"]
            }
            counts = {}
            for root_id in roots:
                count_row = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM messages
                    WHERE conversation_id = ? AND revision_root_id = ?
                          AND role = 'user'
                    """,
                    (conversation_id, root_id),
                ).fetchone()
                counts[root_id] = int(count_row["count"])
        messages = [
            self._message(
                row,
                revision_count=counts.get(str(row["revision_root_id"]), 1),
            )
            for row in rows
        ]
        has_more = bool(rows and rows[0]["parent_message_id"])
        return {
            "messages": messages,
            "hasMore": has_more,
            "nextBefore": messages[0]["id"] if has_more and messages else None,
        }

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if not row:
                return None
            count_row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM messages
                WHERE conversation_id = ? AND revision_root_id = ?
                      AND role = 'user'
                """,
                (row["conversation_id"], row["revision_root_id"]),
            ).fetchone()
        return self._message(row, revision_count=int(count_row["count"] or 1))

    def next_revision_index(self, conversation_id: str, revision_root_id: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision_index), 0) + 1 AS next_index
                FROM messages
                WHERE conversation_id = ? AND revision_root_id = ?
                      AND role = 'user'
                """,
                (conversation_id, revision_root_id),
            ).fetchone()
        return int(row["next_index"])

    def select_message_version(
        self,
        conversation_id: str,
        message_id: str,
        direction: int,
    ) -> List[Dict[str, Any]]:
        if direction not in {-1, 1}:
            raise ValueError("Version direction must be -1 or 1.")
        with self._lock, self._connect() as connection:
            current = connection.execute(
                """
                SELECT * FROM messages
                WHERE id = ? AND conversation_id = ? AND role = 'user'
                """,
                (message_id, conversation_id),
            ).fetchone()
            if not current:
                raise ValueError("User message not found.")
            target = connection.execute(
                """
                SELECT * FROM messages
                WHERE conversation_id = ? AND revision_root_id = ?
                      AND role = 'user' AND revision_index = ?
                """,
                (
                    conversation_id,
                    current["revision_root_id"],
                    int(current["revision_index"]) + direction,
                ),
            ).fetchone()
            if not target:
                raise ValueError("No message version in that direction.")
            leaf = connection.execute(
                """
                WITH RECURSIVE branch AS (
                    SELECT id, created_at FROM messages WHERE id = ?
                    UNION ALL
                    SELECT child.id, child.created_at
                    FROM messages AS child
                    JOIN branch ON child.parent_message_id = branch.id
                )
                SELECT branch.id FROM branch
                WHERE NOT EXISTS (
                    SELECT 1 FROM messages AS child
                    WHERE child.parent_message_id = branch.id
                )
                ORDER BY branch.created_at DESC LIMIT 1
                """,
                (target["id"],),
            ).fetchone()
            connection.execute(
                """
                UPDATE conversations
                SET active_leaf_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (leaf["id"], utc_now(), conversation_id),
            )
        return self.list_messages(conversation_id)

    def serialized_history(self, conversation_id: str) -> Optional[bytes]:
        with self._lock, self._connect() as connection:
            conversation = connection.execute(
                "SELECT active_leaf_message_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conversation or not conversation["active_leaf_message_id"]:
                return None
            path_rows = connection.execute(
                """
                WITH RECURSIVE active_path AS (
                    SELECT id, parent_message_id
                    FROM messages WHERE id = ? AND conversation_id = ?
                    UNION ALL
                    SELECT parent.id, parent.parent_message_id
                    FROM messages AS parent
                    JOIN active_path ON active_path.parent_message_id = parent.id
                )
                SELECT id FROM active_path
                """,
                (conversation["active_leaf_message_id"], conversation_id),
            ).fetchall()
            message_ids = [str(row["id"]) for row in path_rows]
            placeholders = ",".join("?" for _ in message_ids)
            row = connection.execute(
                f"""
                SELECT serialized_json FROM messages
                WHERE id IN ({placeholders}) AND role = 'assistant'
                      AND serialized_json IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                message_ids,
            ).fetchone()
        return str(row["serialized_json"]).encode("utf-8") if row else None

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

    def resolve_approval(self, approval_id: str, resolution: bool | str) -> None:
        if isinstance(resolution, bool):
            status = "approved" if resolution else "denied"
        else:
            status = str(resolution)
        if status not in {"approved", "always_allowed", "denied", "expired"}:
            raise ValueError(f"Unsupported approval resolution: {status}")
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE approvals SET status = ?, resolved_at = ? WHERE id = ?",
                (status, utc_now(), approval_id),
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
                self._migrate_linear_message_history(destination)
            finally:
                source.close()
        return imported

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Dict[str, Any]:
        workflow = None
        if row["workflow_id"]:
            workflow = {
                "id": row["workflow_id"],
                "path": row["workflow_path"],
                "name": row["workflow_name"] or row["workflow_path"] or "Workflow",
            }
        return {
            "id": row["id"],
            "title": row["title"],
            "provider": row["provider"],
            "model": row["model"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "archivedAt": row["archived_at"],
            "workflow": workflow,
        }

    @staticmethod
    def _message(
        row: sqlite3.Row,
        *,
        revision_count: int = 1,
    ) -> Dict[str, Any]:
        revision_root_id = row["revision_root_id"] or row["id"]
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
            "parentMessageId": row["parent_message_id"],
            "revision": {
                "rootId": revision_root_id,
                "index": int(row["revision_index"] or 1),
                "count": max(1, int(revision_count)),
            },
        }


chat_store = ChatStore()
