import asyncio
import time

import httpx

from .errors import IntegrationError


class RateLimiter:
    """Простейший лимитер: не чаще rps запросов в секунду на клиента."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


async def request_json(client: httpx.AsyncClient, method: str, url: str, **kw) -> dict | list | None:
    """HTTP-запрос с разделением ошибок на временные и постоянные."""
    try:
        r = await client.request(method, url, **kw)
    except httpx.TransportError as e:
        raise IntegrationError(f"{method} {url}: сеть: {e!r}", retryable=True) from e
    if r.status_code == 429 or r.status_code >= 500:
        raise IntegrationError(f"{method} {url}: HTTP {r.status_code}", retryable=True)
    if r.status_code == 204 or not r.content:
        if r.is_success:
            return None
    if not r.is_success:
        raise IntegrationError(f"{method} {url}: HTTP {r.status_code}: {r.text[:300]}", retryable=False)
    return r.json()
