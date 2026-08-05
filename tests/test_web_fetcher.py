import httpx
import pytest
from web_fetcher import (
    AsyncWebFetcher,
    ResponseTooLarge,
    TooManyRedirects,
    UnsupportedContentType,
    WebHttpStatusError,
)
from web_security import UnsafeWebUrl


async def public_resolver(_hostname, _port):
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_fetch_streams_supported_content_and_reports_redirect_chain():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/article"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><body>Hello</body></html>",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        result = await fetcher.fetch("https://example.com/start")

    assert result.requested_url == "https://example.com/start"
    assert result.final_url == "https://example.com/article"
    assert result.redirect_chain == ("https://example.com/article",)
    assert result.content_type == "text/html"
    assert "Hello" in result.text


@pytest.mark.asyncio
async def test_fetch_revalidates_redirect_target_before_requesting_it():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        with pytest.raises(UnsafeWebUrl):
            await fetcher.fetch("https://example.com/start")

    assert requests == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_fetch_enforces_streamed_byte_limit():
    def handler(_request):
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"123456")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        with pytest.raises(ResponseTooLarge):
            await fetcher.fetch("https://example.com/file", max_bytes=5)


@pytest.mark.asyncio
async def test_fetch_rejects_binary_content_before_downloading_for_page_extraction():
    def handler(_request):
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        with pytest.raises(UnsupportedContentType):
            await fetcher.fetch("https://example.com/image.png")


@pytest.mark.asyncio
async def test_fetch_surfaces_http_errors_and_redirect_budget():
    def error_handler(_request):
        return httpx.Response(503, headers={"content-type": "text/plain"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(error_handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        with pytest.raises(WebHttpStatusError) as error:
            await fetcher.fetch("https://example.com/unavailable")
        assert error.value.status_code == 503

    def redirect_handler(_request):
        return httpx.Response(302, headers={"location": "/again"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler)) as client:
        fetcher = AsyncWebFetcher(client=client, resolver=public_resolver)
        with pytest.raises(TooManyRedirects):
            await fetcher.fetch("https://example.com/start", max_redirects=1)
