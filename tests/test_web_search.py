import httpx
import pytest
from web_search import (
    SearchCredentialError,
    SearchRateLimitError,
    WebSearchError,
    WebSearchService,
    validate_search_mode,
)

FREE_HTML = """
<!doctype html><html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide%3Futm_source%3Dddg&amp;rut=abc">Example guide</a>
    <a class="result__snippet">A useful guide for the requested topic.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://second.example/story">Second story</a>
    <a class="result__snippet">Another relevant source.</a>
  </div>
</body></html>
"""


def test_search_mode_validation_fails_closed():
    assert validate_search_mode("FREE") == "free"
    with pytest.raises(ValueError, match="Unsupported web search mode"):
        validate_search_mode("automatic-paid-search")


@pytest.mark.asyncio
async def test_free_search_needs_no_key_and_normalizes_result_urls():
    def handler(request):
        assert request.url.host == "html.duckduckgo.com"
        assert request.url.params["q"] == "comfy search"
        assert request.url.params["df"] == "w"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=FREE_HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WebSearchService(mode="free", client=client)
        response = await service.search("  comfy   search ", max_results=2, time_range="week")

    assert response.provider == "free"
    assert response.credits_used == 0
    assert [item.url for item in response.results] == [
        "https://example.com/guide",
        "https://second.example/story",
    ]
    assert response.results[0].snippet == "A useful guide for the requested topic."


@pytest.mark.asyncio
async def test_free_search_reports_rate_limits_clearly():
    def handler(_request):
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WebSearchService(mode="free", client=client)
        with pytest.raises(SearchRateLimitError, match="choose Tavily"):
            await service.search("query")


@pytest.mark.asyncio
async def test_tavily_basic_disables_auto_parameters_and_reports_actual_credits():
    def handler(request):
        assert request.url.host == "api.tavily.com"
        assert request.headers["authorization"] == "Bearer tvly-test"
        payload = request.read()
        assert b'"search_depth":"basic"' in payload
        assert b'"auto_parameters":false' in payload
        return httpx.Response(200, json={
            "results": [{
                "title": "Primary source",
                "url": "https://example.com/source?gclid=tracking",
                "content": "Relevant evidence",
                "score": 0.91,
            }],
            "usage": {"credits": 1},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WebSearchService(
            mode="tavily_basic",
            tavily_api_key="tvly-test",
            client=client,
        )
        response = await service.search("current information")

    assert response.provider == "tavily"
    assert response.mode == "tavily_basic"
    assert response.credits_used == 1
    assert response.results[0].url == "https://example.com/source"
    assert response.results[0].score == 0.91


@pytest.mark.asyncio
async def test_tavily_advanced_requires_key_before_network_request():
    def handler(_request):
        raise AssertionError("request must not be sent without a Tavily key")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WebSearchService(mode="tavily_advanced", client=client)
        with pytest.raises(SearchCredentialError, match="Settings"):
            await service.search("deep research")


@pytest.mark.asyncio
async def test_disabled_mode_and_invalid_limits_fail_before_network_request():
    def handler(_request):
        raise AssertionError("disabled or invalid searches must not make requests")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WebSearchService(mode="off", client=client)
        with pytest.raises(WebSearchError, match="off"):
            await service.search("query")
        with pytest.raises(ValueError, match="between 1 and 10"):
            await service.search("query", max_results=20)
