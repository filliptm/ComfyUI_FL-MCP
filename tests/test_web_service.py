from datetime import UTC, datetime

import pytest
from web_cache import WebCache
from web_models import FetchedDocument
from web_service import WebPageService, page_cache_key


class FakeFetcher:
    def __init__(self):
        self.calls = []

    async def fetch(self, url, *, max_bytes):
        self.calls.append((url, max_bytes))
        html = """
        <html><head><title>Cached story</title></head><body><article>
        <p>This article has enough readable content for the local extraction service to cache.</p>
        <p>A second paragraph makes the result representative of an ordinary fetched page.</p>
        </article></body></html>
        """
        return FetchedDocument(
            requested_url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            content=html.encode(),
            text=html,
            fetched_at=datetime(2026, 8, 3, tzinfo=UTC),
            elapsed_ms=5,
        )


def test_page_cache_key_deduplicates_tracking_variants():
    assert page_cache_key("https://example.com/story?utm_source=a") == page_cache_key(
        "https://EXAMPLE.com:443/story"
    )


@pytest.mark.asyncio
async def test_page_service_combines_fetch_extract_and_cache(tmp_path):
    fetcher = FakeFetcher()
    cache = WebCache(tmp_path / "pages.sqlite3")
    service = WebPageService(fetcher=fetcher, cache=cache)
    try:
        first = await service.fetch_page("https://example.com/story")
        second = await service.fetch_page("https://example.com/story?utm_campaign=repeat")

        assert first.title == "Cached story"
        assert not first.from_cache
        assert second.from_cache
        assert second.content_hash == first.content_hash
        assert fetcher.calls == [("https://example.com/story", 4 * 1024 * 1024)]
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_page_service_force_refresh_bypasses_cache(tmp_path):
    fetcher = FakeFetcher()
    cache = WebCache(tmp_path / "pages.sqlite3")
    service = WebPageService(fetcher=fetcher, cache=cache)
    try:
        await service.fetch_page("https://example.com/story")
        refreshed = await service.fetch_page("https://example.com/story", force_refresh=True)

        assert not refreshed.from_cache
        assert len(fetcher.calls) == 2
    finally:
        cache.close()
