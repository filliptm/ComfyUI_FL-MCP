import sqlite3

from chat_store import ChatStore


def test_conversation_crud_and_serialized_history(tmp_path):
    store = ChatStore(tmp_path / "chat.db", tmp_path / "missing.db")
    conversation = store.create_conversation(provider="lmstudio", model="model")
    assert conversation["title"] == "New chat"

    store.append_message(conversation["id"], "user", "hello")
    store.append_message(
        conversation["id"],
        "assistant",
        "world",
        serialized=[{"kind": "response"}],
        metadata={"toolSteps": [{"name": "workflow_overview"}]},
    )

    messages = store.list_messages(conversation["id"])
    assert [item["content"] for item in messages] == ["hello", "world"]
    assert messages[-1]["metadata"]["toolSteps"][0]["name"] == "workflow_overview"
    assert store.serialized_history(conversation["id"]) is not None

    store.update_conversation(conversation["id"], title="Renamed")
    assert store.get_conversation(conversation["id"])["title"] == "Renamed"
    assert [item["id"] for item in store.list_conversations(view="active")] == [
        conversation["id"]
    ]
    assert store.list_conversations(view="archived") == []

    store.update_conversation(conversation["id"], archived=True)
    assert store.list_conversations(view="active") == []
    assert [item["id"] for item in store.list_conversations(view="archived")] == [
        conversation["id"]
    ]

    store.update_conversation(conversation["id"], archived=False)
    assert store.get_conversation(conversation["id"])["archivedAt"] is None
    assert store.delete_conversation(conversation["id"])
    assert store.get_conversation(conversation["id"]) is None


def test_conversation_list_rejects_unknown_view(tmp_path):
    store = ChatStore(tmp_path / "chat.db", tmp_path / "missing.db")

    try:
        store.list_conversations(view="deleted")
    except ValueError as exc:
        assert "active" in str(exc)
        assert "archived" in str(exc)
    else:
        raise AssertionError("Unknown conversation view should be rejected")


def test_message_edits_create_navigable_request_response_versions(tmp_path):
    store = ChatStore(tmp_path / "chat.db", tmp_path / "missing.db")
    conversation = store.create_conversation()
    first = store.append_message(conversation["id"], "user", "first request")
    store.append_message(conversation["id"], "assistant", "first response")
    store.append_message(conversation["id"], "user", "follow up")
    store.append_message(conversation["id"], "assistant", "follow-up response")

    revision = store.append_message(
        conversation["id"],
        "user",
        "edited request",
        parent_message_id=first["parentMessageId"],
        revision_root_id=first["revision"]["rootId"],
        revision_index=store.next_revision_index(
            conversation["id"],
            first["revision"]["rootId"],
        ),
        branch_from_active=False,
    )
    store.append_message(
        conversation["id"],
        "assistant",
        "edited response",
        parent_message_id=revision["id"],
        branch_from_active=False,
    )

    edited_branch = store.list_messages(conversation["id"])
    assert [item["content"] for item in edited_branch] == [
        "edited request",
        "edited response",
    ]
    assert edited_branch[0]["revision"] == {
        "rootId": first["id"],
        "index": 2,
        "count": 2,
    }

    original_branch = store.select_message_version(
        conversation["id"],
        revision["id"],
        -1,
    )
    assert [item["content"] for item in original_branch] == [
        "first request",
        "first response",
        "follow up",
        "follow-up response",
    ]
    assert original_branch[0]["revision"]["index"] == 1
    assert original_branch[0]["revision"]["count"] == 2

    restored_edit = store.select_message_version(
        conversation["id"],
        first["id"],
        1,
    )
    assert [item["content"] for item in restored_edit] == [
        "edited request",
        "edited response",
    ]


def test_existing_linear_history_migrates_to_active_branch(tmp_path):
    database = tmp_path / "chat.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, provider TEXT,
                model TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                archived_at TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
                provider TEXT, model TEXT, serialized_json TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
            );
            INSERT INTO conversations VALUES (
                'c1', 'Existing chat', 'ollama', 'qwen',
                '2026-01-01', '2026-01-01', NULL
            );
            INSERT INTO messages VALUES (
                'm1', 'c1', 'user', 'before upgrade', 'complete',
                NULL, NULL, NULL, '{}', '2026-01-01T00:00:00Z'
            );
            INSERT INTO messages VALUES (
                'm2', 'c1', 'assistant', 'still here', 'complete',
                NULL, NULL, NULL, '{}', '2026-01-01T00:00:01Z'
            );
            """
        )

    store = ChatStore(database, tmp_path / "missing.db")
    assert [item["content"] for item in store.list_messages("c1")] == [
        "before upgrade",
        "still here",
    ]
    added = store.append_message("c1", "user", "after upgrade")
    assert added["parentMessageId"] == "m2"
    assert [item["content"] for item in store.list_messages("c1")] == [
        "before upgrade",
        "still here",
        "after upgrade",
    ]


def test_legacy_import_is_idempotent_and_drops_sensitive_session_data(tmp_path):
    legacy = tmp_path / "ren.db"
    with sqlite3.connect(legacy) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, title TEXT, session_id TEXT, provider TEXT,
                model TEXT, claude_session_id TEXT, created_at TEXT,
                updated_at TEXT, archived_at TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, conversation_id TEXT, role TEXT, content TEXT,
                status TEXT, created_at TEXT, metadata_json TEXT
            );
            INSERT INTO conversations VALUES (
                'c1', 'Old chat', 'session', 'cloud', 'old-model',
                'secret-session', '2025-01-01', '2025-01-02', NULL
            );
            INSERT INTO messages VALUES (
                'm1', 'c1', 'user', 'legacy hello', 'complete',
                '2025-01-01', '{"token":"do-not-copy"}'
            );
            """
        )

    store = ChatStore(tmp_path / "chat.db", legacy)
    assert store.get_conversation("c1")["title"] == "Old chat"
    message = store.list_messages("c1")[0]
    assert message["metadata"] == {"source": "legacy_ren"}

    second = ChatStore(tmp_path / "chat.db", legacy)
    assert len(second.list_messages("c1")) == 1
