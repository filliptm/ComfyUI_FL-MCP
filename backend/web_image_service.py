"""Safe, bounded web-image previews for Ren's chat gallery."""

from __future__ import annotations

import asyncio
import io
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from web_fetcher import AsyncWebFetcher
from web_security import canonicalize_web_url

WEB_IMAGE_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
WEB_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
WEB_IMAGE_ACCEPT = "image/webp,image/png,image/jpeg;q=0.9,*/*;q=0.1"
DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_DECODED_PIXELS = 32_000_000


class WebImagePreviewError(RuntimeError):
    """Raised when a remote resource cannot become a safe chat preview."""


@dataclass(frozen=True, slots=True)
class WebImagePreview:
    content: bytes
    media_type: str
    source_url: str
    original_size: tuple[int, int]
    preview_size: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    preview: WebImagePreview
    expires_at: float


def render_web_image_preview(
    content: bytes,
    *,
    source_url: str,
    max_dimension: int,
    max_decoded_pixels: int,
) -> WebImagePreview:
    """Decode an approved raster format and emit a bounded PNG/JPEG preview."""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(content)) as source:
                image_format = str(source.format or "").upper()
                if image_format not in WEB_IMAGE_FORMATS:
                    raise WebImagePreviewError(
                        "Only JPEG, PNG, and WebP web images can be previewed."
                    )
                original_size = source.size
                if original_size[0] * original_size[1] > max_decoded_pixels:
                    raise WebImagePreviewError(
                        f"Web image exceeds the {max_decoded_pixels:,}-pixel preview limit."
                    )
                source.seek(0)
                image = ImageOps.exif_transpose(source).copy()
    except WebImagePreviewError:
        raise
    except (OSError, UnidentifiedImageError, PILImage.DecompressionBombError) as exc:
        raise WebImagePreviewError("Remote content is not a readable web image.") from exc
    except PILImage.DecompressionBombWarning as exc:
        raise WebImagePreviewError("Remote image dimensions are too large to preview.") from exc

    image.thumbnail((max_dimension, max_dimension), PILImage.Resampling.LANCZOS)
    preview_size = image.size
    has_alpha = "A" in image.getbands() or (
        image.mode == "P" and "transparency" in image.info
    )
    buffer = io.BytesIO()
    if has_alpha:
        image.save(buffer, format="PNG", optimize=True)
        media_type = "image/png"
    else:
        image.convert("RGB").save(
            buffer,
            format="JPEG",
            quality=86,
            optimize=True,
        )
        media_type = "image/jpeg"
    return WebImagePreview(
        content=buffer.getvalue(),
        media_type=media_type,
        source_url=source_url,
        original_size=original_size,
        preview_size=preview_size,
    )


class WebImagePreviewService:
    """Fetch, validate, resize, and briefly cache public web-image previews."""

    def __init__(
        self,
        *,
        fetcher: AsyncWebFetcher | None = None,
        max_dimension: int = 1400,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        max_decoded_pixels: int = DEFAULT_MAX_DECODED_PIXELS,
        cache_ttl_seconds: float = 30 * 60,
        max_cache_entries: int = 48,
        max_cache_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        if max_dimension < 64 or max_image_bytes < 1 or max_decoded_pixels < 1:
            raise ValueError("Web image preview limits must be positive and usable.")
        if cache_ttl_seconds <= 0 or max_cache_entries < 1 or max_cache_bytes < 1:
            raise ValueError("Web image cache limits must be positive.")
        self._fetcher = fetcher or AsyncWebFetcher(max_concurrency=6)
        self._owns_fetcher = fetcher is None
        self._max_dimension = max_dimension
        self._max_image_bytes = max_image_bytes
        self._max_decoded_pixels = max_decoded_pixels
        self._cache_ttl_seconds = cache_ttl_seconds
        self._max_cache_entries = max_cache_entries
        self._max_cache_bytes = max_cache_bytes
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._cache_bytes = 0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_fetcher:
            await self._fetcher.aclose()

    async def preview(self, url: str) -> WebImagePreview:
        normalized = canonicalize_web_url(url)
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(normalized)
            if cached and cached.expires_at > now:
                self._cache.move_to_end(normalized)
                return cached.preview
            if cached:
                self._remove_cached(normalized)

        document = await self._fetcher.fetch(
            normalized,
            max_bytes=self._max_image_bytes,
            allowed_content_types=WEB_IMAGE_CONTENT_TYPES,
            accept_header=WEB_IMAGE_ACCEPT,
        )
        preview = await asyncio.to_thread(
            render_web_image_preview,
            document.content,
            source_url=document.final_url,
            max_dimension=self._max_dimension,
            max_decoded_pixels=self._max_decoded_pixels,
        )
        async with self._lock:
            self._store_cached(normalized, preview)
        return preview

    def _remove_cached(self, key: str) -> None:
        entry = self._cache.pop(key, None)
        if entry:
            self._cache_bytes -= len(entry.preview.content)

    def _store_cached(self, key: str, preview: WebImagePreview) -> None:
        self._remove_cached(key)
        self._cache[key] = _CacheEntry(
            preview=preview,
            expires_at=time.monotonic() + self._cache_ttl_seconds,
        )
        self._cache_bytes += len(preview.content)
        while (
            len(self._cache) > self._max_cache_entries
            or self._cache_bytes > self._max_cache_bytes
        ):
            _, entry = self._cache.popitem(last=False)
            self._cache_bytes -= len(entry.preview.content)
