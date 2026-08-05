from datetime import UTC, datetime
from types import SimpleNamespace

import mcp_server
import pytest
from web_models import ExtractedWebPage, WebImageCandidate


class FakeWebPageService:
    async def fetch_page(self, url, *, force_refresh=False):
        del force_refresh
        return ExtractedWebPage(
            requested_url=url,
            final_url=url,
            canonical_url=url,
            title="Reference page",
            text="Useful page content",
            markdown="Useful page content",
            images=[WebImageCandidate(
                url="https://example.com/reference.jpg",
                source_url=url,
                alt="Reference",
            )],
            content_hash="a" * 64,
            content_type="text/html",
            status_code=200,
            fetched_at=datetime(2026, 8, 4, tzinfo=UTC),
            elapsed_ms=5,
            quality_score=1,
        )


def fake_context(*, images_allowed):
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context={
        "client": None,
        "web_pages": FakeWebPageService(),
        "web_images_allowed": images_allowed,
    }))


@pytest.mark.asyncio
async def test_web_fetch_page_omits_images_by_default():
    result = await mcp_server.web_fetch_page.fn(
        mcp_server.WebFetchPageRequest(url="https://example.com/story"),
        fake_context(images_allowed=True),
    )

    assert result["images"] == []
    assert result["imagesIncluded"] is False


@pytest.mark.asyncio
async def test_web_fetch_page_requires_request_and_message_gate_for_images():
    request = mcp_server.WebFetchPageRequest(
        url="https://example.com/story",
        include_images=True,
    )
    blocked = await mcp_server.web_fetch_page.fn(
        request,
        fake_context(images_allowed=False),
    )
    allowed = await mcp_server.web_fetch_page.fn(
        request,
        fake_context(images_allowed=True),
    )

    assert blocked["images"] == []
    assert blocked["imagesIncluded"] is False
    assert "did not explicitly request" in blocked["warnings"][-1]
    assert allowed["images"][0]["url"] == "https://example.com/reference.jpg"
    assert allowed["imagesIncluded"] is True
