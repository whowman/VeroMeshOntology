"""Layers 2-4 on the SOURCE side: calling a source system's own API.

Connectors for systems with an HTTP API (Workday, Microsoft Graph, ServiceNow and
so on) share the same needs as the VeroMesh side: retries with backoff on 429 and
5xx (the Transport), and an OAuth token that is fetched, cached and refreshed when
the system answers 401. ``SourceApi`` combines the two. Every call it makes is a
read, so retrying is always safe.

Token grants:

* ``ClientCredentials`` (core.session): app-only sign-in, e.g. Microsoft Entra ID.
* ``RefreshToken``: a long-lived refresh token issued to an integration, the
  usual way Workday API clients sign in.
"""

from __future__ import annotations

import time

import httpx

from .session import ApiError, StaticToken
from .transport import Transport, TransportError

#: Statuses on which a token is renewed (once) before the call is repeated.
RENEW = {401}
#: Reads are safe to repeat, so a source's 500 is retried as well as 429, 502, 503 and 504.
SOURCE_RETRY = frozenset({429, 500, 502, 503, 504})


class SourceError(Exception):
    """A source system refused a request or could not be reached."""

    def __init__(self, system: str, status: int, detail: str) -> None:
        super().__init__(f"{system}: {detail}" + (f" (HTTP {status})" if status else ""))
        self.system = system
        self.status = status


class RefreshToken:
    """OAuth 2.0 refresh-token grant, as Workday API clients use it."""

    refreshable = True

    def __init__(
        self,
        transport: Transport,
        token_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        clock=time.time,
    ) -> None:
        self._t = transport
        self._url = token_url
        self._form = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0

    def token(self, force: bool = False) -> str | None:
        if force or not self._token or self._clock() > self._expires - 30:
            r = self._t.request("POST", self._url, data=self._form)
            if r.status_code != 200:
                raise ApiError(r.status_code, f"token request failed: {r.text[:200]}")
            body = r.json()
            self._token = body["access_token"]
            if body.get("refresh_token"):  # some systems rotate it
                self._form["refresh_token"] = body["refresh_token"]
            self._expires = self._clock() + float(body.get("expires_in", 300))
        return self._token


class SourceApi:
    """GET JSON from a source system, with retries and token renewal."""

    def __init__(
        self,
        system: str,
        transport: Transport,
        auth=None,  # StaticToken, ClientCredentials, RefreshToken or a basic-auth object with header()
    ) -> None:
        self.system = system
        self.t = transport
        self.auth = auth or StaticToken(None)
        self.calls = 0

    def get(self, url: str, params: dict | None = None) -> dict:
        """``url`` is relative to the transport's base URL, or absolute (e.g. an @odata.nextLink)."""
        r: httpx.Response | None = None
        for attempt in range(2):
            try:
                token = self.auth.token(force=attempt > 0)
            except ApiError as exc:
                raise SourceError(self.system, exc.status, exc.detail) from exc
            if token:
                headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            elif hasattr(self.auth, "header"):  # basic authentication (Oracle Fusion)
                headers = {"Authorization": self.auth.header(), "Accept": "application/json"}
            else:
                headers = {}
            full = url if url.startswith(("http://", "https://")) else f"{self.t.base_url}{url}"
            try:
                r = self.t.request("GET", full, params=params, headers=headers)
            except TransportError as exc:
                raise SourceError(self.system, 0, str(exc)) from exc
            self.calls += 1
            if r.status_code in RENEW and attempt == 0 and self.auth.refreshable:
                continue
            break
        assert r is not None
        if r.status_code != 200:
            raise SourceError(self.system, r.status_code, _detail(r))
        try:
            return r.json()
        except ValueError as exc:
            raise SourceError(self.system, r.status_code, "the answer is not JSON") from exc


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200] or r.reason_phrase
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):  # Microsoft Graph
            return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
        if isinstance(err, str):
            return body.get("error_description") or err
        detail = body.get("detail")
        if isinstance(detail, dict):
            return _detail_of(detail)
        if detail:
            return str(detail)
    return str(body)[:200]


def _detail_of(detail: dict) -> str:
    err = detail.get("error")
    if isinstance(err, dict):
        return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
    return str(err or detail)[:200]
