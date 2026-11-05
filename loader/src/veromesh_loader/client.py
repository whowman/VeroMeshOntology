"""Thin client for the VeroMesh model API (/api/v1)."""

from __future__ import annotations

import httpx


class ApiError(Exception):
    def __init__(self, status: int, detail: str, body: dict | None = None) -> None:
        super().__init__(f"API returned {status}: {detail}")
        self.status = status
        self.detail = detail
        self.body = body or {}


class ApiClient:
    def __init__(self, base_url: str, token: str | None = None, http: httpx.Client | None = None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=120.0)
        self._headers = headers

    def _call(self, method: str, path: str, ok=(200, 201), **kw):
        try:
            r = self._http.request(method, f"/api/v1{path}", headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"cannot reach the VeroMesh API ({exc})") from exc
        try:
            body = r.json()
        except ValueError:
            body = {"detail": r.text}
        if r.status_code not in ok:
            detail = body.get("detail") if isinstance(body, dict) else str(body)
            raise ApiError(r.status_code, str(detail), body if isinstance(body, dict) else None)
        return body

    def schema(self) -> list[dict]:
        return self._call("GET", "/schema")

    def example_items(self) -> list[dict]:
        return self._call("GET", "/examples/procure-to-pay")["items"]

    def sources(self) -> list[dict]:
        return self._call("GET", "/sources")

    def entities(self, type_: str, source: str | None = None) -> list[dict]:
        return self._call("GET", f"/{type_}", params={"source": source} if source else None)

    def create_changeset(
        self, source: str, mode: str, kind: str = "spreadsheet", file: str | None = None
    ) -> dict:
        return self._call(
            "POST", "/changesets", json={"source": source, "mode": mode, "kind": kind, "file": file}
        )

    def add_items(self, cs: str, items: list[dict]) -> dict:
        return self._call("POST", f"/changesets/{cs}/items", json={"items": items})

    def validate(self, cs: str) -> dict:
        return self._call("POST", f"/changesets/{cs}/validate")

    def commit(self, cs: str) -> dict:
        return self._call("POST", f"/changesets/{cs}/commit")

    def discard(self, cs: str) -> dict:
        return self._call("DELETE", f"/changesets/{cs}")
