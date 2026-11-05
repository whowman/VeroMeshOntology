"""Layer 4, session: who the connector is, and the state of its conversation.

* Authentication: a static bearer token, or OAuth 2.0 client credentials
  (e.g. a Keycloak service account) with the token refreshed before it
  expires, and once more on a 401.
* Conversation state: the changeset. Its idempotency key lets a connector that
  died mid-load resume the same changeset; ``cleanup_stale`` discards
  changesets a crashed run left open.

``ApiSession`` is also where layer 3 (presentation) lives for the VeroMesh side:
JSON request and response bodies.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from .transport import Transport, TransportError


class ApiError(Exception):
    def __init__(self, status: int, detail: str, body: dict | None = None) -> None:
        super().__init__(f"API returned {status}: {detail}")
        self.status = status
        self.detail = detail
        self.body = body or {}


class StaticToken:
    refreshable = False

    def __init__(self, token: str | None) -> None:
        self._token = token

    def token(self, force: bool = False) -> str | None:
        return self._token


class ClientCredentials:
    """OAuth 2.0 client-credentials grant against ``token_url``."""

    refreshable = True

    def __init__(
        self,
        transport: Transport,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        clock=time.time,
    ) -> None:
        self._t = transport
        self._url = token_url
        self._form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scope:
            self._form["scope"] = scope
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
            self._expires = self._clock() + float(body.get("expires_in", 300))
        return self._token


class ApiSession:
    def __init__(self, transport: Transport, auth: StaticToken | ClientCredentials | None = None) -> None:
        self.t = transport
        self.auth = auth or StaticToken(None)

    # -- one request: auth header, one refresh on 401, JSON both ways
    def call(self, method: str, path: str, ok=(200, 201), **kw):
        for attempt in range(2):
            token = self.auth.token(force=attempt > 0)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                r = self.t.request(method, f"/api/v1{path}", headers=headers, **kw)
            except TransportError as exc:
                raise ApiError(0, str(exc)) from exc
            if r.status_code == 401 and attempt == 0 and self.auth.refreshable:
                continue
            break
        try:
            body = r.json()
        except ValueError:
            body = {"detail": r.text}
        if r.status_code not in ok:
            detail = body.get("detail") if isinstance(body, dict) else str(body)
            raise ApiError(r.status_code, str(detail), body if isinstance(body, dict) else None)
        return body

    # -- model
    def schema(self) -> list[dict]:
        return self.call("GET", "/schema")

    def example_items(self) -> list[dict]:
        return self.call("GET", "/examples/procure-to-pay")["items"]

    def sources(self) -> list[dict]:
        return self.call("GET", "/sources")

    def source_version(self, source: str) -> str:
        return next((s["version"] for s in self.sources() if s["source"] == source), None) or "none"

    def entities(self, type_: str, source: str | None = None) -> list[dict]:
        return self.call("GET", f"/{type_}", params={"source": source} if source else None)

    # -- changesets (the conversation)
    def open_changeset(
        self,
        *,
        source: str,
        mode: str,
        kind: str,
        connector: str,
        idempotency_key: str | None = None,
        file: str | None = None,
    ) -> dict:
        body = {
            "source": source,
            "mode": mode,
            "kind": kind,
            "connector": connector,
            "file": file,
            "idempotencyKey": idempotency_key,
        }
        return self.call("POST", "/changesets", json=body)

    def changeset(self, cs: str) -> dict:
        return self.call("GET", f"/changesets/{cs}")

    def add_items(self, cs: str, items: list[dict]) -> dict:
        return self.call("POST", f"/changesets/{cs}/items", json={"items": items})

    def validate(self, cs: str) -> dict:
        return self.call("POST", f"/changesets/{cs}/validate")

    def commit(self, cs: str) -> dict:
        return self.call("POST", f"/changesets/{cs}/commit")

    def discard(self, cs: str) -> dict:
        return self.call("DELETE", f"/changesets/{cs}")

    def runs(self, source: str | None = None, status: str | None = None, limit: int = 20) -> list[dict]:
        params = {k: v for k, v in {"source": source, "status": status, "limit": limit}.items() if v}
        return self.call("GET", "/changesets", params=params)

    def cleanup_stale(self, older_than_hours: float, kinds=("spreadsheet", "connector")) -> list[str]:
        """Discard changesets that crashed runs left open."""
        cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
        discarded = []
        for status in ("open", "valid", "invalid"):
            for cs in self.runs(status=status, limit=500):
                created = datetime.fromisoformat(cs["createdAt"].replace("Z", "+00:00"))
                if cs["kind"] in kinds and created < cutoff:
                    self.discard(cs["id"])
                    discarded.append(cs["id"])
        return discarded
