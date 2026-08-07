import asyncio
from types import SimpleNamespace

import pytest
import server
from node_catalog_store import NodeCatalogStore


def _catalog(display_name: str = "Example") -> dict:
    return {
        "ExampleNode": {
            "display_name": display_name,
            "category": "testing",
            "description": "A test node",
            "python_module": "custom_nodes.example",
            "input": {"required": {}},
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
        }
    }


class PersistingFakeClient:
    def __init__(self, store: NodeCatalogStore, outcomes: list[object]):
        self.store = store
        self.outcomes = list(outcomes)
        self.fetch_count = 0

    async def fetch_node_library(self, *, force_refresh: bool = False):
        assert force_refresh is True
        self.fetch_count += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            self.store.record_refresh_failure(str(outcome))
            raise outcome
        self.store.reconcile(outcome, source="http://comfy/object_info")
        return outcome

    async def persisted_catalog_status(self):
        return self.store.status()


@pytest.mark.asyncio
async def test_startup_reconciliation_retries_until_comfy_catalog_is_ready(tmp_path):
    store = NodeCatalogStore(tmp_path / "catalog.sqlite3")
    client = PersistingFakeClient(
        store,
        [RuntimeError("starting"), RuntimeError("still starting"), _catalog()],
    )
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    try:
        reconciled = await server.reconcile_node_catalog_on_startup(
            client,
            max_attempts=4,
            initial_retry_delay=0.25,
            max_retry_delay=0.5,
            sleep=sleep,
        )

        assert reconciled is True
        assert client.fetch_count == 3
        assert delays == [0.25, 0.5]
        assert store.status()["state"] == "fresh"
        assert store.status()["node_count"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_exhausted_startup_retries_keep_last_good_catalog_stale(tmp_path):
    store = NodeCatalogStore(tmp_path / "catalog.sqlite3")
    store.reconcile(_catalog("Last good"), source="http://comfy/object_info")
    initial = store.get_snapshot()
    assert initial is not None
    client = PersistingFakeClient(
        store,
        [RuntimeError("offline one"), RuntimeError("offline two")],
    )

    async def no_wait(_delay: float) -> None:
        return None

    try:
        reconciled = await server.reconcile_node_catalog_on_startup(
            client,
            max_attempts=2,
            initial_retry_delay=0,
            max_retry_delay=0,
            sleep=no_wait,
        )

        stale = store.get_snapshot()
        assert reconciled is False
        assert stale is not None
        assert stale.state == "stale"
        assert stale.generation == initial.generation
        assert stale.catalog_hash == initial.catalog_hash
        assert stale.data == initial.data
        assert stale.last_error == "offline two"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_startup_retries_when_http_succeeds_but_persistence_stays_stale():
    class StalePersistenceClient:
        def __init__(self):
            self.fetch_count = 0

        async def fetch_node_library(self, *, force_refresh: bool = False):
            assert force_refresh is True
            self.fetch_count += 1
            return _catalog()

        async def persisted_catalog_status(self):
            return {"state": "stale", "last_error": "database busy"}

    client = StalePersistenceClient()

    async def no_wait(_delay: float) -> None:
        return None

    reconciled = await server.reconcile_node_catalog_on_startup(
        client,
        max_attempts=2,
        initial_retry_delay=0,
        max_retry_delay=0,
        sleep=no_wait,
    )

    assert reconciled is False
    assert client.fetch_count == 2


@pytest.mark.asyncio
async def test_catalog_lifespan_is_non_blocking_and_releases_owned_resources(
    monkeypatch,
    tmp_path,
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeStore:
        def __init__(self, path):
            self.path = path
            self.closed = False

        def close(self):
            self.closed = True

    class FakeClient:
        def __init__(self):
            self.bound = None
            self.unbound = []

        def bind_persistence(self, store):
            self.bound = store

        def unbind_persistence(self, store=None):
            self.unbound.append(store)
            if self.bound is store:
                self.bound = None
                return True
            return False

    client = FakeClient()

    async def blocked_reconciliation(_client):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "NodeCatalogStore", FakeStore)
    monkeypatch.setattr(server, "get_node_library_client", lambda **_kwargs: client)
    monkeypatch.setattr(
        server,
        "reconcile_node_catalog_on_startup",
        blocked_reconciliation,
    )
    app = SimpleNamespace(state=SimpleNamespace())

    async with server.node_catalog_persistence_lifespan(app):
        # Entering the lifespan does not wait for ComfyUI or catalog refresh.
        await asyncio.wait_for(started.wait(), timeout=1)
        store = app.state.node_catalog_store
        assert store.path == tmp_path / "node_catalog.sqlite3"
        assert client.bound is store
        assert store.closed is False
        assert app.state.node_catalog_reconciliation_task.done() is False

    assert cancelled.is_set()
    assert client.unbound == [store]
    assert client.bound is None
    assert store.closed is True
    assert app.state.node_catalog_store is None
    assert app.state.node_catalog_reconciliation_task is None
