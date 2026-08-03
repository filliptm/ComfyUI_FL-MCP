"""Bounded asynchronous HTTP fetching for Ren's web research tools."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from web_models import FetchedDocument
from web_security import DEFAULT_ALLOWED_PORTS, Resolver, validate_public_web_url

DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_CONTENT_TYPES = frozenset({"text/html", "text/plain", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class WebFetchError(RuntimeError):
    """Base class for bounded web fetch failures."""


class WebHttpStatusError(WebFetchError):
    """Raised for unsuccessful HTTP status codes."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"Web request returned HTTP {status_code} for {url}")


class TooManyRedirects(WebFetchError):
    """Raised when the configured redirect budget is exceeded."""


class ResponseTooLarge(WebFetchError):
    """Raised when a response exceeds the configured byte limit."""


class UnsupportedContentType(WebFetchError):
    """Raised when a fetch returns content that the local extractor cannot parse."""


class AsyncWebFetcher:
    """Reusable fetcher with redirect validation, streaming limits, and no proxy inheritance."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        max_concurrency: int = 6,
        timeout_seconds: float = 15.0,
        allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
    ) -> None:
        self._resolver = resolver
        self._allowed_ports = allowed_ports
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 8.0)),
            limits=httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=max_concurrency),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                "User-Agent": "ComfyUI-FL-MCP-Ren/0.6 (+https://github.com/filliptm/ComfyUI_FL-MCP)",
            },
        )

    async def __aenter__(self) -> AsyncWebFetcher:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        allowed_content_types: frozenset[str] = DEFAULT_CONTENT_TYPES,
    ) -> FetchedDocument:
        """Fetch one public text document while enforcing every resource budget."""

        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")

        requested_url = await validate_public_web_url(
            url,
            resolver=self._resolver,
            allowed_ports=self._allowed_ports,
        )
        current_url = requested_url
        redirect_chain: list[str] = []
        started = time.monotonic()

        async with self._semaphore:
            for redirect_count in range(max_redirects + 1):
                async with self._client.stream("GET", current_url) as response:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise WebHttpStatusError(response.status_code, current_url)
                        if redirect_count >= max_redirects:
                            raise TooManyRedirects(f"More than {max_redirects} redirects for {requested_url}")
                        next_url = urljoin(current_url, location)
                        current_url = await validate_public_web_url(
                            next_url,
                            resolver=self._resolver,
                            allowed_ports=self._allowed_ports,
                        )
                        redirect_chain.append(current_url)
                        continue

                    if response.status_code < 200 or response.status_code >= 300:
                        raise WebHttpStatusError(response.status_code, current_url)

                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type and content_type not in allowed_content_types:
                        raise UnsupportedContentType(
                            f"Content type {content_type!r} is not supported for {current_url}"
                        )

                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise ResponseTooLarge(f"Response exceeds {max_bytes} bytes")
                        except ValueError:
                            pass

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ResponseTooLarge(f"Response exceeds {max_bytes} bytes")
                        chunks.append(chunk)
                    content = b"".join(chunks)

                    if not content_type:
                        prefix = content[:512].lstrip().lower()
                        content_type = (
                            "text/html"
                            if prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
                            else "text/plain"
                        )
                    encoding = response.encoding or "utf-8"
                    try:
                        text = content.decode(encoding, errors="replace")
                    except LookupError:
                        text = content.decode("utf-8", errors="replace")
                    return FetchedDocument(
                        requested_url=requested_url,
                        final_url=current_url,
                        status_code=response.status_code,
                        content_type=content_type,
                        content=content,
                        text=text,
                        fetched_at=datetime.now(UTC),
                        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
                        redirect_chain=tuple(redirect_chain),
                    )

        raise WebFetchError(f"Fetch ended without a response for {requested_url}")
