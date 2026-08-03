import json
import zlib

import pytest
from web_cache import WebCache


def compressed_size(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return len(zlib.compress(encoded, level=6))


def test_cache_round_trip_and_expiry(tmp_path):
    now = [100.0]
    cache = WebCache(tmp_path / "web-cache.sqlite3", clock=lambda: now[0])
    try:
        assert cache.set("page:1", {"title": "Hello", "items": [1, 2]}, ttl_seconds=10)
        assert cache.get("page:1") == {"title": "Hello", "items": [1, 2]}

        now[0] = 111.0
        assert cache.get("page:1") is None
    finally:
        cache.close()


def test_cache_rejects_item_larger_than_total_budget():
    value = {"payload": "not very large"}
    cache = WebCache(":memory:", max_bytes=compressed_size(value) - 1)
    try:
        assert not cache.set("oversized", value, ttl_seconds=30)
        assert cache.get("oversized") is None
    finally:
        cache.close()


def test_cache_prunes_least_recently_used_entries(tmp_path):
    first = {"payload": "0123456789abcdef" * 12}
    second = {"payload": "fedcba9876543210" * 12}
    budget = compressed_size(first) + compressed_size(second) - 1
    now = [1.0]
    cache = WebCache(tmp_path / "bounded.sqlite3", max_bytes=budget, clock=lambda: now[0])
    try:
        assert cache.set("first", first, ttl_seconds=60)
        now[0] = 2.0
        assert cache.set("second", second, ttl_seconds=60)
        assert cache.get("first") is None
        assert cache.get("second") == second
    finally:
        cache.close()


def test_cache_requires_positive_limits(tmp_path):
    with pytest.raises(ValueError):
        WebCache(tmp_path / "bad.sqlite3", max_bytes=0)

    cache = WebCache(":memory:")
    try:
        with pytest.raises(ValueError):
            cache.set("bad", {}, ttl_seconds=0)
    finally:
        cache.close()
