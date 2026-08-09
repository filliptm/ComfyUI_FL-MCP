"""Lightweight persistent knowledge for the locally loaded ComfyUI node catalog.

The store deliberately treats ``/object_info`` as the source of truth.  A complete
catalog is reconciled in one SQLite transaction, so readers observe either the
previous valid generation or the new one, never a mixture of both.  Removed node
classes remain available as inactive records for diagnostics, while search only
returns classes loaded in the current generation.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from node_library import (
    canonical_schema_hash,
    catalog_contract_hash,
    classify_node_origin,
    node_schema_hash,
)

NodeOrigin = Literal["native", "custom", "partner", "unknown"]
CatalogState = Literal["fresh", "stale"]

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WORD_PATTERN = re.compile(r"[\w]+", re.UNICODE)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class CatalogReconciliation:
    """Summary of one successfully committed catalog generation."""

    generation: int
    catalog_hash: str
    observed_catalog_hash: str
    node_count: int
    new_count: int
    changed_count: int
    removed_count: int
    unchanged_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StoredCatalogSnapshot:
    """One last-valid catalog generation and its freshness state."""

    generation: int
    data: dict[str, Any]
    source: str
    catalog_hash: str
    observed_catalog_hash: str
    fetched_at: float
    state: CatalogState
    last_error: str | None
    origin_counts: dict[str, int]


class NodeCatalogStore:
    """Thread-safe SQLite catalog, search index, and schema-scoped lessons."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        max_catalog_json_bytes: int = 64 * 1024 * 1024,
        max_node_json_bytes: int = 2 * 1024 * 1024,
        max_lesson_json_bytes: int = 256 * 1024,
        max_lessons: int = 4096,
        prefer_fts: bool = True,
    ) -> None:
        if (
            min(
                max_catalog_json_bytes,
                max_node_json_bytes,
                max_lesson_json_bytes,
                max_lessons,
            )
            < 1
        ):
            raise ValueError("catalog store limits must be positive")

        self._path = str(path)
        self._clock = clock
        self._max_catalog_json_bytes = max_catalog_json_bytes
        self._max_node_json_bytes = max_node_json_bytes
        self._max_lesson_json_bytes = max_lesson_json_bytes
        self._max_lessons = max_lessons
        self._lock = threading.RLock()
        self._closed = False

        if self._path != ":memory:":
            Path(self._path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        if self._path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
        self._init_schema(prefer_fts=prefer_fts)

    def __enter__(self) -> NodeCatalogStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def fts_enabled(self) -> bool:
        """Whether this Python/SQLite build supports the FTS5 search path."""

        return self._fts_enabled

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def _init_schema(self, *, prefer_fts: bool) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    generation INTEGER NOT NULL DEFAULT 0,
                    catalog_hash TEXT,
                    observed_catalog_hash TEXT,
                    source TEXT,
                    fetched_at REAL,
                    node_count INTEGER NOT NULL DEFAULT 0,
                    last_refresh_attempt_at REAL,
                    last_refresh_error TEXT
                );
                INSERT OR IGNORE INTO catalog_state(singleton) VALUES (1);

                CREATE TABLE IF NOT EXISTS catalog_nodes (
                    node_type TEXT PRIMARY KEY,
                    schema_json TEXT NOT NULL,
                    schema_hash TEXT NOT NULL,
                    origin TEXT NOT NULL CHECK (
                        origin IN ('native', 'custom', 'partner', 'unknown')
                    ),
                    display_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    python_module TEXT NOT NULL,
                    aliases TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    first_seen_generation INTEGER NOT NULL,
                    last_seen_generation INTEGER NOT NULL,
                    removed_generation INTEGER
                );
                CREATE INDEX IF NOT EXISTS catalog_nodes_active
                    ON catalog_nodes(active, node_type);
                CREATE INDEX IF NOT EXISTS catalog_nodes_schema
                    ON catalog_nodes(node_type, schema_hash, active);

                CREATE TABLE IF NOT EXISTS verified_node_lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_type TEXT NOT NULL,
                    schema_hash TEXT NOT NULL,
                    lesson_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    verified_at REAL NOT NULL,
                    UNIQUE(node_type, schema_hash, lesson_key)
                );
                CREATE INDEX IF NOT EXISTS verified_node_lessons_scope
                    ON verified_node_lessons(node_type, schema_hash, lesson_key);
                """
            )

            self._fts_enabled = False
            if prefer_fts:
                try:
                    self._connection.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS node_catalog_fts USING fts5(
                            node_type UNINDEXED,
                            searchable,
                            tokenize='unicode61 remove_diacritics 2'
                        )
                        """
                    )
                    self._fts_enabled = True
                except sqlite3.OperationalError as exc:
                    # Python distributions may compile SQLite without FTS5.  The
                    # deterministic LIKE/scoring fallback needs no optional module.
                    self._connection.rollback()
                    if "fts5" not in str(exc).casefold():
                        raise

    @staticmethod
    def _require_hash(value: str, label: str) -> str:
        if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
        return value

    @staticmethod
    def _bounded_text(value: Any, max_chars: int = 16_384) -> str:
        return str(value or "")[:max_chars]

    @staticmethod
    def _aliases(node_info: Mapping[str, Any]) -> str:
        value = node_info.get("search_aliases", [])
        if isinstance(value, str):
            aliases = [value]
        elif isinstance(value, (list, tuple)):
            aliases = [str(item) for item in value]
        else:
            aliases = []
        return " ".join(aliases)[:16_384]

    @staticmethod
    def _searchable_text(
        node_type: str,
        display_name: str,
        category: str,
        description: str,
        python_module: str,
        aliases: str,
    ) -> str:
        split_node_type = _CAMEL_BOUNDARY.sub(" ", node_type.replace("_", " "))
        return " ".join(
            (
                node_type,
                split_node_type,
                display_name,
                category,
                description,
                python_module,
                aliases,
            )
        )

    def _encode_json(self, value: Any, *, max_bytes: int, label: str) -> str:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be JSON-compatible: {exc}") from exc
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError(f"{label} exceeds the {max_bytes}-byte JSON limit")
        return encoded

    def reconcile(
        self,
        catalog: Mapping[str, Any],
        *,
        source: str,
        catalog_hash: str | None = None,
        observed_catalog_hash: str | None = None,
        node_schema_hashes: Mapping[str, str] | None = None,
    ) -> CatalogReconciliation:
        """Atomically replace the active generation with a valid full catalog.

        Callers may supply identities already pinned by ``NodeLibraryCache``.  They
        are verified before any SQL is written, preventing a mismatched catalog and
        hash from becoming the last-valid snapshot.
        """

        if not isinstance(catalog, Mapping):
            raise TypeError("catalog must be an /object_info-like mapping")
        source = self._bounded_text(source, 4096)
        if not source:
            raise ValueError("source must not be empty")

        prepared: list[dict[str, Any]] = []
        total_bytes = 0
        raw_catalog: dict[str, Any] = {}
        for node_type in sorted(catalog):
            if not isinstance(node_type, str) or not node_type or len(node_type) > 512:
                raise ValueError("catalog node types must be non-empty strings up to 512 chars")
            node_info = catalog[node_type]
            if not isinstance(node_info, dict):
                raise ValueError(f"catalog entry {node_type!r} must be an object")
            schema_json = self._encode_json(
                node_info,
                max_bytes=self._max_node_json_bytes,
                label=f"schema for {node_type}",
            )
            schema_bytes = len(schema_json.encode("utf-8"))
            total_bytes += schema_bytes + len(node_type.encode("utf-8"))
            if total_bytes > self._max_catalog_json_bytes:
                raise ValueError(
                    f"catalog exceeds the {self._max_catalog_json_bytes}-byte JSON limit"
                )

            computed_schema_hash = node_schema_hash(node_type, node_info)
            supplied_schema_hash = (
                node_schema_hashes.get(node_type) if node_schema_hashes is not None else None
            )
            if supplied_schema_hash is not None:
                self._require_hash(supplied_schema_hash, f"schema hash for {node_type}")
                if supplied_schema_hash != computed_schema_hash:
                    raise ValueError(f"schema hash mismatch for {node_type}")
            schema_hash = supplied_schema_hash or computed_schema_hash

            display_name = self._bounded_text(node_info.get("display_name") or node_type)
            category = self._bounded_text(node_info.get("category"))
            description = self._bounded_text(node_info.get("description"))
            python_module = self._bounded_text(node_info.get("python_module"))
            aliases = self._aliases(node_info)
            prepared.append(
                {
                    "node_type": node_type,
                    "schema_json": schema_json,
                    "schema_hash": schema_hash,
                    "origin": classify_node_origin(node_info),
                    "display_name": display_name,
                    "category": category,
                    "description": description,
                    "python_module": python_module,
                    "aliases": aliases,
                    "searchable": self._searchable_text(
                        node_type,
                        display_name,
                        category,
                        description,
                        python_module,
                        aliases,
                    ),
                }
            )
            raw_catalog[node_type] = node_info

        computed_catalog_hash = catalog_contract_hash(raw_catalog)
        if catalog_hash is not None:
            self._require_hash(catalog_hash, "catalog_hash")
            if catalog_hash != computed_catalog_hash:
                raise ValueError("catalog_hash does not match the supplied catalog")
        catalog_hash = catalog_hash or computed_catalog_hash

        computed_observed_hash = canonical_schema_hash(raw_catalog)
        if observed_catalog_hash is not None:
            self._require_hash(observed_catalog_hash, "observed_catalog_hash")
            if observed_catalog_hash != computed_observed_hash:
                raise ValueError("observed_catalog_hash does not match the supplied catalog")
        observed_catalog_hash = observed_catalog_hash or computed_observed_hash
        now = self._clock()

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                state = self._connection.execute(
                    "SELECT generation FROM catalog_state WHERE singleton = 1"
                ).fetchone()
                generation = int(state["generation"]) + 1
                previous = {
                    row["node_type"]: row
                    for row in self._connection.execute(
                        "SELECT node_type, schema_hash FROM catalog_nodes WHERE active = 1"
                    )
                }

                new_count = 0
                changed_count = 0
                unchanged_count = 0
                incoming = {item["node_type"] for item in prepared}
                for item in prepared:
                    prior = previous.get(item["node_type"])
                    if prior is None:
                        new_count += 1
                    elif prior["schema_hash"] != item["schema_hash"]:
                        changed_count += 1
                    else:
                        unchanged_count += 1

                    self._connection.execute(
                        """
                        INSERT INTO catalog_nodes (
                            node_type, schema_json, schema_hash, origin, display_name,
                            category, description, python_module, aliases, active,
                            first_seen_generation, last_seen_generation, removed_generation
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                        ON CONFLICT(node_type) DO UPDATE SET
                            schema_json = excluded.schema_json,
                            schema_hash = excluded.schema_hash,
                            origin = excluded.origin,
                            display_name = excluded.display_name,
                            category = excluded.category,
                            description = excluded.description,
                            python_module = excluded.python_module,
                            aliases = excluded.aliases,
                            active = 1,
                            last_seen_generation = excluded.last_seen_generation,
                            removed_generation = NULL
                        """,
                        (
                            item["node_type"],
                            item["schema_json"],
                            item["schema_hash"],
                            item["origin"],
                            item["display_name"],
                            item["category"],
                            item["description"],
                            item["python_module"],
                            item["aliases"],
                            generation,
                            generation,
                        ),
                    )

                removed = set(previous) - incoming
                self._connection.execute(
                    """
                    UPDATE catalog_nodes
                    SET active = 0, removed_generation = ?
                    WHERE active = 1 AND last_seen_generation <> ?
                    """,
                    (generation, generation),
                )

                if self._fts_enabled:
                    self._connection.execute("DELETE FROM node_catalog_fts")
                    self._connection.executemany(
                        "INSERT INTO node_catalog_fts(node_type, searchable) VALUES (?, ?)",
                        [(item["node_type"], item["searchable"]) for item in prepared],
                    )

                self._connection.execute(
                    """
                    UPDATE catalog_state SET
                        generation = ?, catalog_hash = ?, observed_catalog_hash = ?,
                        source = ?, fetched_at = ?, node_count = ?,
                        last_refresh_attempt_at = ?, last_refresh_error = NULL
                    WHERE singleton = 1
                    """,
                    (
                        generation,
                        catalog_hash,
                        observed_catalog_hash,
                        source,
                        now,
                        len(prepared),
                        now,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

        return CatalogReconciliation(
            generation=generation,
            catalog_hash=catalog_hash,
            observed_catalog_hash=observed_catalog_hash,
            node_count=len(prepared),
            new_count=new_count,
            changed_count=changed_count,
            removed_count=len(removed),
            unchanged_count=unchanged_count,
        )

    def record_refresh_failure(self, error: str) -> None:
        """Mark refresh as failed without replacing the last-valid generation."""

        error = self._bounded_text(error, 4096) or "unknown catalog refresh error"
        with self._lock:
            self._connection.execute(
                """
                UPDATE catalog_state
                SET last_refresh_attempt_at = ?, last_refresh_error = ?
                WHERE singleton = 1
                """,
                (self._clock(), error),
            )

    def _state_and_rows(self) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        state = self._connection.execute(
            "SELECT * FROM catalog_state WHERE singleton = 1"
        ).fetchone()
        rows = list(
            self._connection.execute(
                """
                SELECT node_type, schema_json, schema_hash, origin, active,
                       first_seen_generation, last_seen_generation, removed_generation,
                       display_name, category, description, python_module, aliases
                FROM catalog_nodes
                WHERE active = 1
                ORDER BY node_type COLLATE NOCASE, node_type
                """
            )
        )
        return state, rows

    def get_snapshot(
        self,
        *,
        max_age_seconds: float | None = None,
        allow_stale: bool = True,
    ) -> StoredCatalogSnapshot | None:
        """Read the last valid snapshot, optionally rejecting stale data."""

        if max_age_seconds is not None and max_age_seconds < 0:
            raise ValueError("max_age_seconds must not be negative")
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                state, rows = self._state_and_rows()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            if int(state["generation"]) == 0:
                return None
            is_stale = state["last_refresh_error"] is not None
            if max_age_seconds is not None:
                is_stale = is_stale or self._clock() - float(state["fetched_at"]) > max_age_seconds
            if is_stale and not allow_stale:
                return None

            data = {row["node_type"]: json.loads(row["schema_json"]) for row in rows}
            origin_counts = {"native": 0, "custom": 0, "partner": 0, "unknown": 0}
            for row in rows:
                origin_counts[row["origin"]] += 1
            return StoredCatalogSnapshot(
                generation=int(state["generation"]),
                data=data,
                source=str(state["source"]),
                catalog_hash=str(state["catalog_hash"]),
                observed_catalog_hash=str(state["observed_catalog_hash"]),
                fetched_at=float(state["fetched_at"]),
                state="stale" if is_stale else "fresh",
                last_error=state["last_refresh_error"],
                origin_counts=origin_counts,
            )

    def status(self, *, max_age_seconds: float | None = None) -> dict[str, Any]:
        """Return compact persisted status without discarding a stale snapshot."""

        snapshot = self.get_snapshot(max_age_seconds=max_age_seconds)
        if snapshot is None:
            return {
                "state": "empty",
                "generation": 0,
                "node_count": 0,
                "origin_counts": {
                    "native": 0,
                    "custom": 0,
                    "partner": 0,
                    "unknown": 0,
                },
                "catalog_hash": None,
                "observed_catalog_hash": None,
                "source": None,
                "fetched_at": None,
                "last_error": None,
                "fts_enabled": self._fts_enabled,
            }
        return {
            "state": snapshot.state,
            "generation": snapshot.generation,
            "node_count": len(snapshot.data),
            "origin_counts": snapshot.origin_counts,
            "catalog_hash": snapshot.catalog_hash,
            "observed_catalog_hash": snapshot.observed_catalog_hash,
            "source": snapshot.source,
            "fetched_at": snapshot.fetched_at,
            "last_error": snapshot.last_error,
            "fts_enabled": self._fts_enabled,
        }

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "node_type": row["node_type"],
            "schema": json.loads(row["schema_json"]),
            "schema_hash": row["schema_hash"],
            "origin": row["origin"],
            "active": bool(row["active"]),
            "display_name": row["display_name"],
            "category": row["category"],
            "description": row["description"],
            "python_module": row["python_module"],
            "first_seen_generation": int(row["first_seen_generation"]),
            "last_seen_generation": int(row["last_seen_generation"]),
            "removed_generation": row["removed_generation"],
        }

    def get_node(self, node_type: str, *, include_inactive: bool = False) -> dict[str, Any] | None:
        with self._lock:
            query = "SELECT * FROM catalog_nodes WHERE node_type = ?"
            parameters: tuple[Any, ...] = (node_type,)
            if not include_inactive:
                query += " AND active = 1"
            row = self._connection.execute(query, parameters).fetchone()
            return self._row_to_node(row) if row is not None else None

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        return [term.casefold() for term in _WORD_PATTERN.findall(query) if term]

    @staticmethod
    def _fallback_score(row: sqlite3.Row, query: str, terms: list[str]) -> int:
        node_type = str(row["node_type"]).casefold()
        display_name = str(row["display_name"]).casefold()
        fields = " ".join(
            str(row[field]).casefold()
            for field in (
                "node_type",
                "display_name",
                "category",
                "description",
                "python_module",
                "aliases",
            )
        )
        folded = query.casefold().strip()
        if node_type == folded:
            return 1000
        if display_name == folded:
            return 950
        if node_type.startswith(folded) or display_name.startswith(folded):
            return 800
        if terms and all(term in fields for term in terms):
            return 600 + sum(20 for term in terms if term in node_type or term in display_name)
        return 0

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Search active nodes deterministically via FTS5 or a SQL/Python fallback."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        terms = self._query_terms(query)
        with self._lock:
            if self._fts_enabled and terms:
                match = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
                rows = list(
                    self._connection.execute(
                        """
                        SELECT nodes.*, bm25(node_catalog_fts) AS fts_rank
                        FROM node_catalog_fts
                        JOIN catalog_nodes AS nodes
                          ON nodes.node_type = node_catalog_fts.node_type
                        WHERE node_catalog_fts MATCH ? AND nodes.active = 1
                        ORDER BY
                            CASE WHEN lower(nodes.node_type) = lower(?) THEN 0
                                 WHEN lower(nodes.display_name) = lower(?) THEN 1
                                 ELSE 2 END,
                            fts_rank,
                            nodes.node_type COLLATE NOCASE,
                            nodes.node_type
                        LIMIT ?
                        """,
                        (match, query.strip(), query.strip(), limit),
                    )
                )
                results = []
                for row in rows:
                    result = self._row_to_node(row)
                    result["search_backend"] = "fts5"
                    results.append(result)
                return results

            rows = list(self._connection.execute("SELECT * FROM catalog_nodes WHERE active = 1"))
            scored = [(self._fallback_score(row, query, terms), row) for row in rows]
            scored = [item for item in scored if item[0] > 0]
            scored.sort(
                key=lambda item: (
                    -item[0],
                    str(item[1]["node_type"]).casefold(),
                    str(item[1]["node_type"]),
                )
            )
            results = []
            for score, row in scored[:limit]:
                result = self._row_to_node(row)
                result.update({"score": score, "search_backend": "fallback"})
                results.append(result)
            return results

    def record_verified_lesson(
        self,
        node_type: str,
        schema_hash: str,
        lesson_key: str,
        payload: Any,
    ) -> None:
        """Persist a verified lesson only for the node's active exact schema."""

        self._require_hash(schema_hash, "schema_hash")
        if not lesson_key or len(lesson_key) > 512:
            raise ValueError("lesson_key must be between 1 and 512 characters")
        payload_json = self._encode_json(
            payload,
            max_bytes=self._max_lesson_json_bytes,
            label="lesson payload",
        )
        with self._lock:
            current = self._connection.execute(
                """
                SELECT schema_hash FROM catalog_nodes
                WHERE node_type = ? AND active = 1
                """,
                (node_type,),
            ).fetchone()
            if current is None:
                raise ValueError(f"node {node_type!r} is not active")
            if current["schema_hash"] != schema_hash:
                raise ValueError("lesson schema_hash does not match the active node schema")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO verified_node_lessons (
                        node_type, schema_hash, lesson_key, payload_json, verified_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(node_type, schema_hash, lesson_key) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        verified_at = excluded.verified_at
                    """,
                    (node_type, schema_hash, lesson_key, payload_json, self._clock()),
                )
                self._connection.execute(
                    """
                    DELETE FROM verified_node_lessons
                    WHERE id IN (
                        SELECT id FROM verified_node_lessons
                        ORDER BY verified_at DESC, id DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (self._max_lessons,),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get_verified_lessons(self, node_type: str) -> list[dict[str, Any]]:
        """Return lessons for the active schema; old-schema lessons never apply."""

        with self._lock:
            rows = list(
                self._connection.execute(
                    """
                    SELECT lessons.lesson_key, lessons.payload_json,
                           lessons.schema_hash, lessons.verified_at
                    FROM verified_node_lessons AS lessons
                    JOIN catalog_nodes AS nodes
                      ON nodes.node_type = lessons.node_type
                     AND nodes.schema_hash = lessons.schema_hash
                     AND nodes.active = 1
                    WHERE lessons.node_type = ?
                    ORDER BY lessons.lesson_key COLLATE NOCASE, lessons.lesson_key
                    """,
                    (node_type,),
                )
            )
            return [
                {
                    "lesson_key": row["lesson_key"],
                    "payload": json.loads(row["payload_json"]),
                    "schema_hash": row["schema_hash"],
                    "verified_at": float(row["verified_at"]),
                }
                for row in rows
            ]

    def get_all_active_verified_lessons(self) -> list[dict[str, Any]]:
        """Return every lesson whose exact node schema is still active.

        The semantic workflow compiler uses this bounded collection only as a
        ranking prior. Both endpoints recorded in a connection lesson must
        still have their exact schemas loaded. The live catalog and GraphPatch
        validation remain the authority for every node, value, and connection.
        """

        with self._lock:
            active_hashes = {
                row["node_type"]: row["schema_hash"]
                for row in self._connection.execute(
                    """
                    SELECT node_type, schema_hash
                    FROM catalog_nodes
                    WHERE active = 1
                    """
                )
            }
            rows = list(
                self._connection.execute(
                    """
                    SELECT lessons.node_type, lessons.lesson_key,
                           lessons.payload_json, lessons.schema_hash,
                           lessons.verified_at
                    FROM verified_node_lessons AS lessons
                    JOIN catalog_nodes AS nodes
                      ON nodes.node_type = lessons.node_type
                     AND nodes.schema_hash = lessons.schema_hash
                     AND nodes.active = 1
                    ORDER BY lessons.node_type COLLATE NOCASE,
                             lessons.node_type,
                             lessons.lesson_key COLLATE NOCASE,
                             lessons.lesson_key
                    """
                )
            )
            result: list[dict[str, Any]] = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                source_type = payload.get("source_node_type")
                source_hash = payload.get("source_schema_hash")
                target_type = payload.get("target_node_type")
                target_hash = payload.get("target_schema_hash")
                if (
                    not isinstance(source_type, str)
                    or not isinstance(target_type, str)
                    or not isinstance(source_hash, str)
                    or not isinstance(target_hash, str)
                    or _HASH_PATTERN.fullmatch(source_hash) is None
                    or _HASH_PATTERN.fullmatch(target_hash) is None
                    or active_hashes.get(source_type) != source_hash
                    or active_hashes.get(target_type) != target_hash
                    or row["node_type"] not in {source_type, target_type}
                ):
                    continue
                result.append(
                    {
                        "node_type": row["node_type"],
                        "lesson_key": row["lesson_key"],
                        "payload": payload,
                        "schema_hash": row["schema_hash"],
                        "verified_at": float(row["verified_at"]),
                    }
                )
            return result
