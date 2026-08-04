import io

import httpx
import pytest
from PIL import Image
from web_fetcher import AsyncWebFetcher, UnsupportedContentType
from web_image_service import WebImagePreviewError, WebImagePreviewService


async def public_resolver(_hostname, _port):
    return ["93.184.216.34"]


def image_bytes(size=(1200, 600), mode="RGB", image_format="PNG"):
    buffer = io.BytesIO()
    color = (60, 110, 210, 180) if mode == "RGBA" else (60, 110, 210)
    Image.new(mode, size, color).save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_web_image_preview_is_bounded_and_cached():
    requests = 0
    payload = image_bytes()

    def handler(request):
        nonlocal requests
        assert request.headers["accept"].startswith("image/webp,image/png,image/jpeg")
        requests += 1
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=payload,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        service = WebImagePreviewService(fetcher=fetcher, max_dimension=600)
        first = await service.preview("https://example.com/reference.png")
        second = await service.preview("https://example.com/reference.png")

    assert requests == 1
    assert first is second
    assert first.media_type == "image/jpeg"
    assert first.original_size == (1200, 600)
    assert first.preview_size == (600, 300)
    with Image.open(io.BytesIO(first.content)) as rendered:
        assert rendered.size == (600, 300)


@pytest.mark.asyncio
async def test_web_image_preview_preserves_transparency_as_png():
    payload = image_bytes(size=(200, 100), mode="RGBA")

    def handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=payload,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        service = WebImagePreviewService(fetcher=fetcher)
        preview = await service.preview("https://example.com/alpha.png")

    assert preview.media_type == "image/png"
    with Image.open(io.BytesIO(preview.content)) as rendered:
        assert "A" in rendered.getbands()


@pytest.mark.asyncio
async def test_web_image_preview_rejects_unapproved_types_and_bad_pixels():
    def gif_handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "image/gif"},
            content=b"GIF89a",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gif_handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        service = WebImagePreviewService(fetcher=fetcher)
        with pytest.raises(UnsupportedContentType):
            await service.preview("https://example.com/animated.gif")

    payload = image_bytes(size=(20, 20))

    def large_handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=payload,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(large_handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        service = WebImagePreviewService(fetcher=fetcher, max_decoded_pixels=100)
        with pytest.raises(WebImagePreviewError, match="pixel preview limit"):
            await service.preview("https://example.com/too-large.png")


@pytest.mark.asyncio
async def test_web_image_preview_rejects_mislabeled_content():
    def handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"not an image",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        service = WebImagePreviewService(fetcher=fetcher)
        with pytest.raises(WebImagePreviewError, match="not a readable"):
            await service.preview("https://example.com/fake.png")
