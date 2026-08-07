import concurrent.futures

import pytest
from node_catalog_store import NodeCatalogStore
from node_library import canonical_schema_hash, catalog_contract_hash, node_schema_hash


def node_info(
    *,
    display_name="Example",
    python_module="nodes",
    category="image",
    description="",
    input_type="IMAGE",
):
    return {
        "display_name": display_name,
        "python_module": python_module,
        "category": category,
        "description": description,
        "input": {"required": {"image": [input_type, {}]}},
        "output": ["IMAGE"],
        "output_name": ["IMAGE"],
    }


def test_reconcile_is_generation_pinned_and_tracks_changes(tmp_path):
    store = NodeCatalogStore(tmp_path / "catalog.sqlite3")
    first = {
        "LoadImage": node_info(display_name="Load Image"),
        "CustomBlur": node_info(
            display_name="Custom Blur",
            python_module="custom_nodes.blur",
            description="soften an image",
        ),
        "PartnerEdit": node_info(
            display_name="Partner Edit",
            python_module="comfy_api_nodes.edit",
            category="partner/image",
        ),
        "Mystery": node_info(python_module="vendor.mystery"),
    }
    try:
        result = store.reconcile(
            first,
            source="http://127.0.0.1:8188/object_info",
            catalog_hash=catalog_contract_hash(first),
            observed_catalog_hash=canonical_schema_hash(first),
            node_schema_hashes={name: node_schema_hash(name, info) for name, info in first.items()},
        )
        assert result.as_dict() | {} == {
            "generation": 1,
            "catalog_hash": catalog_contract_hash(first),
            "observed_catalog_hash": canonical_schema_hash(first),
            "node_count": 4,
            "new_count": 4,
            "changed_count": 0,
            "removed_count": 0,
            "unchanged_count": 0,
        }
        assert store.status()["origin_counts"] == {
            "native": 1,
            "custom": 1,
            "partner": 1,
            "unknown": 1,
        }

        second = {
            "LoadImage": first["LoadImage"],
            "CustomBlur": node_info(
                display_name="Custom Blur",
                python_module="custom_nodes.blur",
                description="soften an image",
                input_type="LATENT",
            ),
            "SaveImage": node_info(display_name="Save Image"),
        }
        result = store.reconcile(second, source="local/object_info")

        assert result.generation == 2
        assert (result.new_count, result.changed_count, result.removed_count) == (1, 1, 2)
        assert result.unchanged_count == 1
        assert store.get_node("PartnerEdit") is None
        removed = store.get_node("PartnerEdit", include_inactive=True)
        assert removed["active"] is False
        assert removed["removed_generation"] == 2
        assert set(store.get_snapshot().data) == {"LoadImage", "CustomBlur", "SaveImage"}
    finally:
        store.close()


def test_invalid_hash_or_oversized_json_cannot_replace_last_valid_snapshot(tmp_path):
    store = NodeCatalogStore(
        tmp_path / "catalog.sqlite3",
        max_node_json_bytes=400,
        max_catalog_json_bytes=800,
    )
    catalog = {"LoadImage": node_info(display_name="Load Image")}
    try:
        store.reconcile(catalog, source="object_info")
        before = store.get_snapshot()

        with pytest.raises(ValueError, match="catalog_hash"):
            store.reconcile(catalog, source="object_info", catalog_hash="0" * 64)
        with pytest.raises(ValueError, match="JSON limit"):
            store.reconcile(
                {"Huge": {**node_info(), "description": "x" * 1_000}},
                source="object_info",
            )

        after = store.get_snapshot()
        assert after.generation == before.generation
        assert after.catalog_hash == before.catalog_hash
        assert after.data == before.data
    finally:
        store.close()


def test_failed_refresh_serves_last_valid_snapshot_as_stale(tmp_path):
    now = [100.0]
    store = NodeCatalogStore(tmp_path / "catalog.sqlite3", clock=lambda: now[0])
    catalog = {"LoadImage": node_info(display_name="Load Image")}
    try:
        store.reconcile(catalog, source="object_info")
        store.record_refresh_failure("ComfyUI is restarting")

        stale = store.get_snapshot()
        assert stale.state == "stale"
        assert stale.data == catalog
        assert stale.last_error == "ComfyUI is restarting"
        assert store.get_snapshot(allow_stale=False) is None

        now[0] = 200.0
        store.reconcile(catalog, source="object_info")
        assert store.get_snapshot(max_age_seconds=1).state == "fresh"
        now[0] = 202.0
        assert store.get_snapshot(max_age_seconds=1).state == "stale"
    finally:
        store.close()


def test_search_is_deterministic_and_uses_fallback_without_fts(tmp_path):
    catalog = {
        "ZImageLoader": node_info(display_name="Z Image Loader"),
        "LoadImage": node_info(display_name="Load Image"),
        "ImageLoaderPlus": node_info(
            display_name="Image Loader Plus",
            python_module="custom_nodes.loader",
        ),
        "Unrelated": node_info(display_name="Text Encoder", category="conditioning"),
    }
    store = NodeCatalogStore(tmp_path / "fallback.sqlite3", prefer_fts=False)
    try:
        store.reconcile(catalog, source="object_info")
        assert [item["node_type"] for item in store.search("LoadImage")] == ["LoadImage"]
        first = store.search("image loader")
        second = store.search("image loader")
        assert [item["node_type"] for item in first] == [item["node_type"] for item in second]
        assert all(item["search_backend"] == "fallback" for item in first)

        store.reconcile({"Unrelated": catalog["Unrelated"]}, source="object_info")
        assert store.search("image loader") == []
    finally:
        store.close()


def test_fts_search_when_available(tmp_path):
    store = NodeCatalogStore(tmp_path / "fts.sqlite3")
    catalog = {
        "LoadImage": node_info(display_name="Load Image"),
        "StyleTransfer": {
            **node_info(
                display_name="Style Transfer",
                python_module="custom_nodes.style",
                description="apply a visual reference atmosphere",
            ),
            "search_aliases": ["reference image", "look transfer"],
        },
    }
    try:
        store.reconcile(catalog, source="object_info")
        if not store.fts_enabled:
            pytest.skip("SQLite was compiled without FTS5")
        assert store.search("visual reference")[0]["node_type"] == "StyleTransfer"
        assert store.search("visual reference")[0]["search_backend"] == "fts5"
    finally:
        store.close()


def test_verified_lessons_only_apply_to_the_active_exact_schema(tmp_path):
    store = NodeCatalogStore(tmp_path / "catalog.sqlite3")
    first = {"CustomBlur": node_info(python_module="custom_nodes.blur")}
    try:
        store.reconcile(first, source="object_info")
        first_hash = node_schema_hash("CustomBlur", first["CustomBlur"])
        store.record_verified_lesson(
            "CustomBlur",
            first_hash,
            "safe-radius",
            {"widget": "radius", "maximum": 32},
        )
        assert store.get_verified_lessons("CustomBlur")[0]["payload"]["maximum"] == 32

        second = {
            "CustomBlur": node_info(
                python_module="custom_nodes.blur",
                input_type="LATENT",
            )
        }
        store.reconcile(second, source="object_info")
        assert store.get_verified_lessons("CustomBlur") == []

        with pytest.raises(ValueError, match="does not match"):
            store.record_verified_lesson("CustomBlur", first_hash, "old", {})
    finally:
        store.close()


def test_lesson_payload_is_bounded(tmp_path):
    store = NodeCatalogStore(tmp_path / "catalog.sqlite3", max_lesson_json_bytes=64)
    catalog = {"LoadImage": node_info()}
    try:
        store.reconcile(catalog, source="object_info")
        schema_hash = node_schema_hash("LoadImage", catalog["LoadImage"])
        with pytest.raises(ValueError, match="JSON limit"):
            store.record_verified_lesson(
                "LoadImage",
                schema_hash,
                "oversized",
                {"value": "x" * 100},
            )
    finally:
        store.close()


def test_store_is_safe_for_concurrent_readers_and_writers(tmp_path):
    store = NodeCatalogStore(tmp_path / "catalog.sqlite3", prefer_fts=False)
    catalog = {
        "LoadImage": node_info(display_name="Load Image"),
        "SaveImage": node_info(display_name="Save Image"),
    }
    store.reconcile(catalog, source="object_info")

    def read_many():
        for _ in range(40):
            snapshot = store.get_snapshot()
            assert snapshot.catalog_hash == catalog_contract_hash(snapshot.data)
            store.search("image")

    def write_many():
        for index in range(10):
            updated = dict(catalog)
            if index % 2:
                updated["PreviewImage"] = node_info(display_name="Preview Image")
            store.reconcile(updated, source="object_info")

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(read_many) for _ in range(4)]
            futures.append(executor.submit(write_many))
            for future in futures:
                future.result()
        assert store.get_snapshot().generation == 11
    finally:
        store.close()
