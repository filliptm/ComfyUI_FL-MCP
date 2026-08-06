"""Small, read-only client for the official Comfy Registry.

The client deliberately performs server-side searches instead of mirroring the
registry.  Returned packages are safe to present to a user only after both the
official registry URL and the package's canonical GitHub repository have been
validated.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

OFFICIAL_API_URL = "https://api.comfy.org"
OFFICIAL_WEBSITE_URL = "https://registry.comfy.org"
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_CANDIDATES = 40
MAX_SEARCH_DETAIL_FALLBACKS = 2
MAX_CLASS_METADATA = 200
CLASS_PAGE_SIZE = 100
MAX_CLASS_PAGES = 2
MAX_429_RETRY_DELAY = 5.0
MAX_SHORT_TEXT = 512
MAX_DESCRIPTION_TEXT = 4_096
MAX_METADATA_TEXT = 8_192
MAX_METADATA_ITEMS = 256

_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_QUOTED_RE = re.compile(r'''["']([^"']+)["']''')
_SEARCH_INTENT_WORDS = {
    "a",
    "about",
    "all",
    "and",
    "best",
    "can",
    "comfy",
    "comfyui",
    "could",
    "find",
    "for",
    "from",
    "good",
    "i",
    "in",
    "latest",
    "me",
    "new",
    "node",
    "nodes",
    "of",
    "on",
    "pack",
    "packs",
    "please",
    "pls",
    "registry",
    "search",
    "show",
    "some",
    "that",
    "the",
    "to",
    "want",
    "which",
    "with",
}


class ComfyRegistryError(Exception):
    """Base error for official Registry requests."""


class ComfyRegistryHTTPError(ComfyRegistryError):
    """A bounded official Registry request failed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        diagnostics: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class _CacheEntry:
    value: Any
    stored_at: float
    expires_at: float
    fetched_at: str


@dataclass(frozen=True)
class _InstalledIdentities:
    package_ids: frozenset[str]
    repository_urls: frozenset[str]
    identity_complete: bool


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _utc_datetime_now() -> datetime:
    return datetime.now(UTC)


def _retry_after_seconds(value: str, *, now: datetime | None = None) -> tuple[float | None, str]:
    raw = value.strip()
    if not raw:
        return None, "missing"
    try:
        seconds = float(raw)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None, "invalid"
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current = now or _utc_datetime_now()
        seconds = max(0.0, (retry_at.astimezone(UTC) - current.astimezone(UTC)).total_seconds())
        return seconds, "http_date"
    if not math.isfinite(seconds) or seconds < 0:
        return None, "invalid"
    return seconds, "numeric"


def _validate_official_origin(value: str, expected_host: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Official host must be {expected_host}")
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Official host must be {expected_host}") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Official host must be https://{expected_host}")
    return f"https://{expected_host}"


def _valid_package_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _PACKAGE_ID_RE.fullmatch(value) else None


def _valid_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _VERSION_RE.fullmatch(value) else None


def normalize_github_repository_url(value: Any) -> str | None:
    """Normalize supported GitHub HTTPS/SSH repository forms.

    Only an exact ``github.com/{owner}/{repo}`` repository is accepted.  Paths
    to issues, trees, releases, or lookalike hosts are rejected.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(character in raw for character in ("\r", "\n", "\t")):
        return None

    owner: str | None = None
    repository: str | None = None
    scp_match = re.fullmatch(
        r"git@github\.com:([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?",
        raw,
        flags=re.IGNORECASE,
    )
    if scp_match:
        owner, repository = scp_match.groups()
    else:
        candidate = raw[4:] if raw.casefold().startswith("git+https://") else raw
        parsed = urlsplit(candidate)
        try:
            port = parsed.port
        except ValueError:
            return None
        scheme = parsed.scheme.casefold()
        if scheme not in {"https", "ssh"}:
            return None
        if parsed.hostname is None or parsed.hostname.casefold() != "github.com":
            return None
        if port is not None or parsed.password is not None or parsed.query or parsed.fragment:
            return None
        if scheme == "https" and parsed.username is not None:
            return None
        if scheme == "ssh" and parsed.username not in (None, "git"):
            return None
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) != 2:
            return None
        owner, repository = segments

    if repository.casefold().endswith(".git"):
        repository = repository[:-4]
    if (
        not owner
        or not repository
        or not _GITHUB_OWNER_RE.fullmatch(owner)
        or not _GITHUB_REPO_RE.fullmatch(repository)
        or owner in {".", ".."}
        or repository in {".", ".."}
        or "%" in owner
        or "%" in repository
    ):
        return None
    return f"https://github.com/{owner}/{repository}"


def validate_repository_url(value: Any) -> str | None:
    """Return a safe, normalized HTTPS repository URL, without fetching it."""
    github = normalize_github_repository_url(value)
    if github:
        return github
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return None
    if host.startswith("github.com.") or host.endswith(".github.com"):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return None
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        return None
    return urlunsplit(("https", host, path, "", ""))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return []
    bounded = {
        _bounded_text(item, MAX_SHORT_TEXT)
        for item in values[:MAX_METADATA_ITEMS]
        if isinstance(item, (str, int, float))
    }
    bounded.discard("")
    return sorted(bounded, key=str.casefold)


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = str(value)
    text = "".join(character for character in text if character >= " " or character in "\n\t")
    return text.strip()[:limit]


def _bounded_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value, MAX_METADATA_TEXT)
    if isinstance(value, list):
        return [
            _bounded_metadata(item, depth=depth + 1)
            for item in value[:MAX_METADATA_ITEMS]
        ]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        keys = sorted(value, key=lambda item: str(item).casefold())[:MAX_METADATA_ITEMS]
        for raw_key in keys:
            key = _bounded_text(raw_key, MAX_SHORT_TEXT)
            if key:
                sanitized[key] = _bounded_metadata(value[raw_key], depth=depth + 1)
        return sanitized
    return _bounded_text(value, MAX_METADATA_TEXT)


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _status_name(value: Any) -> str:
    text = str(value or "unknown").strip()
    for prefix in ("NodeVersionStatus", "NodeStatus", "PublisherStatus"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.casefold() or "unknown"


def _latest_version(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("latest_version")
    return value if isinstance(value, dict) else {}


def _compatibility(item: dict[str, Any]) -> dict[str, Any]:
    latest = _latest_version(item)
    operating_systems = _string_list(
        item.get("supported_os")
        if item.get("supported_os") is not None
        else latest.get("supported_os")
    )
    accelerators = _string_list(
        item.get("supported_accelerators")
        if item.get("supported_accelerators") is not None
        else latest.get("supported_accelerators")
    )
    os_text = " ".join(operating_systems).casefold()
    accelerator_text = " ".join(accelerators).casefold()
    mac_declared = any(token in os_text for token in ("mac", "darwin", "osx"))
    os_independent = any(
        token in os_text
        for token in ("os independent", "cross platform", "cross-platform", "any", "all")
    )
    apple_declared = any(
        token in accelerator_text
        for token in ("apple", "mps", "metal", "arm64", "aarch64", "cpu")
    )

    reasons: list[str] = []
    if not operating_systems:
        os_state = "unknown"
        reasons.append("The Registry does not publish operating-system support.")
    elif mac_declared or os_independent:
        os_state = "compatible"
        if mac_declared:
            reasons.append("The package explicitly lists macOS support.")
        if os_independent:
            reasons.append("The package declares operating-system-independent support.")
    else:
        os_state = "incompatible"
        reasons.append("Published operating-system support does not include macOS.")

    if not accelerators:
        accelerator_state = "unknown"
        reasons.append("The Registry does not publish accelerator support.")
    elif apple_declared:
        accelerator_state = "compatible"
        reasons.append("The package lists an Apple Silicon-compatible accelerator path.")
    else:
        accelerator_state = "incompatible"
        reasons.append("Published accelerator support does not include Apple Silicon, MPS, Metal, or CPU.")

    if "incompatible" in {os_state, accelerator_state}:
        status = "incompatible"
    elif os_state == "compatible" and accelerator_state == "compatible":
        status = "compatible"
    else:
        status = "unknown"
    return {
        "macos_apple_silicon": status,
        "os_state": os_state,
        "accelerator_state": accelerator_state,
        "reasons": reasons,
        "supported_os": operating_systems,
        "supported_accelerators": accelerators,
    }


def _security_metadata(item: dict[str, Any]) -> dict[str, Any]:
    latest = _latest_version(item)
    node_status = _status_name(item.get("status"))
    version_status = _status_name(latest.get("status"))
    deprecated = bool(latest.get("deprecated"))
    admin_tags = _string_list(item.get("tags_admin"))
    admin_tags = sorted(
        set(admin_tags) | set(_string_list(latest.get("tags_admin"))), key=str.casefold
    )
    reasons: list[str] = []
    blocked_states = {"banned", "deleted"}
    review_states = {"flagged", "pending"}
    if node_status in blocked_states:
        reasons.append(f"Registry package status is {node_status}.")
    if version_status in blocked_states | review_states:
        reasons.append(f"Latest version status is {version_status}.")
    if deprecated:
        reasons.append("The latest Registry version is deprecated.")
    if admin_tags:
        reasons.append(f"Registry admin/security tags: {', '.join(admin_tags)}.")
    status_detail = _bounded_text(item.get("status_detail"), MAX_DESCRIPTION_TEXT)
    if status_detail:
        reasons.append(status_detail)

    if node_status in blocked_states or version_status in blocked_states:
        state = "blocked"
    elif version_status in review_states or deprecated or status_detail or admin_tags:
        state = "review"
    elif node_status == "active" and version_status in {"active", "unknown"}:
        state = "clear"
    else:
        state = "unknown"
    return {
        "state": state,
        "reasons": reasons,
        "registry_package_status": node_status,
        "latest_version_status": version_status,
        "admin_tags": admin_tags,
    }


def _tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(value.casefold())


def normalize_registry_search_query(value: str) -> str:
    """Reduce a conversational request to capability terms for Registry search.

    The Registry's exact server-side text search handles concise capability
    phrases substantially better than full conversational requests. Quoted
    phrases are kept intact and common search-intent words are removed.
    """
    if not isinstance(value, str):
        return ""
    quoted_phrases = [" ".join(_tokens(match)) for match in _QUOTED_RE.findall(value)]
    quoted_phrases = [phrase for phrase in quoted_phrases if phrase]
    unquoted = _QUOTED_RE.sub(" ", value)
    capability_words = [
        token for token in _tokens(unquoted) if token not in _SEARCH_INTENT_WORDS
    ]
    parts: list[str] = []
    for part in [*quoted_phrases, *capability_words]:
        if part and part not in parts:
            parts.append(part)
    return " ".join(parts)[:MAX_SHORT_TEXT]


def _functional_relevance(
    item: dict[str, Any], query: str, comfy_node_search: str | None
) -> tuple[int, list[str]]:
    package_id = str(item.get("id") or "")
    name = str(item.get("name") or "")
    description = str(item.get("description") or "")
    tags = _string_list(item.get("tags"))
    q = query.strip().casefold()
    score = 0
    reasons: list[str] = []

    if comfy_node_search and comfy_node_search.strip():
        score = 900
        reasons.append(
            f'Official Registry class search matched "{comfy_node_search.strip()}".'
        )
    if q:
        id_text = package_id.casefold()
        name_text = name.casefold()
        tag_text = " ".join(tags).casefold()
        corpus = " ".join((id_text, name_text, tag_text, description.casefold()))
        query_tokens = _tokens(q)
        if q == id_text:
            package_score, reason = 1000, "Exact package ID match."
        elif q == name_text:
            package_score, reason = 980, "Exact package name match."
        elif id_text.startswith(q) or name_text.startswith(q):
            package_score, reason = 850, "Package ID or name prefix match."
        elif q in id_text or q in name_text:
            package_score, reason = 780, "Package ID or name contains the query."
        elif query_tokens and all(token in f"{id_text} {name_text}" for token in query_tokens):
            package_score, reason = 720, "All query terms match the package ID or name."
        elif q in tag_text:
            package_score, reason = 650, "Package tag match."
        elif q in description.casefold():
            package_score, reason = 600, "Package description phrase match."
        elif query_tokens:
            matches = sum(token in corpus for token in query_tokens)
            package_score = 300 + round(250 * matches / len(query_tokens))
            reason = "Official Registry search matched package metadata."
        else:
            package_score, reason = 250, "Official Registry search match."
        if package_score > score:
            score = package_score
        reasons.append(reason)
    if not reasons:
        reasons.append("Official Registry listing.")
    return score, list(dict.fromkeys(reasons))


def _quality_score(item: dict[str, Any]) -> float:
    downloads = _safe_int(item.get("downloads"))
    stars = _safe_int(item.get("github_stars"))
    rating = _safe_float(item.get("rating"))
    official_search_ranking = min(100.0, _safe_float(item.get("search_ranking")))
    return round(
        math.log10(downloads + 1) * 10
        + math.log10(stars + 1) * 5
        + rating * 2
        + official_search_ranking * 0.01,
        3,
    )


class ComfyRegistryClient:
    """Read-only, bounded client for the two fixed official Comfy hosts."""

    def __init__(
        self,
        base_url: str = OFFICIAL_API_URL,
        website_url: str = OFFICIAL_WEBSITE_URL,
        timeout: float = 10,
        cache_ttl: float = 300,
    ) -> None:
        self.base_url = _validate_official_origin(base_url, "api.comfy.org")
        self.website_url = _validate_official_origin(website_url, "registry.comfy.org")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        if (
            isinstance(cache_ttl, bool)
            or not isinstance(cache_ttl, (int, float))
            or cache_ttl < 0
        ):
            raise ValueError("cache_ttl must be a non-negative number")
        self.timeout = float(timeout)
        self.cache_ttl = float(cache_ttl)
        self._cache: dict[tuple[Any, ...], _CacheEntry] = {}
        self._cache_lock = asyncio.Lock()

    @property
    def source(self) -> dict[str, Any]:
        return {
            "kind": "official_comfy_registry",
            "api_base_url": self.base_url,
            "website_url": self.website_url,
            "catalog_scope": "server_side_full_registry_search",
            "retrieved_at": None,
            "served_at": _utc_now(),
        }

    async def _cache_get(self, key: tuple[Any, ...]) -> _CacheEntry | None:
        async with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expires_at:
                self._cache.pop(key, None)
                return None
            return entry

    async def _cache_set(self, key: tuple[Any, ...], value: Any) -> _CacheEntry:
        now = time.monotonic()
        entry = _CacheEntry(
            value=copy.deepcopy(value),
            stored_at=now,
            expires_at=now + self.cache_ttl,
            fetched_at=_utc_now(),
        )
        async with self._cache_lock:
            self._cache[key] = entry
        return entry

    async def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        refresh: bool,
        expect_json: bool,
    ) -> tuple[Any, dict[str, Any]]:
        parsed = urlsplit(url)
        allowed_host = "api.comfy.org" if expect_json else "registry.comfy.org"
        if parsed.scheme != "https" or parsed.hostname != allowed_host or parsed.port is not None:
            raise ComfyRegistryError("Refused a request outside the fixed official Comfy hosts")
        normalized_params = tuple(
            sorted((str(key), str(value)) for key, value in (params or {}).items())
        )
        key = ("json" if expect_json else "text", url, normalized_params)
        if not refresh:
            entry = await self._cache_get(key)
            if entry is not None:
                return copy.deepcopy(entry.value), {
                    "state": "hit",
                    "fetched_at": entry.fetched_at,
                    "retries": 0,
                    "rate_limit": None,
                }

        rate_limit_diagnostics: dict[str, Any] | None = None
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                headers={"User-Agent": "ComfyUI-FL-MCP/registry-discovery"},
            ) as client:
                retries = 0
                for attempt in range(2):
                    response = await client.get(url, params=params)
                    if response.status_code != 429:
                        break
                    retry_after = response.headers.get("Retry-After", "").strip()
                    delay, retry_after_kind = _retry_after_seconds(retry_after)
                    if attempt > 0:
                        if rate_limit_diagnostics is None:
                            rate_limit_diagnostics = {
                                "encountered": True,
                                "retry_attempted": True,
                            }
                        rate_limit_diagnostics.update(
                            {
                                "outcome": "retry_exhausted",
                                "final_retry_after_raw": retry_after or None,
                                "final_retry_after_kind": retry_after_kind,
                            }
                        )
                        break
                    rate_limit_diagnostics = {
                        "encountered": True,
                        "retry_after_raw": retry_after or None,
                        "retry_after_kind": retry_after_kind,
                        "retry_after_seconds": delay,
                        "retry_cap_seconds": MAX_429_RETRY_DELAY,
                        "retry_attempted": False,
                    }
                    if delay is None:
                        rate_limit_diagnostics.update(
                            {
                                "outcome": "retry_not_attempted",
                                "retry_skipped_reason": "missing_or_invalid_retry_after",
                            }
                        )
                        break
                    if delay > MAX_429_RETRY_DELAY:
                        rate_limit_diagnostics.update(
                            {
                                "outcome": "retry_not_attempted",
                                "retry_skipped_reason": "delay_exceeds_cap",
                            }
                        )
                        break
                    rate_limit_diagnostics.update(
                        {
                            "retry_attempted": True,
                            "outcome": "retrying",
                        }
                    )
                    retries = 1
                    await asyncio.sleep(delay)
        except httpx.HTTPError as exc:
            raise ComfyRegistryHTTPError("Official Comfy Registry is unreachable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ComfyRegistryHTTPError(
                f"Official Comfy Registry returned HTTP {response.status_code}",
                status_code=response.status_code,
                diagnostics=rate_limit_diagnostics,
            )
        if rate_limit_diagnostics is not None:
            rate_limit_diagnostics["outcome"] = "recovered_after_retry"
        try:
            value = response.json() if expect_json else response.text
        except (ValueError, TypeError) as exc:
            raise ComfyRegistryHTTPError("Official Comfy Registry returned invalid JSON") from exc
        entry = await self._cache_set(key, value)
        return copy.deepcopy(value), {
            "state": "refreshed" if refresh else "miss",
            "fetched_at": entry.fetched_at,
            "retries": retries,
            "rate_limit": rate_limit_diagnostics,
        }

    async def _get_json(
        self, path: str, *, params: dict[str, Any] | None = None, refresh: bool = False
    ) -> tuple[Any, dict[str, Any]]:
        if not path.startswith("/") or path.startswith("//"):
            raise ComfyRegistryError("Registry API path must be relative to the official API")
        return await self._request(
            f"{self.base_url}{path}",
            params=params,
            refresh=refresh,
            expect_json=True,
        )

    def _registry_url(self, package_id: str) -> str:
        valid_id = _valid_package_id(package_id)
        if valid_id is None:
            raise ValueError("Invalid Comfy Registry package ID")
        url = f"{self.website_url}/nodes/{quote(valid_id, safe='._-')}"
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "registry.comfy.org":
            raise ValueError("Invalid official Registry URL")
        return url

    def _link_diagnostics(
        self,
        *,
        package_id: Any = None,
        github_url: str | None = None,
        repository_url: str | None = None,
        reason: str,
    ) -> dict[str, Any]:
        valid_id = _valid_package_id(package_id)
        registry_url = self._registry_url(valid_id) if valid_id is not None else None
        normalized_github = normalize_github_repository_url(github_url)
        normalized_repository = validate_repository_url(repository_url)
        return {
            "registry": {
                "state": "validated" if registry_url else "unavailable",
                "url": registry_url,
                "host": "registry.comfy.org" if registry_url else None,
                "reason": None if registry_url else reason,
            },
            "github": {
                "state": "validated" if normalized_github else "unavailable",
                "url": normalized_github,
                "host": "github.com" if normalized_github else None,
                "reason": None if normalized_github else reason,
            },
            "repository": {
                "state": "validated" if normalized_repository else "unavailable",
                "url": normalized_repository,
                "host": urlsplit(normalized_repository).hostname
                if normalized_repository
                else None,
                "reason": None if normalized_repository else reason,
            },
        }

    @staticmethod
    def _accepted_links(
        *,
        registry_url: str,
        github_url: str,
        repository_url: str | None,
        repository_source: str,
    ) -> dict[str, Any]:
        effective_repository_url = repository_url or github_url
        return {
            "registry": {
                "url": registry_url,
                "host": "registry.comfy.org",
                "validated": True,
                "source": "validated_package_id",
            },
            "github": {
                "url": github_url,
                "host": "github.com",
                "validated": True,
                "source": repository_source,
            },
            "repository": {
                "url": effective_repository_url,
                "host": urlsplit(effective_repository_url).hostname,
                "validated": True,
                "source": repository_source,
            },
        }

    async def _resolve_repository(
        self,
        package_id: str,
        *,
        search_item: dict[str, Any] | None,
        detail: dict[str, Any] | None,
        refresh: bool,
    ) -> tuple[str | None, str | None, str | None, dict[str, Any] | None, list[dict[str, Any]]]:
        cache_events: list[dict[str, Any]] = []
        repository_url: str | None = None
        if search_item is not None:
            value = search_item.get("repository")
            repository_url = validate_repository_url(value)
            github_url = normalize_github_repository_url(value)
            if github_url:
                return github_url, repository_url or github_url, "search", detail, cache_events

        resolved_detail = detail
        if resolved_detail is None:
            try:
                payload, cache = await self._get_json(
                    f"/nodes/{quote(package_id, safe='._-')}", refresh=refresh
                )
                cache_events.append(cache)
                payload_id = _valid_package_id(payload.get("id")) if isinstance(payload, dict) else None
                if payload_id is not None and payload_id.casefold() == package_id.casefold():
                    resolved_detail = payload
            except ComfyRegistryError:
                resolved_detail = None
        if resolved_detail is not None:
            value = resolved_detail.get("repository")
            repository_url = repository_url or validate_repository_url(value)
            github_url = normalize_github_repository_url(value)
            if github_url:
                return github_url, repository_url or github_url, "detail", resolved_detail, cache_events

        return None, repository_url, None, resolved_detail, cache_events

    @staticmethod
    def _installed_ids(installed_pack_ids: Any) -> _InstalledIdentities | None:
        if installed_pack_ids is None:
            return None
        if isinstance(installed_pack_ids, dict):
            raw_package_ids = installed_pack_ids.get("package_ids", [])
            raw_repository_urls = installed_pack_ids.get("repository_urls", [])
            identity_complete = installed_pack_ids.get("identity_complete") is True
        else:
            raw_package_ids = installed_pack_ids
            raw_repository_urls = []
            identity_complete = True

        if isinstance(raw_package_ids, str):
            package_values = [raw_package_ids]
        else:
            try:
                package_values = list(raw_package_ids)
            except TypeError:
                package_values = []
        if isinstance(raw_repository_urls, str):
            repository_values = [raw_repository_urls]
        else:
            try:
                repository_values = list(raw_repository_urls)
            except TypeError:
                repository_values = []
        package_ids = {
            valid.casefold()
            for item in package_values
            if (valid := _valid_package_id(item)) is not None
        }
        repository_urls = {
            normalized.casefold()
            for item in repository_values
            if (normalized := normalize_github_repository_url(item)) is not None
        }
        return _InstalledIdentities(
            package_ids=frozenset(package_ids),
            repository_urls=frozenset(repository_urls),
            identity_complete=identity_complete,
        )

    def _normalize_package(
        self,
        item: dict[str, Any],
        *,
        github_url: str,
        repository_url: str | None,
        repository_source: str,
        installed_ids: _InstalledIdentities | None,
        query: str = "",
        comfy_node_search: str | None = None,
    ) -> dict[str, Any]:
        package_id = _valid_package_id(item.get("id"))
        if package_id is None:
            raise ValueError("Registry package has an invalid ID")
        latest = _latest_version(item)
        node_status = _status_name(item.get("status"))
        version_status = _status_name(latest.get("status"))
        deprecated = bool(latest.get("deprecated"))
        relevance, match_reasons = _functional_relevance(item, query, comfy_node_search)
        downloads = _safe_int(item.get("downloads"))
        stars = _safe_int(item.get("github_stars"))
        rating = _safe_float(item.get("rating"))
        publisher = item.get("publisher") if isinstance(item.get("publisher"), dict) else {}
        installation_match: str | None = None
        if installed_ids is None:
            installed = None
            identity_complete: bool | None = None
        elif package_id.casefold() in installed_ids.package_ids:
            installed = True
            installation_match = "package_id"
            identity_complete = installed_ids.identity_complete
        elif github_url.casefold() in installed_ids.repository_urls:
            installed = True
            installation_match = "repository_url"
            identity_complete = installed_ids.identity_complete
        elif installed_ids.identity_complete:
            installed = False
            identity_complete = True
        else:
            installed = None
            identity_complete = False
        if installed is None:
            installation_state = "unknown"
        else:
            installation_state = "installed" if installed else "not_installed"
        official_search_ranking = _safe_float(item.get("search_ranking"))
        registry_url = self._registry_url(package_id)
        active = node_status == "active" and version_status in {"active", "unknown"} and not deprecated
        security = _security_metadata(item)
        compatibility = _compatibility(item)
        nonblocked = security["state"] != "blocked"
        compatibility_rank = {
            "compatible": 2,
            "unknown": 1,
            "incompatible": 0,
        }[compatibility["macos_apple_silicon"]]
        quality_score = _quality_score(item)
        return {
            "package_id": package_id,
            "name": _bounded_text(item.get("name"), MAX_SHORT_TEXT) or package_id,
            "description": _bounded_text(item.get("description"), MAX_DESCRIPTION_TEXT),
            "category": _bounded_text(item.get("category"), MAX_SHORT_TEXT),
            "author": _bounded_text(item.get("author"), MAX_SHORT_TEXT),
            "publisher_id": _bounded_text(publisher.get("id"), MAX_SHORT_TEXT),
            "tags": _string_list(item.get("tags")),
            "registry_url": registry_url,
            "github_url": github_url,
            "repository_url": repository_url,
            "links": self._accepted_links(
                registry_url=registry_url,
                github_url=github_url,
                repository_url=repository_url,
                repository_source=repository_source,
            ),
            "repository_resolution": {
                "source": repository_source,
                "github_host_validated": True,
            },
            "latest_version": {
                "version": _bounded_text(latest.get("version"), MAX_SHORT_TEXT),
                "status": version_status,
                "deprecated": deprecated,
                "created_at": _bounded_text(latest.get("createdAt"), MAX_SHORT_TEXT),
            },
            "active": active,
            "deprecated": deprecated,
            "security": security,
            "compatibility": compatibility,
            "installed": installed,
            "installation_state": installation_state,
            "installation_match": installation_match,
            "installation_identity_complete": identity_complete,
            "quality": {
                "downloads": downloads,
                "github_stars": stars,
                "rating": rating,
                "official_search_ranking": official_search_ranking,
                "score": quality_score,
            },
            "official_search_ranking": official_search_ranking,
            "relevance_score": relevance,
            "match_reasons": match_reasons,
            "ranking_signals": {
                "functional_relevance": relevance,
                "active": active,
                "nonblocked": nonblocked,
                "macos_apple_silicon": compatibility["macos_apple_silicon"],
                "macos_apple_silicon_rank": compatibility_rank,
                "quality_score": quality_score,
            },
            "class_metadata_state": "not_requested",
        }

    @staticmethod
    def _cache_state(events: list[dict[str, Any]], refresh: bool) -> str:
        if refresh:
            return "refreshed"
        states = {event.get("state") for event in events}
        if states == {"hit"}:
            return "hit"
        if "hit" in states and len(states) > 1:
            return "mixed"
        return "miss"

    @staticmethod
    def _rate_limit_state(events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in reversed(events):
            diagnostics = event.get("rate_limit")
            if isinstance(diagnostics, dict):
                return copy.deepcopy(diagnostics)
        return {"encountered": False}

    @staticmethod
    def _retrieved_at(events: list[dict[str, Any]]) -> str | None:
        timestamps = [
            event["fetched_at"]
            for event in events
            if isinstance(event.get("fetched_at"), str) and event["fetched_at"]
        ]
        return max(timestamps) if timestamps else None

    def _source_for_events(
        self,
        events: list[dict[str, Any]],
        *,
        refresh: bool,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            **self.source,
            "retrieved_at": self._retrieved_at(events),
            "cache_state": self._cache_state(events, refresh),
            "rate_limit": self._rate_limit_state(events),
            **extra,
        }

    @staticmethod
    def _error(
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if status_code is not None:
            error["status_code"] = status_code
        if diagnostics is not None:
            error["diagnostics"] = copy.deepcopy(diagnostics)
        return error

    async def search_packages(
        self,
        query: str,
        *,
        comfy_node_search: str | None = None,
        supported_os: str | None = None,
        supported_accelerator: str | None = None,
        max_results: int = 10,
        refresh: bool = False,
        installed_pack_ids: Any = None,
        include_installed: bool = False,
    ) -> dict[str, Any]:
        """Search all registered packages through the official server-side index."""
        if not isinstance(query, str):
            return {
                "ok": False,
                "results": [],
                "skipped_results": [],
                "error": self._error("invalid_query", "query must be a string"),
                "link_diagnostics": self._link_diagnostics(
                    reason="No package was selected because the query is invalid."
                ),
                "source": self.source,
            }
        if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results <= 0:
            return {
                "ok": False,
                "results": [],
                "skipped_results": [],
                "error": self._error("invalid_limit", "max_results must be a positive integer"),
                "link_diagnostics": self._link_diagnostics(
                    reason="No package was selected because the result limit is invalid."
                ),
                "source": self.source,
            }
        if not isinstance(include_installed, bool):
            return {
                "ok": False,
                "results": [],
                "skipped_results": [],
                "error": self._error("invalid_filter", "include_installed must be a boolean"),
                "link_diagnostics": self._link_diagnostics(
                    reason="No package was selected because the filter is invalid."
                ),
                "source": self.source,
            }
        registry_query = normalize_registry_search_query(query)
        has_class_query = isinstance(comfy_node_search, str) and bool(comfy_node_search.strip())
        query_mode = "registry_browse" if not registry_query and not has_class_query else "capability_search"
        effective_limit = min(max_results, MAX_SEARCH_RESULTS)
        candidate_limit = min(MAX_SEARCH_CANDIDATES, max(10, effective_limit * 2))
        params: dict[str, Any] = {
            "search": registry_query,
            "page": 1,
            "limit": candidate_limit,
        }
        if isinstance(comfy_node_search, str) and comfy_node_search.strip():
            params["comfy_node_search"] = comfy_node_search.strip()
        if isinstance(supported_os, str) and supported_os.strip():
            params["supported_os"] = supported_os.strip()
        if isinstance(supported_accelerator, str) and supported_accelerator.strip():
            params["supported_accelerator"] = supported_accelerator.strip()

        events: list[dict[str, Any]] = []
        try:
            payload, cache = await self._get_json(
                "/nodes/search", params=params, refresh=refresh
            )
            events.append(cache)
        except ComfyRegistryHTTPError as exc:
            error_code = (
                "registry_rate_limited" if exc.status_code == 429 else "registry_request_failed"
            )
            return {
                "ok": False,
                "query": query,
                "results": [],
                "skipped_results": [],
                "error": self._error(
                    error_code,
                    str(exc),
                    status_code=exc.status_code,
                    diagnostics=exc.diagnostics,
                ),
                "link_diagnostics": self._link_diagnostics(
                    reason="Registry search failed before package links could be validated."
                ),
                "source": self._source_for_events(events, refresh=refresh),
            }
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
            return {
                "ok": False,
                "query": query,
                "results": [],
                "skipped_results": [],
                "error": self._error(
                    "invalid_registry_response", "Official Registry search response is malformed"
                ),
                "link_diagnostics": self._link_diagnostics(
                    reason="The malformed Registry response did not provide a package to validate."
                ),
                "source": self._source_for_events(events, refresh=refresh),
            }

        installed_ids = self._installed_ids(installed_pack_ids)
        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        detail_fallbacks_used = 0
        for raw_item in payload["nodes"][:candidate_limit]:
            if not isinstance(raw_item, dict):
                skipped.append(
                    {
                        "package_id": None,
                        "reason": "invalid_registry_result",
                        "link_diagnostics": self._link_diagnostics(
                            reason="The Registry result is not a package object."
                        ),
                    }
                )
                continue
            package_id = _valid_package_id(raw_item.get("id"))
            if package_id is None:
                skipped.append(
                    {
                        "package_id": None,
                        "reason": "invalid_package_id",
                        "value": str(raw_item.get("id") or ""),
                        "link_diagnostics": self._link_diagnostics(
                            reason="The Registry package ID is invalid."
                        ),
                    }
                )
                continue
            search_github_url = normalize_github_repository_url(raw_item.get("repository"))
            if search_github_url is None:
                if detail_fallbacks_used >= MAX_SEARCH_DETAIL_FALLBACKS:
                    repository_url = validate_repository_url(raw_item.get("repository"))
                    skipped.append(
                        {
                            "package_id": package_id,
                            "registry_url": self._registry_url(package_id),
                            "reason": "repository_resolution_budget_exhausted",
                            "link_diagnostics": self._link_diagnostics(
                                package_id=package_id,
                                repository_url=repository_url,
                                reason="The bounded detail fallback budget was exhausted.",
                            ),
                        }
                    )
                    continue
                detail_fallbacks_used += 1
            github_url, repository_url, repository_source, _detail, repo_events = (
                await self._resolve_repository(
                    package_id,
                    search_item=raw_item,
                    detail=None,
                    refresh=refresh,
                )
            )
            events.extend(repo_events)
            if github_url is None or repository_source is None:
                skipped.append(
                    {
                        "package_id": package_id,
                        "registry_url": self._registry_url(package_id),
                        "reason": "missing_valid_github_repository",
                        "link_diagnostics": self._link_diagnostics(
                            package_id=package_id,
                            repository_url=repository_url,
                            reason="No exact-host github.com repository was verified.",
                        ),
                    }
                )
                continue
            try:
                normalized = self._normalize_package(
                    raw_item,
                    github_url=github_url,
                    repository_url=repository_url,
                    repository_source=repository_source,
                    installed_ids=installed_ids,
                    query=registry_query,
                    comfy_node_search=comfy_node_search,
                )
                if normalized["security"]["state"] == "blocked":
                    skipped.append(
                        {
                            "package_id": package_id,
                            "registry_url": normalized["registry_url"],
                            "reason": "security_blocked",
                            "security": normalized["security"],
                            "link_diagnostics": self._link_diagnostics(
                                package_id=package_id,
                                github_url=normalized["github_url"],
                                repository_url=normalized["repository_url"]
                                or normalized["github_url"],
                                reason="The Registry marks this package as blocked.",
                            ),
                        }
                    )
                    continue
                if not include_installed and normalized["installed"] is True:
                    skipped.append(
                        {
                            "package_id": package_id,
                            "registry_url": normalized["registry_url"],
                            "reason": "already_installed",
                            "link_diagnostics": self._link_diagnostics(
                                package_id=package_id,
                                github_url=normalized["github_url"],
                                repository_url=normalized["repository_url"]
                                or normalized["github_url"],
                                reason="The package was skipped because it is already installed.",
                            ),
                        }
                    )
                    continue
                candidates.append(normalized)
            except ValueError:
                skipped.append(
                    {
                        "package_id": package_id,
                        "reason": "invalid_registry_result",
                        "link_diagnostics": self._link_diagnostics(
                            package_id=package_id,
                            github_url=github_url,
                            repository_url=repository_url,
                            reason="Package metadata failed deterministic validation.",
                        ),
                    }
                )

        candidates.sort(
            key=lambda item: (
                -item["relevance_score"],
                -int(item["ranking_signals"]["active"]),
                -int(item["ranking_signals"]["nonblocked"]),
                -item["ranking_signals"]["macos_apple_silicon_rank"],
                -item["quality"]["score"],
                item["package_id"].casefold(),
                item["package_id"],
            )
        )
        results = candidates[:effective_limit]
        for index, candidate in enumerate(results, start=1):
            candidate["rank"] = index
        skipped.sort(key=lambda item: (str(item.get("package_id") or "").casefold(), item["reason"]))
        return {
            "ok": True,
            "query": query,
            "registry_query": registry_query,
            "query_mode": query_mode,
            "comfy_node_search": comfy_node_search,
            "filters": {
                "supported_os": supported_os,
                "supported_accelerator": supported_accelerator,
                "include_installed": include_installed,
            },
            "results": results,
            "result_count": len(results),
            "skipped_results": skipped,
            "registry_total": _safe_int(payload.get("total")),
            "limits": {
                "requested_results": max_results,
                "effective_results": effective_limit,
                "candidate_pool": candidate_limit,
                "pages_fetched": 1,
                "detail_fallback_budget": MAX_SEARCH_DETAIL_FALLBACKS,
                "detail_fallbacks_used": detail_fallbacks_used,
            },
            "source": self._source_for_events(
                events,
                refresh=refresh,
                endpoint="/nodes/search",
                original_query=query,
                registry_query=registry_query,
                query_mode=query_mode,
            ),
        }

    async def _class_metadata(
        self,
        package_id: str,
        version: str | None,
        *,
        refresh: bool,
        max_classes: int,
    ) -> tuple[list[dict[str, Any]], str, str | None, list[dict[str, Any]], int]:
        if version is None:
            return [], "missing", "Latest version is not published.", [], 0
        page_size = min(CLASS_PAGE_SIZE, max_classes)
        max_pages = min(MAX_CLASS_PAGES, max(1, math.ceil(max_classes / page_size)))
        classes: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        declared_pages: int | None = None
        pages_fetched = 0
        for page in range(1, max_pages + 1):
            try:
                payload, cache = await self._get_json(
                    f"/nodes/{quote(package_id, safe='._-')}/versions/{quote(version, safe='._+-')}/comfy-nodes",
                    params={"page": page, "limit": page_size},
                    refresh=refresh,
                )
                events.append(cache)
                pages_fetched += 1
            except ComfyRegistryHTTPError as exc:
                if exc.status_code == 404 and not classes:
                    return [], "missing", "No class metadata is published for the latest version.", events, pages_fetched
                return classes, "partial", str(exc), events, pages_fetched
            if not isinstance(payload, dict):
                return classes, "partial", "Class metadata response is malformed.", events, pages_fetched
            raw_classes = payload.get("comfy_nodes")
            raw_page_count = (
                payload.get("totalNumberOfPages")
                if "totalNumberOfPages" in payload
                else payload.get("total_pages", payload.get("totalPages"))
            )
            if raw_classes is None and raw_page_count in {0, "0"}:
                return (
                    [],
                    "missing",
                    "No class metadata is published for the latest version.",
                    events,
                    pages_fetched,
                )
            if not isinstance(raw_classes, list):
                return classes, "partial", "Class metadata response is malformed.", events, pages_fetched
            for raw_class in raw_classes:
                if isinstance(raw_class, dict):
                    sanitized_class = _bounded_metadata(raw_class)
                    if isinstance(sanitized_class, dict):
                        classes.append(sanitized_class)
                    if len(classes) >= max_classes:
                        break
            try:
                declared_pages = max(
                    1,
                    int(
                        raw_page_count
                        or 1
                    ),
                )
            except (TypeError, ValueError):
                return classes, "partial", "Class metadata pagination is malformed.", events, pages_fetched
            if len(classes) >= max_classes or page >= declared_pages:
                break

        classes.sort(
            key=lambda item: (
                str(item.get("comfy_node_name") or item.get("comfy_node_id") or "").casefold(),
                str(item.get("comfy_node_name") or item.get("comfy_node_id") or ""),
            )
        )
        if not classes:
            return [], "missing", "No class metadata is published for the latest version.", events, pages_fetched
        if declared_pages is not None and pages_fetched < declared_pages:
            return classes, "partial", f"Class metadata was capped at {max_classes} classes.", events, pages_fetched
        return classes, "available", None, events, pages_fetched

    async def get_package(
        self,
        package_id: str,
        *,
        refresh: bool = False,
        installed_pack_ids: Any = None,
        max_classes: int = MAX_CLASS_METADATA,
    ) -> dict[str, Any]:
        """Return a validated package and bounded latest-version class metadata."""
        valid_id = _valid_package_id(package_id)
        if valid_id is None:
            return {
                "ok": False,
                "error": self._error("invalid_package_id", "Invalid Comfy Registry package ID"),
                "link_diagnostics": self._link_diagnostics(
                    package_id=package_id,
                    reason="The package ID cannot produce a validated Registry link.",
                ),
                "source": self.source,
            }
        if isinstance(max_classes, bool) or not isinstance(max_classes, int) or max_classes <= 0:
            return {
                "ok": False,
                "package_id": valid_id,
                "error": self._error("invalid_limit", "max_classes must be a positive integer"),
                "link_diagnostics": self._link_diagnostics(
                    package_id=valid_id,
                    reason="Package links were not fetched because the class limit is invalid.",
                ),
                "source": self.source,
            }
        effective_max_classes = min(max_classes, MAX_CLASS_METADATA)
        events: list[dict[str, Any]] = []
        try:
            payload, cache = await self._get_json(
                f"/nodes/{quote(valid_id, safe='._-')}", refresh=refresh
            )
            events.append(cache)
        except ComfyRegistryHTTPError as exc:
            if exc.status_code == 404:
                code = "package_not_found"
            elif exc.status_code == 429:
                code = "registry_rate_limited"
            else:
                code = "registry_request_failed"
            return {
                "ok": False,
                "package_id": valid_id,
                "error": self._error(
                    code,
                    str(exc),
                    status_code=exc.status_code,
                    diagnostics=exc.diagnostics,
                ),
                "link_diagnostics": self._link_diagnostics(
                    package_id=valid_id,
                    reason="Package detail retrieval failed before GitHub validation.",
                ),
                "source": self._source_for_events(events, refresh=refresh),
            }
        payload_id = _valid_package_id(payload.get("id")) if isinstance(payload, dict) else None
        if payload_id is None or payload_id.casefold() != valid_id.casefold():
            return {
                "ok": False,
                "package_id": valid_id,
                "error": self._error(
                    "invalid_registry_response", "Registry package detail is malformed or mismatched"
                ),
                "link_diagnostics": self._link_diagnostics(
                    package_id=valid_id,
                    reason="Malformed package detail prevented GitHub validation.",
                ),
                "source": self._source_for_events(events, refresh=refresh),
            }
        valid_id = payload_id

        github_url, repository_url, repository_source, _detail, repo_events = (
            await self._resolve_repository(
                valid_id,
                search_item=None,
                detail=payload,
                refresh=refresh,
            )
        )
        events.extend(repo_events)
        if github_url is None or repository_source is None:
            return {
                "ok": False,
                "package_id": valid_id,
                "registry_url": self._registry_url(valid_id),
                "repository_url": repository_url,
                "error": self._error(
                    "missing_valid_github_repository",
                    "The official Registry package does not expose a verifiable github.com repository",
                ),
                "link_diagnostics": self._link_diagnostics(
                    package_id=valid_id,
                    repository_url=repository_url,
                    reason="No exact-host github.com repository was verified.",
                ),
                "source": self._source_for_events(events, refresh=refresh),
            }

        package = self._normalize_package(
            payload,
            github_url=github_url,
            repository_url=repository_url,
            repository_source=repository_source,
            installed_ids=self._installed_ids(installed_pack_ids),
        )
        version = _valid_version(_latest_version(payload).get("version"))
        classes, class_state, class_reason, class_events, pages_fetched = (
            await self._class_metadata(
                valid_id,
                version,
                refresh=refresh,
                max_classes=effective_max_classes,
            )
        )
        events.extend(class_events)
        package["class_metadata_state"] = class_state
        package["class_metadata_reason"] = class_reason
        package["comfy_nodes"] = classes
        package["class_count"] = len(classes)
        return {
            "ok": True,
            "package": package,
            "limits": {
                "requested_classes": max_classes,
                "effective_classes": effective_max_classes,
                "class_pages_fetched": pages_fetched,
                "max_class_pages": max(
                    1,
                    min(
                        MAX_CLASS_PAGES,
                        math.ceil(
                            effective_max_classes
                            / min(CLASS_PAGE_SIZE, effective_max_classes)
                        ),
                    ),
                ),
            },
            "source": self._source_for_events(
                events,
                refresh=refresh,
                endpoint=f"/nodes/{valid_id}",
            ),
        }


__all__ = [
    "ComfyRegistryClient",
    "ComfyRegistryError",
    "ComfyRegistryHTTPError",
    "normalize_github_repository_url",
    "normalize_registry_search_query",
    "validate_repository_url",
]
