"""Lightweight no-key and Tavily web-search adapters."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser
from web_models import WebSearchResponse, WebSearchResult
from web_security import WebUrlError, canonicalize_web_url

FREE_SEARCH_URL = "https://html.duckduckgo.com/html/"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SEARCH_MODES = frozenset({"off", "free", "tavily_basic", "tavily_advanced"})
TIME_RANGES = frozenset({"day", "week", "month", "year"})
FREE_TIME_RANGES = {"day": "d", "week": "w", "month": "m", "year": "y"}


class WebSearchError(RuntimeError):
    """Base class for provider failures that should be explained to the user."""


class SearchCredentialError(WebSearchError):
    """Raised when a selected provider requires a missing credential."""


class SearchRateLimitError(WebSearchError):
    """Raised when a provider refuses a request because of usage limits."""


def validate_search_mode(value: str) -> str:
    mode = str(value or "off").strip().lower()
    if mode not in SEARCH_MODES:
        raise ValueError(f"Unsupported web search mode: {mode}")
    return mode


def _result_url(candidate: str | None) -> str | None:
    """Unwrap DuckDuckGo redirect links and normalize public-web syntax."""

    if not candidate:
        return None
    raw = candidate.strip()
    if raw.startswith("//"):
        raw = f"https:{raw}"
    parts = urlsplit(raw)
    if (parts.hostname or "").lower().endswith("duckduckgo.com") and parts.path == "/l/":
        raw = (parse_qs(parts.query).get("uddg") or [""])[0]
    try:
        return canonicalize_web_url(raw)
    except WebUrlError:
        return None


class FreeWebSearch:
    """No-key, no-cost best-effort search using DuckDuckGo's HTML results."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        time_range: str | None = None,
    ) -> WebSearchResponse:
        params = {"q": query, "kl": "wt-wt"}
        if time_range:
            params["df"] = FREE_TIME_RANGES[time_range]
        try:
            response = await self._client.get(FREE_SEARCH_URL, params=params)
        except httpx.HTTPError as exc:
            raise WebSearchError(f"Free web search could not connect: {exc}") from exc
        if response.status_code == 429:
            raise SearchRateLimitError(
                "Free web search is temporarily rate-limited. Try again later or choose Tavily."
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise WebSearchError(f"Free web search returned HTTP {response.status_code}")

        parser = LexborHTMLParser(response.text)
        if parser.css_first(".anomaly-modal, #challenge-form") is not None:
            raise SearchRateLimitError(
                "Free web search requested a bot check. Try again later or choose Tavily."
            )
        results: list[WebSearchResult] = []
        seen: set[str] = set()
        for item in parser.css(".result"):
            link = item.css_first("a.result__a")
            if link is None:
                continue
            url = _result_url(link.attributes.get("href"))
            if not url or url in seen:
                continue
            title = link.text(deep=True, separator=" ", strip=True).strip()
            if not title:
                continue
            snippet_node = item.css_first(".result__snippet")
            snippet = (
                snippet_node.text(deep=True, separator=" ", strip=True).strip()
                if snippet_node is not None
                else ""
            )
            seen.add(url)
            results.append(WebSearchResult(title=title, url=url, snippet=snippet))
            if len(results) >= max_results:
                break
        warnings = []
        if not results:
            warnings.append(
                "Free web search returned no results. It may be temporarily limited; Tavily is the managed fallback."
            )
        return WebSearchResponse(
            query=query,
            provider="free",
            mode="free",
            results=results,
            warnings=warnings,
        )


class TavilyWebSearch:
    """Small REST adapter that avoids adding Tavily's SDK as a dependency."""

    def __init__(self, client: httpx.AsyncClient, api_key: str | None) -> None:
        self._client = client
        self._api_key = str(api_key or "").strip()

    async def search(
        self,
        query: str,
        *,
        depth: str,
        max_results: int = 5,
        time_range: str | None = None,
    ) -> WebSearchResponse:
        if not self._api_key:
            raise SearchCredentialError(
                "Tavily search needs an API key. Add one in Ren Settings → Web search."
            )
        payload: dict[str, object] = {
            "query": query,
            "search_depth": depth,
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "auto_parameters": False,
        }
        if time_range:
            payload["time_range"] = time_range
        try:
            response = await self._client.post(
                TAVILY_SEARCH_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise WebSearchError(f"Tavily search could not connect: {exc}") from exc
        if response.status_code == 401:
            raise SearchCredentialError(
                "Tavily rejected the API key. Update it in Ren Settings → Web search."
            )
        if response.status_code in {429, 432, 433}:
            raise SearchRateLimitError(
                "Tavily's rate or credit limit was reached. Choose Free web or update the Tavily plan."
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise WebSearchError(f"Tavily search returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise WebSearchError("Tavily returned an invalid JSON response") from exc

        results: list[WebSearchResult] = []
        seen: set[str] = set()
        for item in data.get("results", []):
            if not isinstance(item, dict):
                continue
            url = _result_url(str(item.get("url") or ""))
            title = str(item.get("title") or "").strip()
            if not url or not title or url in seen:
                continue
            score_value = item.get("score")
            score = float(score_value) if isinstance(score_value, (int, float)) else None
            seen.add(url)
            results.append(
                WebSearchResult(
                    title=title,
                    url=url,
                    snippet=str(item.get("content") or "").strip(),
                    score=score,
                    published_date=str(item.get("published_date") or "").strip() or None,
                )
            )
            if len(results) >= max_results:
                break
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        credits = usage.get("credits", 2 if depth == "advanced" else 1)
        return WebSearchResponse(
            query=query,
            provider="tavily",
            mode=f"tavily_{depth}",
            results=results,
            credits_used=max(0, int(credits)),
        )


class WebSearchService:
    """Route a request to the search mode selected in Ren's composer."""

    def __init__(
        self,
        *,
        mode: str,
        tavily_api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.mode = validate_search_mode(mode)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "text/html,application/json;q=0.9",
                "User-Agent": "ComfyUI-FL-MCP-Ren/0.6 (+https://github.com/filliptm/ComfyUI_FL-MCP)",
            },
        )
        self._free = FreeWebSearch(self._client)
        self._tavily = TavilyWebSearch(self._client, tavily_api_key)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        time_range: str | None = None,
    ) -> WebSearchResponse:
        normalized_query = " ".join(str(query).split())
        if not normalized_query:
            raise ValueError("Search query cannot be empty")
        if len(normalized_query) > 500:
            raise ValueError("Search query cannot exceed 500 characters")
        if max_results < 1 or max_results > 10:
            raise ValueError("max_results must be between 1 and 10")
        normalized_range = str(time_range or "").strip().lower() or None
        if normalized_range and normalized_range not in TIME_RANGES:
            raise ValueError(f"Unsupported search time range: {normalized_range}")
        if self.mode == "off":
            raise WebSearchError("Web search is off for this message")
        if self.mode == "free":
            return await self._free.search(
                normalized_query,
                max_results=max_results,
                time_range=normalized_range,
            )
        depth = "advanced" if self.mode == "tavily_advanced" else "basic"
        return await self._tavily.search(
            normalized_query,
            depth=depth,
            max_results=max_results,
            time_range=normalized_range,
        )
