"""SSRF-resistant URL validation and canonicalization for web research."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_ALLOWED_PORTS = frozenset({80, 443})
TRACKING_PARAMETERS = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "ref_src",
    }
)
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
    }
)

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class WebUrlError(ValueError):
    """Base class for invalid or unsafe web targets."""


class UnsafeWebUrl(WebUrlError):
    """Raised when a URL could reach a non-public or otherwise unsafe target."""


class WebResolutionError(WebUrlError):
    """Raised when a web hostname cannot be resolved safely."""


def _normalize_hostname(hostname: str) -> str:
    host = hostname.rstrip(".").lower()
    if not host:
        raise WebUrlError("URL must include a hostname")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebUrlError("URL hostname is not valid IDNA") from exc


def _is_tracking_parameter(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("utm_") or lowered in TRACKING_PARAMETERS


def canonicalize_web_url(
    url: str,
    *,
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
    strip_tracking: bool = True,
) -> str:
    """Normalize an HTTP(S) URL and reject unsafe syntax or ports."""

    candidate = url.strip()
    if not candidate:
        raise WebUrlError("URL is empty")

    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise WebUrlError("URL has an invalid hostname or port") from exc

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeWebUrl("Only http:// and https:// URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeWebUrl("URLs containing credentials are not allowed")

    hostname = _normalize_hostname(parts.hostname or "")
    effective_port = port or (443 if scheme == "https" else 80)
    if effective_port not in allowed_ports:
        raise UnsafeWebUrl(f"Port {effective_port} is not allowed")

    default_port = 443 if scheme == "https" else 80
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = display_host if effective_port == default_port else f"{display_host}:{effective_port}"
    path = parts.path or "/"

    query_items = parse_qsl(parts.query, keep_blank_values=True)
    if strip_tracking:
        query_items = [(key, value) for key, value in query_items if not _is_tracking_parameter(key)]
    query = urlencode(query_items, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def is_public_ip(value: str) -> bool:
    """Return whether an IPv4/IPv6 address is globally routable."""

    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global


async def _default_resolver(hostname: str, port: int) -> Sequence[str]:
    def resolve() -> list[str]:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return list({record[4][0] for record in records})

    return await asyncio.to_thread(resolve)


async def validate_public_web_url(
    url: str,
    *,
    resolver: Resolver | None = None,
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
) -> str:
    """Canonicalize a URL and ensure every resolved address is public."""

    normalized = canonicalize_web_url(url, allowed_ports=allowed_ports)
    parts = urlsplit(normalized)
    hostname = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)

    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise UnsafeWebUrl(f"Host {hostname!r} is not a public web target")

    try:
        direct_address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        direct_address = None

    if direct_address is not None:
        addresses = [str(direct_address)]
    else:
        try:
            addresses = list(await (resolver or _default_resolver)(hostname, port))
        except (OSError, socket.gaierror) as exc:
            raise WebResolutionError(f"Could not resolve {hostname!r}") from exc

    if not addresses:
        raise WebResolutionError(f"Could not resolve {hostname!r}")
    blocked = [address for address in addresses if not is_public_ip(address)]
    if blocked:
        raise UnsafeWebUrl(f"Host {hostname!r} resolved to a non-public address")
    return normalized
