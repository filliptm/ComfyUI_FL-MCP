import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from backend import comfy_registry
from backend.comfy_registry import (
    ComfyRegistryClient,
    normalize_github_repository_url,
    normalize_registry_search_query,
    validate_repository_url,
)


@dataclass
class FakeResponse:
    path: str
    payload: Any
    status: int = 200
    params: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


class FakeAsyncClient:
    def __init__(self, responses, calls, *args, **kwargs):
        self.responses = responses
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        assert self.responses, f"Unexpected Registry request: {url}"
        expected = self.responses.pop(0)
        self.calls.append({"url": url, "params": params})
        assert url.endswith(expected.path)
        if expected.params is not None:
            assert params == expected.params
        request = httpx.Request("GET", url, params=params)
        if isinstance(expected.payload, str):
            return httpx.Response(
                expected.status,
                text=expected.payload,
                headers=expected.headers,
                request=request,
            )
        return httpx.Response(
            expected.status,
            json=expected.payload,
            headers=expected.headers,
            request=request,
        )


def install_fake_http(monkeypatch, responses):
    remaining = list(responses)
    calls = []

    def factory(*args, **kwargs):
        return FakeAsyncClient(remaining, calls, *args, **kwargs)

    monkeypatch.setattr(comfy_registry.httpx, "AsyncClient", factory)
    return calls, remaining


def package(
    package_id="example-pack",
    *,
    name=None,
    repository=None,
    description="Useful image tools",
    downloads=10,
    stars=2,
    rating=0,
    supported_os=None,
    supported_accelerators=None,
    status="NodeStatusActive",
    version_status="NodeVersionStatusActive",
    deprecated=False,
    version="1.2.3",
    search_ranking=0,
    tags_admin=None,
):
    return {
        "id": package_id,
        "name": name or package_id,
        "description": description,
        "category": "image",
        "author": "Author",
        "publisher": {"id": "publisher", "status": "PublisherStatusActive"},
        "repository": repository,
        "downloads": downloads,
        "github_stars": stars,
        "rating": rating,
        "search_ranking": search_ranking,
        "supported_os": supported_os,
        "supported_accelerators": supported_accelerators,
        "status": status,
        "status_detail": "",
        "tags": ["image"],
        "tags_admin": tags_admin or [],
        "latest_version": {
            "version": version,
            "status": version_status,
            "deprecated": deprecated,
            "createdAt": "2026-01-01T00:00:00Z",
            "supported_os": supported_os,
            "supported_accelerators": supported_accelerators,
            "tags_admin": tags_admin or [],
        },
    }


def search_payload(*nodes):
    return {
        "nodes": list(nodes),
        "total": len(nodes),
        "page": 1,
        "limit": max(1, len(nodes)),
        "totalPages": 1,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://github.com/Owner/Repo", "https://github.com/Owner/Repo"),
        ("https://github.com/Owner/Repo.git/", "https://github.com/Owner/Repo"),
        ("git@github.com:Owner/Repo.git", "https://github.com/Owner/Repo"),
        ("ssh://git@github.com/Owner/Repo.git", "https://github.com/Owner/Repo"),
        ("git+https://github.com/Owner/Repo.git", "https://github.com/Owner/Repo"),
    ],
)
def test_normalize_github_repository_forms(value, expected):
    assert normalize_github_repository_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://github.com/owner/repo",
        "https://github.com.evil.test/owner/repo",
        "https://www.github.com/owner/repo",
        "https://github.com/owner",
        "https://github.com/owner/repo/issues",
        "https://github.com/owner/repo?tab=readme",
        "https://user@github.com/owner/repo",
        "git@gitlab.com:owner/repo.git",
        "",
        None,
    ],
)
def test_rejects_noncanonical_or_unsafe_github_urls(value):
    assert normalize_github_repository_url(value) is None


def test_non_github_repository_validation_is_https_only():
    assert validate_repository_url("https://gitlab.com/Owner/Repo/") == (
        "https://gitlab.com/Owner/Repo"
    )
    assert validate_repository_url("http://gitlab.com/Owner/Repo") is None
    assert validate_repository_url("https://127.0.0.1/Owner/Repo") is None
    assert validate_repository_url("https://localhost/Owner/Repo") is None


def test_client_accepts_only_fixed_official_hosts():
    client = ComfyRegistryClient()
    assert client.base_url == "https://api.comfy.org"
    assert client.website_url == "https://registry.comfy.org"
    with pytest.raises(ValueError):
        ComfyRegistryClient(base_url="https://api.comfy.org.evil.test")
    with pytest.raises(ValueError):
        ComfyRegistryClient(website_url="https://registry.comfy.org/nodes")


def test_query_normalization_preserves_capability_and_quoted_phrases():
    assert normalize_registry_search_query(
        "Please find the best new ComfyUI nodes for background removal"
    ) == "background removal"
    assert normalize_registry_search_query('show me nodes for "depth estimation" please') == (
        "depth estimation"
    )
    assert normalize_registry_search_query("search for new nodes in the registry") == ""


@pytest.mark.asyncio
async def test_search_normalizes_query_ranks_deterministically_and_adds_links(
    monkeypatch,
):
    exact = package(
        "background-removal",
        name="Background Removal",
        repository="git@github.com:owner/remove-bg.git",
        downloads=1,
        supported_os=["macos"],
        supported_accelerators=["MPS"],
    )
    popular = package(
        "other-tools",
        name="Other Tools",
        repository="https://github.com/owner/other-tools.git",
        description="Background removal and masking",
        downloads=1_000_000,
        stars=5_000,
    )
    calls, remaining = install_fake_http(
        monkeypatch,
        [FakeResponse("/nodes/search", search_payload(popular, exact))],
    )
    client = ComfyRegistryClient()

    result = await client.search_packages(
        "Please find the best new nodes for background removal in ComfyUI",
        installed_pack_ids=["OTHER-TOOLS"],
        include_installed=True,
    )

    assert result["ok"] is True
    assert result["registry_query"] == "background removal"
    assert [item["package_id"] for item in result["results"]] == [
        "background-removal",
        "other-tools",
    ]
    assert result["results"][0]["github_url"] == "https://github.com/owner/remove-bg"
    assert result["results"][0]["registry_url"] == (
        "https://registry.comfy.org/nodes/background-removal"
    )
    assert result["results"][0]["links"]["registry"] == {
        "url": "https://registry.comfy.org/nodes/background-removal",
        "host": "registry.comfy.org",
        "validated": True,
        "source": "validated_package_id",
    }
    assert result["results"][0]["links"]["github"]["host"] == "github.com"
    assert result["results"][0]["compatibility"]["macos_apple_silicon"] == "compatible"
    assert result["results"][1]["installed"] is True
    assert result["results"][1]["installation_state"] == "installed"
    assert calls[0]["params"]["search"] == "background removal"
    assert result["source"]["original_query"].startswith("Please find")
    assert result["source"]["query_mode"] == "capability_search"
    datetime.fromisoformat(result["source"]["retrieved_at"])
    assert not remaining
    json.dumps(result)


@pytest.mark.asyncio
async def test_search_for_class_and_filters_uses_official_parameters(monkeypatch):
    item = package(
        repository="https://github.com/owner/example-pack",
        supported_os=["macos"],
        supported_accelerators=["mps"],
    )
    calls, _ = install_fake_http(
        monkeypatch, [FakeResponse("/nodes/search", search_payload(item))]
    )
    client = ComfyRegistryClient()

    result = await client.search_packages(
        "find nodes",
        comfy_node_search="BackgroundRemover",
        supported_os="macos",
        supported_accelerator="mps",
    )

    assert result["ok"] is True
    assert calls[0]["params"] == {
        "search": "",
        "page": 1,
        "limit": 20,
        "comfy_node_search": "BackgroundRemover",
        "supported_os": "macos",
        "supported_accelerator": "mps",
    }
    assert "class search" in result["results"][0]["match_reasons"][0]


@pytest.mark.asyncio
async def test_generic_new_node_request_browses_bounded_registry_and_excludes_installed(
    monkeypatch,
):
    installed = package(
        "installed-pack",
        repository="https://github.com/owner/installed",
        downloads=10_000,
    )
    candidate = package(
        "candidate-pack",
        repository="https://github.com/owner/candidate",
        downloads=10,
    )
    calls, _ = install_fake_http(
        monkeypatch,
        [FakeResponse("/nodes/search", search_payload(installed, candidate))],
    )
    client = ComfyRegistryClient()

    result = await client.search_packages(
        "search for new nodes in the registry",
        installed_pack_ids=["installed-pack"],
    )

    assert result["ok"] is True
    assert result["registry_query"] == ""
    assert result["query_mode"] == "registry_browse"
    assert [item["package_id"] for item in result["results"]] == ["candidate-pack"]
    assert any(item["reason"] == "already_installed" for item in result["skipped_results"])
    assert calls[0]["params"]["search"] == ""
    assert calls[0]["params"]["page"] == 1
    assert result["source"]["catalog_scope"] == "server_side_full_registry_search"
    assert "recent" not in json.dumps(result).casefold()


@pytest.mark.asyncio
async def test_repository_resolution_uses_only_structured_search_and_detail(monkeypatch):
    from_detail = package("detail-pack", repository=None)
    from_page = package(
        "page-pack", repository="https://gitlab.com/author/page-pack"
    )
    detail_payload = package(
        "detail-pack", repository="ssh://git@github.com/author/detail-pack.git"
    )
    page_detail = package(
        "page-pack", repository="https://gitlab.com/author/page-pack"
    )
    calls, remaining = install_fake_http(
        monkeypatch,
        [
            FakeResponse("/nodes/search", search_payload(from_detail, from_page)),
            FakeResponse("/nodes/detail-pack", detail_payload),
            FakeResponse("/nodes/page-pack", page_detail),
        ],
    )
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools")

    by_id = {item["package_id"]: item for item in result["results"]}
    assert by_id["detail-pack"]["repository_resolution"]["source"] == "detail"
    assert by_id["detail-pack"]["github_url"] == (
        "https://github.com/author/detail-pack"
    )
    assert "page-pack" not in by_id
    page_skip = next(
        item for item in result["skipped_results"] if item["package_id"] == "page-pack"
    )
    assert page_skip["reason"] == "missing_valid_github_repository"
    assert all(call["url"].startswith("https://api.comfy.org/") for call in calls)
    assert not remaining


@pytest.mark.asyncio
async def test_search_bounds_missing_repository_detail_fallbacks(monkeypatch):
    nodes = [package(f"missing-{index}", repository=None) for index in range(3)]
    calls, remaining = install_fake_http(
        monkeypatch,
        [
            FakeResponse("/nodes/search", search_payload(*nodes)),
            FakeResponse(
                "/nodes/missing-0",
                package("missing-0", repository="https://github.com/owner/zero"),
            ),
            FakeResponse(
                "/nodes/missing-1",
                package("missing-1", repository="https://github.com/owner/one"),
            ),
        ],
    )
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools")

    assert len(calls) == 3
    assert result["limits"]["detail_fallbacks_used"] == 2
    exhausted = next(
        item for item in result["skipped_results"] if item["package_id"] == "missing-2"
    )
    assert exhausted["reason"] == "repository_resolution_budget_exhausted"
    assert not remaining


@pytest.mark.asyncio
async def test_search_excludes_result_without_verifiable_github_repository(monkeypatch):
    item = package("unsafe-pack", repository="https://github.com.evil.test/a/b")
    detail = package("unsafe-pack", repository="https://gitlab.com/a/b")
    _calls, _ = install_fake_http(
        monkeypatch,
        [
            FakeResponse("/nodes/search", search_payload(item)),
            FakeResponse("/nodes/unsafe-pack", detail),
        ],
    )
    client = ComfyRegistryClient()

    result = await client.search_packages("unsafe capability")

    assert result["ok"] is True
    assert result["results"] == []
    skipped = result["skipped_results"][0]
    assert skipped["package_id"] == "unsafe-pack"
    assert skipped["registry_url"] == "https://registry.comfy.org/nodes/unsafe-pack"
    assert skipped["reason"] == "missing_valid_github_repository"
    assert skipped["link_diagnostics"]["registry"]["state"] == "validated"
    assert skipped["link_diagnostics"]["github"]["state"] == "unavailable"
    assert skipped["link_diagnostics"]["repository"]["url"] == "https://gitlab.com/a/b"


@pytest.mark.asyncio
async def test_search_cache_and_refresh(monkeypatch):
    item = package(repository="https://github.com/owner/example-pack")
    calls, remaining = install_fake_http(
        monkeypatch,
        [
            FakeResponse("/nodes/search", search_payload(item)),
            FakeResponse("/nodes/search", search_payload(item)),
        ],
    )
    client = ComfyRegistryClient(cache_ttl=300)

    first = await client.search_packages("image tools")
    second = await client.search_packages("image tools")
    refreshed = await client.search_packages("image tools", refresh=True)

    assert len(calls) == 2
    assert first["source"]["cache_state"] == "miss"
    assert second["source"]["cache_state"] == "hit"
    assert second["source"]["retrieved_at"] == first["source"]["retrieved_at"]
    assert second["source"]["served_at"]
    assert refreshed["source"]["cache_state"] == "refreshed"
    assert not remaining


@pytest.mark.asyncio
async def test_registry_retries_429_once_for_short_numeric_retry_after(monkeypatch):
    item = package(repository="https://github.com/owner/example-pack")
    calls, remaining = install_fake_http(
        monkeypatch,
        [
            FakeResponse(
                "/nodes/search",
                {"message": "rate limited"},
                status=429,
                headers={"Retry-After": "2"},
            ),
            FakeResponse("/nodes/search", search_payload(item)),
        ],
    )
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(comfy_registry.asyncio, "sleep", fake_sleep)
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools")

    assert result["ok"] is True
    assert len(calls) == 2
    assert delays == [2.0]
    assert result["source"]["rate_limit"]["outcome"] == "recovered_after_retry"
    assert not remaining


@pytest.mark.asyncio
async def test_registry_retries_short_http_date_retry_after(monkeypatch):
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    retry_at = format_datetime(now + timedelta(seconds=3), usegmt=True)
    item = package(repository="https://github.com/owner/example-pack")
    calls, _ = install_fake_http(
        monkeypatch,
        [
            FakeResponse(
                "/nodes/search",
                {"message": "rate limited"},
                status=429,
                headers={"Retry-After": retry_at},
            ),
            FakeResponse("/nodes/search", search_payload(item)),
        ],
    )
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(comfy_registry, "_utc_datetime_now", lambda: now)
    monkeypatch.setattr(comfy_registry.asyncio, "sleep", fake_sleep)
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools")

    assert result["ok"] is True
    assert len(calls) == 2
    assert delays == [3.0]
    assert result["source"]["rate_limit"]["retry_after_kind"] == "http_date"


@pytest.mark.asyncio
async def test_registry_does_not_retry_long_rate_limit_and_reports_diagnostics(
    monkeypatch,
):
    calls, remaining = install_fake_http(
        monkeypatch,
        [
            FakeResponse(
                "/nodes/search",
                {"message": "rate limited"},
                status=429,
                headers={"Retry-After": "30"},
            ),
            FakeResponse("/nodes/search", search_payload()),
        ],
    )
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools")

    assert result["ok"] is False
    assert result["error"]["code"] == "registry_rate_limited"
    assert len(calls) == 1
    assert len(remaining) == 1
    diagnostics = result["error"]["diagnostics"]
    assert diagnostics["retry_attempted"] is False
    assert diagnostics["retry_skipped_reason"] == "delay_exceeds_cap"
    assert diagnostics["retry_after_seconds"] == 30.0


@pytest.mark.asyncio
async def test_registry_does_not_retry_non_429_errors(monkeypatch):
    calls, remaining = install_fake_http(
        monkeypatch,
        [
            FakeResponse("/nodes/search", {"message": "unavailable"}, status=503),
            FakeResponse("/nodes/search", search_payload()),
        ],
    )
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools")

    assert result["ok"] is False
    assert len(calls) == 1
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_unknown_manager_state_does_not_claim_package_is_uninstalled(monkeypatch):
    item = package(repository="https://github.com/owner/example-pack")
    install_fake_http(
        monkeypatch, [FakeResponse("/nodes/search", search_payload(item))]
    )
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools", installed_pack_ids=None)

    assert result["results"][0]["installed"] is None
    assert result["results"][0]["installation_state"] == "unknown"


@pytest.mark.asyncio
async def test_installed_identity_envelope_matches_canonical_github_repository(
    monkeypatch,
):
    item = package(
        "renamed-pack",
        repository="git@github.com:Owner/Shared-Repo.git",
    )
    install_fake_http(
        monkeypatch, [FakeResponse("/nodes/search", search_payload(item))]
    )
    client = ComfyRegistryClient()

    result = await client.search_packages(
        "image tools",
        installed_pack_ids={
            "package_ids": [],
            "repository_urls": ["https://github.com/owner/shared-repo.git"],
            "identity_complete": True,
        },
        include_installed=True,
    )

    candidate = result["results"][0]
    assert candidate["installed"] is True
    assert candidate["installation_state"] == "installed"
    assert candidate["installation_match"] == "repository_url"


@pytest.mark.asyncio
async def test_incomplete_installed_identity_envelope_leaves_nonmatch_unknown(
    monkeypatch,
):
    item = package(repository="https://github.com/owner/example-pack")
    install_fake_http(
        monkeypatch, [FakeResponse("/nodes/search", search_payload(item))]
    )
    client = ComfyRegistryClient()

    result = await client.search_packages(
        "image tools",
        installed_pack_ids={
            "package_ids": ["some-other-pack"],
            "repository_urls": [],
            "identity_complete": False,
        },
    )

    candidate = result["results"][0]
    assert candidate["installed"] is None
    assert candidate["installation_state"] == "unknown"
    assert candidate["installation_identity_complete"] is False


@pytest.mark.asyncio
async def test_official_search_ranking_is_bounded_secondary_tie_signal(monkeypatch):
    lower = package(
        "alpha-pack",
        repository="https://github.com/owner/alpha",
        search_ranking=1,
    )
    higher = package(
        "zulu-pack",
        repository="https://github.com/owner/zulu",
        search_ranking=10,
    )
    install_fake_http(
        monkeypatch, [FakeResponse("/nodes/search", search_payload(lower, higher))]
    )
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools")

    assert [item["package_id"] for item in result["results"]] == [
        "zulu-pack",
        "alpha-pack",
    ]
    assert result["results"][0]["official_search_ranking"] == 10


@pytest.mark.asyncio
async def test_active_safety_and_m4_signals_rank_ahead_of_popularity(monkeypatch):
    compatible = package(
        "compatible-pack",
        repository="https://github.com/owner/compatible",
        supported_os=["OS Independent"],
        supported_accelerators=["MPS"],
        downloads=1,
    )
    incompatible = package(
        "incompatible-pack",
        repository="https://github.com/owner/incompatible",
        supported_os=["linux"],
        downloads=10_000_000,
        stars=100_000,
    )
    inactive = package(
        "inactive-pack",
        repository="https://github.com/owner/inactive",
        supported_os=["OS Independent"],
        supported_accelerators=["CPU"],
        deprecated=True,
        downloads=100_000_000,
        stars=1_000_000,
    )
    blocked = package(
        "blocked-pack",
        repository="https://github.com/owner/blocked",
        supported_os=["OS Independent"],
        supported_accelerators=["Metal"],
        status="NodeStatusBanned",
        downloads=1_000_000_000,
        stars=10_000_000,
    )
    install_fake_http(
        monkeypatch,
        [
            FakeResponse(
                "/nodes/search",
                search_payload(blocked, inactive, incompatible, compatible),
            )
        ],
    )
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools")

    assert [item["package_id"] for item in result["results"]] == [
        "compatible-pack",
        "incompatible-pack",
        "inactive-pack",
    ]
    assert result["results"][0]["ranking_signals"]["active"] is True
    assert result["results"][0]["ranking_signals"]["nonblocked"] is True
    assert result["results"][0]["ranking_signals"]["macos_apple_silicon_rank"] == 2
    blocked_skip = next(
        item for item in result["skipped_results"] if item["package_id"] == "blocked-pack"
    )
    assert blocked_skip["reason"] == "security_blocked"
    assert blocked_skip["security"]["state"] == "blocked"


@pytest.mark.asyncio
async def test_compatibility_and_security_metadata(monkeypatch):
    mac = package(
        "mac-pack",
        repository="https://github.com/owner/mac",
        supported_os=["macOS"],
    )
    linux = package(
        "linux-pack",
        repository="https://github.com/owner/linux",
        supported_os=["linux"],
    )
    unknown = package(
        "unknown-pack",
        repository="https://github.com/owner/unknown",
        status="NodeStatusBanned",
        version_status="NodeVersionStatusFlagged",
        deprecated=True,
        tags_admin=["malware-review"],
    )
    independent = package(
        "independent-pack",
        repository="https://github.com/owner/independent",
        supported_os=["OS Independent"],
        tags_admin=["manual-review"],
    )
    cuda_only = package(
        "cuda-pack",
        repository="https://github.com/owner/cuda",
        supported_os=["OS Independent"],
        supported_accelerators=["CUDA"],
    )
    apple_ready = package(
        "apple-pack",
        repository="https://github.com/owner/apple",
        supported_os=["macOS"],
        supported_accelerators=["MPS"],
    )
    install_fake_http(
        monkeypatch,
        [
            FakeResponse(
                "/nodes/search",
                search_payload(mac, linux, unknown, independent, cuda_only, apple_ready),
            )
        ],
    )
    client = ComfyRegistryClient()

    result = await client.search_packages("image tools")

    by_id = {item["package_id"]: item for item in result["results"]}
    assert by_id["mac-pack"]["compatibility"]["macos_apple_silicon"] == "unknown"
    assert by_id["mac-pack"]["compatibility"]["os_state"] == "compatible"
    assert by_id["mac-pack"]["compatibility"]["accelerator_state"] == "unknown"
    assert by_id["linux-pack"]["compatibility"]["macos_apple_silicon"] == "incompatible"
    assert by_id["independent-pack"]["compatibility"]["macos_apple_silicon"] == (
        "unknown"
    )
    assert by_id["cuda-pack"]["compatibility"]["macos_apple_silicon"] == "incompatible"
    assert by_id["apple-pack"]["compatibility"] == {
        "macos_apple_silicon": "compatible",
        "os_state": "compatible",
        "accelerator_state": "compatible",
        "reasons": [
            "The package explicitly lists macOS support.",
            "The package lists an Apple Silicon-compatible accelerator path.",
        ],
        "supported_os": ["macOS"],
        "supported_accelerators": ["MPS"],
    }
    assert by_id["independent-pack"]["security"]["state"] == "review"
    unknown_skip = next(
        item for item in result["skipped_results"] if item["package_id"] == "unknown-pack"
    )
    assert unknown_skip["reason"] == "security_blocked"
    assert unknown_skip["security"]["admin_tags"] == ["malware-review"]


def comfy_class(name):
    return {
        "comfy_node_name": name,
        "category": "image",
        "description": f"{name} description",
        "deprecated": False,
        "experimental": False,
        "input_types": "{}",
        "return_types": "[]",
    }


@pytest.mark.asyncio
async def test_get_package_fetches_available_class_metadata_with_bounded_pages(
    monkeypatch,
):
    detail = package(repository="https://github.com/owner/example-pack")
    calls, remaining = install_fake_http(
        monkeypatch,
        [
            FakeResponse("/nodes/example-pack", detail),
            FakeResponse(
                "/nodes/example-pack/versions/1.2.3/comfy-nodes",
                {
                    "comfy_nodes": [comfy_class("Zulu"), comfy_class("Alpha")],
                    "totalNumberOfPages": 2,
                },
            ),
            FakeResponse(
                "/nodes/example-pack/versions/1.2.3/comfy-nodes",
                {
                    "comfy_nodes": [comfy_class("Beta")],
                    "totalNumberOfPages": 2,
                },
            ),
        ],
    )
    client = ComfyRegistryClient()

    result = await client.get_package("example-pack", max_classes=150)

    assert result["ok"] is True
    assert result["package"]["class_metadata_state"] == "available"
    assert [node["comfy_node_name"] for node in result["package"]["comfy_nodes"]] == [
        "Alpha",
        "Beta",
        "Zulu",
    ]
    assert result["package"]["class_count"] == 3
    assert result["limits"]["class_pages_fetched"] == 2
    class_calls = [call for call in calls if call["url"].endswith("/comfy-nodes")]
    assert [call["params"]["page"] for call in class_calls] == [1, 2]
    assert all(call["params"] == {"page": index, "limit": 100} for index, call in enumerate(class_calls, 1))
    assert not remaining


@pytest.mark.asyncio
async def test_get_package_marks_class_metadata_partial_at_bound(monkeypatch):
    detail = package(repository="https://github.com/owner/example-pack")
    calls, _ = install_fake_http(
        monkeypatch,
        [
            FakeResponse("/nodes/example-pack", detail),
            FakeResponse(
                "/nodes/example-pack/versions/1.2.3/comfy-nodes",
                {
                    "comfy_nodes": [comfy_class("One"), comfy_class("Two")],
                    "totalNumberOfPages": 3,
                },
            ),
        ],
    )
    client = ComfyRegistryClient()

    result = await client.get_package("example-pack", max_classes=2)

    assert result["package"]["class_metadata_state"] == "partial"
    assert result["package"]["class_count"] == 2
    assert "capped at 2" in result["package"]["class_metadata_reason"]
    assert len([call for call in calls if call["url"].endswith("/comfy-nodes")]) == 1


@pytest.mark.asyncio
async def test_get_package_marks_missing_class_metadata_on_404(monkeypatch):
    detail = package(repository="https://github.com/owner/example-pack")
    install_fake_http(
        monkeypatch,
        [
            FakeResponse("/nodes/example-pack", detail),
            FakeResponse(
                "/nodes/example-pack/versions/1.2.3/comfy-nodes",
                {"error": "not found"},
                status=404,
            ),
        ],
    )
    client = ComfyRegistryClient()

    result = await client.get_package("example-pack")

    assert result["ok"] is True
    assert result["package"]["class_metadata_state"] == "missing"
    assert result["package"]["comfy_nodes"] == []


@pytest.mark.asyncio
async def test_get_package_marks_null_zero_page_class_metadata_missing(monkeypatch):
    detail = package(repository="https://github.com/owner/example-pack")
    install_fake_http(
        monkeypatch,
        [
            FakeResponse("/nodes/example-pack", detail),
            FakeResponse(
                "/nodes/example-pack/versions/1.2.3/comfy-nodes",
                {"comfy_nodes": None, "totalNumberOfPages": 0},
            ),
        ],
    )
    client = ComfyRegistryClient()

    result = await client.get_package("example-pack")

    assert result["package"]["class_metadata_state"] == "missing"
    assert result["package"]["class_metadata_reason"] == (
        "No class metadata is published for the latest version."
    )
    assert result["package"]["comfy_nodes"] == []


@pytest.mark.asyncio
async def test_get_package_without_latest_version_does_not_fetch_class_pages(monkeypatch):
    detail = package(repository="https://github.com/owner/example-pack", version="")
    calls, remaining = install_fake_http(
        monkeypatch, [FakeResponse("/nodes/example-pack", detail)]
    )
    client = ComfyRegistryClient()

    result = await client.get_package("example-pack")

    assert result["package"]["class_metadata_state"] == "missing"
    assert result["limits"]["class_pages_fetched"] == 0
    assert len(calls) == 1
    assert not remaining


@pytest.mark.asyncio
async def test_get_package_matches_id_case_insensitively_and_returns_canonical_id(
    monkeypatch,
):
    detail = package(
        "Canonical-Pack",
        repository="https://github.com/owner/canonical-pack",
        version="",
    )
    calls, _ = install_fake_http(
        monkeypatch, [FakeResponse("/nodes/canonical-pack", detail)]
    )
    client = ComfyRegistryClient()

    result = await client.get_package("canonical-pack")

    assert result["ok"] is True
    assert result["package"]["package_id"] == "Canonical-Pack"
    assert result["package"]["registry_url"] == (
        "https://registry.comfy.org/nodes/Canonical-Pack"
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_get_package_reports_unverifiable_repository(monkeypatch):
    detail = package(repository="https://gitlab.com/owner/example-pack")
    calls, remaining = install_fake_http(
        monkeypatch,
        [FakeResponse("/nodes/example-pack", detail)],
    )
    client = ComfyRegistryClient()

    result = await client.get_package("example-pack")

    assert result["ok"] is False
    assert result["error"]["code"] == "missing_valid_github_repository"
    assert result["registry_url"] == "https://registry.comfy.org/nodes/example-pack"
    assert result["repository_url"] == "https://gitlab.com/owner/example-pack"
    assert result["link_diagnostics"]["registry"]["state"] == "validated"
    assert result["link_diagnostics"]["github"]["state"] == "unavailable"
    assert len(calls) == 1
    assert not remaining


@pytest.mark.asyncio
async def test_error_paths_are_clear_and_json_serializable(monkeypatch):
    client = ComfyRegistryClient()
    invalid = await client.get_package("../escape")
    invalid_limit = await client.search_packages("image", max_results=0)
    assert invalid["error"]["code"] == "invalid_package_id"
    assert invalid["link_diagnostics"]["registry"]["state"] == "unavailable"
    assert invalid_limit["error"]["code"] == "invalid_limit"

    install_fake_http(
        monkeypatch,
        [FakeResponse("/nodes/search", {"message": "unavailable"}, status=503)],
    )
    unavailable = await client.search_packages("image")
    assert unavailable["ok"] is False
    assert unavailable["error"] == {
        "code": "registry_request_failed",
        "message": "Official Comfy Registry returned HTTP 503",
        "status_code": 503,
    }
    json.dumps(unavailable)


@pytest.mark.asyncio
async def test_get_package_404_has_specific_error(monkeypatch):
    install_fake_http(
        monkeypatch,
        [FakeResponse("/nodes/missing-pack", {"message": "missing"}, status=404)],
    )
    client = ComfyRegistryClient()

    result = await client.get_package("missing-pack")

    assert result["ok"] is False
    assert result["error"]["code"] == "package_not_found"
    assert result["error"]["status_code"] == 404
