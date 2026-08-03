"""Orchestration layer for safe fetching, extraction, and bounded caching."""

from __future__ import annotations

import asyncio
import hashlib

from web_cache import WebCache
from web_extract import extract_web_page
from web_fetcher import DEFAULT_MAX_BYTES, AsyncWebFetcher
from web_models import ExtractedWebPage
from web_security import canonicalize_web_url

PAGE_CACHE_VERSION = 1


def page_cache_key(url: str) -> str:
    """Build a stable versioned cache key without storing the URL in the index."""

    normalized = canonicalize_web_url(url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"page:v{PAGE_CACHE_VERSION}:{digest}"


class WebPageService:
    """Shared local-first page reader used by the future MCP search tools."""

    def __init__(
        self,
        *,
        fetcher: AsyncWebFetcher,
        cache: WebCache,
        page_ttl_seconds: float = 6 * 60 * 60,
    ) -> None:
        if page_ttl_seconds <= 0:
            raise ValueError("page_ttl_seconds must be positive")
        self._fetcher = fetcher
        self._cache = cache
        self._page_ttl_seconds = page_ttl_seconds

    async def fetch_page(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> ExtractedWebPage:
        """Return a locally extracted page, using the TTL cache when possible."""

        key = page_cache_key(url)
        if not force_refresh:
            cached = await asyncio.to_thread(self._cache.get, key)
            if cached is not None:
                return ExtractedWebPage.model_validate(cached).model_copy(update={"from_cache": True})

        document = await self._fetcher.fetch(url, max_bytes=max_bytes)
        page = extract_web_page(document)
        await asyncio.to_thread(
            self._cache.set,
            key,
            page.model_dump(mode="json"),
            ttl_seconds=self._page_ttl_seconds,
            kind="page",
        )
        return page
