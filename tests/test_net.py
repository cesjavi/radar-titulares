"""Seguridad de red (SSRF), reintentos, bloqueos, límites y robots.txt."""

from __future__ import annotations

import pytest

from radar.net import (
    BlockedError,
    DomainRateLimiter,
    FetchError,
    UnsafeURLError,
    is_public_ip,
    validate_url,
)
from tests.helpers import PUBLIC_IP, Routes, make_fetcher

ALLOWED = ["infobae.com"]


def public(host, port):
    return [PUBLIC_IP]


@pytest.mark.parametrize("url", [
    "ftp://www.infobae.com/x",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "http://localhost/x",
    "http://127.0.0.1/x",
    "http://[::1]/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://10.0.0.5/x",
    "https://user:pass@www.infobae.com/x",
    "https://www.infobae.com.evil.example/x",
    "https://evil.example/?u=https://www.infobae.com",
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url, ALLOWED, public)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1",
                                "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fe80::1",
                                "fc00::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_private_or_special_ips_are_not_public(ip):
    assert not is_public_ip(ip)


def test_dns_resolving_to_private_ip_is_blocked():
    with pytest.raises(UnsafeURLError, match="no pública"):
        validate_url("https://www.infobae.com/x", ALLOWED, lambda h, p: ["10.0.0.7"])
    with pytest.raises(UnsafeURLError):
        validate_url("https://www.infobae.com/x", ALLOWED, lambda h, p: [PUBLIC_IP, "127.0.0.1"])
    assert validate_url("https://www.infobae.com/x", ALLOWED, public)


def test_each_redirect_is_validated():
    routes = Routes({
        "https://www.infobae.com/a": (302, b"", {"location": "https://www.infobae.com/b"}),
        "https://www.infobae.com/b": (301, b"", {"location": "http://169.254.169.254/meta"}),
        "https://www.infobae.com/c": (302, b"", {"location": "https://otro.example/"}),
        "https://www.infobae.com/ok": (302, b"", {"location": "/final"}),
        "https://www.infobae.com/final": (200, b"hola"),
    })
    with make_fetcher(routes) as f:
        with pytest.raises(UnsafeURLError):
            f.get("https://www.infobae.com/a", ALLOWED)
        with pytest.raises(UnsafeURLError, match="Dominio no permitido"):
            f.get("https://www.infobae.com/c", ALLOWED)
        resp = f.get("https://www.infobae.com/ok", ALLOWED)
    assert resp.url == "https://www.infobae.com/final"
    assert resp.redirects == ["https://www.infobae.com/final"]
    assert "169.254.169.254" not in " ".join(routes.urls())  # nunca se contactó


def test_redirect_to_host_resolving_private_is_blocked():
    routes = Routes({"https://www.infobae.com/a": (302, b"", {"location": "https://interno.infobae.com/"})})
    resolver = lambda h, p: ["192.168.0.10"] if h.startswith("interno") else [PUBLIC_IP]  # noqa: E731
    with make_fetcher(routes, resolver=resolver) as f, pytest.raises(UnsafeURLError):
        f.get("https://www.infobae.com/a", ALLOWED)


def test_too_many_redirects():
    routes = Routes({"https://www.infobae.com/loop": (302, b"", {"location": "/loop"})})
    with make_fetcher(routes) as f, pytest.raises(FetchError, match="redirecciones"):
        f.get("https://www.infobae.com/loop", ALLOWED)


def test_retries_with_limit_then_success():
    calls = {"n": 0}

    def flaky(request):
        import httpx
        calls["n"] += 1
        return httpx.Response(503 if calls["n"] < 3 else 200, content=b"ok")

    with make_fetcher(Routes({"https://www.infobae.com/x": flaky}), retries=2) as f:
        resp = f.get("https://www.infobae.com/x", ALLOWED)
    assert resp.status_code == 200 and resp.attempts == 3


def test_retries_exhausted():
    with make_fetcher(Routes({"https://www.infobae.com/x": (500, b"")}), retries=1) as f:
        with pytest.raises(FetchError) as exc:
            f.get("https://www.infobae.com/x", ALLOWED)
    assert exc.value.status_code == 500 and exc.value.attempts == 2


def test_timeouts_are_retried_and_reported():
    import httpx

    def timeout(request):
        raise httpx.ReadTimeout("lento", request=request)

    routes = Routes({"https://www.infobae.com/x": timeout})
    with make_fetcher(routes, retries=2) as f, pytest.raises(FetchError, match="ReadTimeout"):
        f.get("https://www.infobae.com/x", ALLOWED)
    assert len(routes.requests) == 3


def test_forbidden_is_blocked_without_retry():
    routes = Routes({"https://www.infobae.com/x": (403, b"no")})
    with make_fetcher(routes) as f, pytest.raises(BlockedError) as exc:
        f.get("https://www.infobae.com/x", ALLOWED)
    assert exc.value.status_code == 403
    assert len(routes.requests) == 1


def test_response_size_limit():
    routes = Routes({"https://www.infobae.com/x": (200, b"x" * 5000)})
    with make_fetcher(routes, max_bytes=1000) as f, pytest.raises(FetchError, match="mayor"):
        f.get("https://www.infobae.com/x", ALLOWED)


def test_robots_txt_is_respected_and_sets_crawl_delay():
    routes = Routes({
        "https://www.infobae.com/robots.txt": (200, b"User-agent: *\nDisallow: /buscador\nCrawl-delay: 7\n"),
        "https://www.infobae.com/ok": (200, b"ok"),
    })
    with make_fetcher(routes, robots=True) as f:
        assert f.get("https://www.infobae.com/ok", ALLOWED).status_code == 200
        with pytest.raises(BlockedError, match="robots"):
            f.get("https://www.infobae.com/buscador?q=x", ALLOWED)
        assert f.rate.per_host["www.infobae.com"] == 7
    assert routes.urls().count("https://www.infobae.com/robots.txt") == 1  # en caché


def test_domain_rate_limiter_spaces_requests():
    slept = []
    limiter = DomainRateLimiter(2.0, sleep=slept.append)
    limiter.wait("a.example")
    limiter.wait("a.example")
    limiter.wait("b.example")  # otro dominio: sin espera
    assert len(slept) == 1 and 1.9 <= slept[0] <= 2.0
