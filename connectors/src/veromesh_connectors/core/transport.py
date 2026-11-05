"""Layer 2, transport: getting requests to the VeroMesh API reliably.

Retries connection failures and retryable statuses (429, 502, 503, 504) with
exponential backoff and jitter, honouring Retry-After. Every API call the SDK
makes is safe to repeat (changesets use idempotency keys, item upserts replace
by type and ID, and commit is idempotent), so retrying never duplicates work.

TLS verification is on by default; ``ca_bundle`` points at a private CA. HTTP
proxies come from the standard environment variables (HTTPS_PROXY, NO_PROXY).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import httpx

RETRYABLE = {429, 502, 503, 504}


class TransportError(Exception):
    """The API could not be reached after all retries."""


class Transport:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
        retries: int = 4,
        backoff: float = 0.5,
        ca_bundle: str | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._http = http or httpx.Client(base_url=self.base_url, timeout=timeout, verify=ca_bundle or True)

    def _delay(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            after = resp.headers.get("Retry-After", "")
            if after.isdigit():
                return min(float(after), 60.0)
        return self.backoff * (2**attempt) * (0.5 + random.random())

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._http.request(method, url, **kw)
            except httpx.TransportError as exc:
                last = exc
                if attempt == self.retries:
                    break
                self._sleep(self._delay(attempt, None))
                continue
            if resp.status_code in RETRYABLE and attempt < self.retries:
                self._sleep(self._delay(attempt, resp))
                continue
            return resp
        raise TransportError(f"cannot reach the VeroMesh API at {self.base_url} ({last})")
