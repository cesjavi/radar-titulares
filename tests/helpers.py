"""Utilidades de prueba: fetcher con transporte simulado y DNS falso."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from radar.net import DomainRateLimiter, SafeFetcher

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_IP = "93.184.216.34"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Routes:
    """Transporte simulado: URL → (status, cuerpo, encabezados) o función(request)."""

    def __init__(self, routes: dict, delay: float = 0.0):
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            route = self.routes.get(str(request.url))
            if route is None:
                return httpx.Response(404, content=b"no encontrado")
            if callable(route):
                return route(request)
            status, body, *rest = route
            headers = rest[0] if rest else {}
            if isinstance(body, str):
                body = body.encode()
            return httpx.Response(status, content=body, headers=headers)
        finally:
            with self._lock:
                self.active -= 1

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def make_fetcher(routes: Routes, resolver=None, robots: bool = False, retries: int = 2,
                 max_bytes: int = 10 * 1024 * 1024) -> SafeFetcher:
    return SafeFetcher(
        user_agent="RadarTest/1.0",
        transport=httpx.MockTransport(routes),
        resolver=resolver or (lambda host, port: [PUBLIC_IP]),
        rate_limiter=DomainRateLimiter(0),
        respect_robots=robots,
        retries=retries,
        backoff=0,
        max_bytes=max_bytes,
        sleep=lambda s: None,
    )
