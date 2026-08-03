import pytest
from web_security import (
    UnsafeWebUrl,
    WebUrlError,
    canonicalize_web_url,
    validate_public_web_url,
)


def test_canonicalize_removes_fragments_credentials_and_tracking_noise():
    url = canonicalize_web_url(
        " HTTPS://Example.COM:443/path?q=hello&utm_source=test&fbclid=123#section "
    )

    assert url == "https://example.com/path?q=hello"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.com/",
        "https://example.com:22/",
        "https:///missing-host",
    ],
)
def test_canonicalize_rejects_unsafe_url_syntax(url):
    with pytest.raises(WebUrlError):
        canonicalize_web_url(url)


@pytest.mark.asyncio
async def test_validate_public_url_accepts_public_dns_and_idna_normalizes():
    calls = []

    async def resolver(hostname, port):
        calls.append((hostname, port))
        return ["93.184.216.34"]

    result = await validate_public_web_url("https://BÜCHER.example/test", resolver=resolver)

    assert result == "https://xn--bcher-kva.example/test"
    assert calls == [("xn--bcher-kva.example", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://localhost/",
        "http://service.local/",
    ],
)
async def test_validate_public_url_rejects_local_and_metadata_targets(url):
    async def resolver(_hostname, _port):
        return ["127.0.0.1"]

    with pytest.raises(UnsafeWebUrl):
        await validate_public_web_url(url, resolver=resolver)


@pytest.mark.asyncio
async def test_validate_public_url_rejects_mixed_public_private_dns_answers():
    async def resolver(_hostname, _port):
        return ["93.184.216.34", "10.0.0.4"]

    with pytest.raises(UnsafeWebUrl):
        await validate_public_web_url("https://example.com/", resolver=resolver)
