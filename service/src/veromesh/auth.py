"""Sign-in for the VeroMesh API.

Every route except /health and the docs pages needs a bearer token from the
customer's OpenID Connect provider: Entra ID first, Keycloak for customers
without Microsoft, or a customer's own provider. The service checks the
token's signature against the provider's published keys, its issuer, audience
and expiry, then reads the caller's roles from one claim.

Roles (app roles in Entra ID, realm or client roles in Keycloak):

    admin      everything, including raw SPARQL and loading ontologies
    steward    reviews, attests and corrects facts; sees people and provenance
    connector  writes changesets (the spreadsheet and CSV connectors, HR and CMDB feeds)
    reader     reads the model, except people's personal details
    agent      an AI agent: reads the model like a reader (the decision check comes next)

A token may carry several. Role names may have a prefix (``VEROMESH_OIDC_ROLE_PREFIX``,
e.g. ``VeroMesh.`` for Entra app roles named ``VeroMesh.Admin``); matching
ignores case.

Local development and the test suites can turn sign-in off with
``VEROMESH_AUTH_DISABLED=true``: every request then acts as an administrator.
The server refuses to start with sign-in neither configured nor turned off.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings

log = logging.getLogger("veromesh.auth")

ROLES = ("admin", "steward", "connector", "reader", "agent")

# Who may call what. "admin" may always; these name everyone else.
READ = ("reader", "agent", "steward", "connector")
STEWARD = ("steward",)
CONNECTOR = ("connector",)
STEWARD_OR_CONNECTOR = ("steward", "connector")
ADMIN: tuple[str, ...] = ()

# Entity types holding personal details: not for readers or agents until
# answers are scoped to what the caller may see.
PERSONAL_TYPES = {"people"}

bearer = HTTPBearer(auto_error=False, description="Access token from the customer's identity provider")


@dataclass(frozen=True)
class Caller:
    subject: str
    roles: frozenset[str]
    kind: str = "user"  # "user" (a person) or "app" (an agent, connector or other service)
    name: str | None = None
    client_id: str | None = None
    claims: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def has(self, *roles: str) -> bool:
        return "admin" in self.roles or any(r in self.roles for r in roles)


LOCAL_ADMIN = Caller(subject="local", roles=frozenset({"admin"}), name="sign-in disabled")


class ConfigError(RuntimeError):
    pass


def check_config(s: Settings) -> None:
    """Raise unless sign-in is configured or explicitly turned off."""
    if s.auth_disabled:
        return
    missing = [
        n
        for n, v in (("VEROMESH_OIDC_ISSUER", s.oidc_issuer), ("VEROMESH_OIDC_AUDIENCE", s.oidc_audience))
        if not v
    ]
    if missing:
        raise ConfigError(
            f"Sign-in is not configured: set {' and '.join(missing)}, "
            "or VEROMESH_AUTH_DISABLED=true for local development only."
        )


def _claim(claims: dict, path: str) -> Any:
    cur: Any = claims
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def roles_from(claims: dict, claim: str, prefix: str) -> frozenset[str]:
    raw = _claim(claims, claim)
    if isinstance(raw, str):
        raw = raw.split()
    out = set()
    for r in raw or []:
        if not isinstance(r, str):
            continue
        name = r
        if prefix and name.lower().startswith(prefix.lower()):
            name = name[len(prefix) :]
        name = name.lower()
        if name in ROLES:
            out.add(name)
    return frozenset(out)


def kind_of(claims: dict) -> str:
    """'app' for a token issued to a service rather than a person."""
    if claims.get("idtyp") == "app":  # Entra ID v2 app-only token
        return "app"
    if str(claims.get("preferred_username", "")).startswith("service-account-"):  # Keycloak
        return "app"
    if not any(k in claims for k in ("scp", "upn", "email", "preferred_username", "name")):
        return "app"
    return "user"


class Verifier:
    """Checks bearer tokens against one issuer's signing keys.

    ``key_for(token) -> key`` can be injected (tests); by default the keys come
    from the issuer's JWKS, found through its OpenID Connect discovery document.
    """

    def __init__(self, s: Settings, key_for: Callable[[str], Any] | None = None) -> None:
        self.issuer = s.oidc_issuer.rstrip("/") if s.oidc_issuer else ""
        self.audiences = [a.strip() for a in (s.oidc_audience or "").split(",") if a.strip()]
        self.algorithms = [a.strip() for a in s.oidc_algorithms.split(",") if a.strip()]
        self.roles_claim = s.oidc_roles_claim
        self.role_prefix = s.oidc_role_prefix
        self._jwks_url = s.oidc_jwks_url
        self._key_for = key_for
        self._jwks: jwt.PyJWKClient | None = None

    def _discover(self) -> str:
        url = f"{self.issuer}/.well-known/openid-configuration"
        doc = httpx.get(url, timeout=10).raise_for_status().json()
        return doc["jwks_uri"]

    def key_for(self, token: str) -> Any:
        if self._key_for:
            return self._key_for(token)
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(self._jwks_url or self._discover(), cache_keys=True, lifespan=3600)
        return self._jwks.get_signing_key_from_jwt(token).key

    def verify(self, token: str) -> Caller:
        key = self.key_for(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=self.algorithms,
            audience=self.audiences,
            options={"require": ["exp", "iss", "aud", "sub"]},
            leeway=60,
        )
        # Compare issuers without a trailing slash (Keycloak and Entra differ).
        if str(claims.get("iss", "")).rstrip("/") != self.issuer:
            raise jwt.InvalidIssuerError("Invalid issuer")
        return Caller(
            subject=str(claims.get("oid") or claims["sub"]),
            roles=roles_from(claims, self.roles_claim, self.role_prefix),
            kind=kind_of(claims),
            name=claims.get("name") or claims.get("preferred_username") or claims.get("upn"),
            client_id=claims.get("azp") or claims.get("appid") or claims.get("client_id"),
            claims=claims,
        )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def current_caller(request: Request, creds: HTTPAuthorizationCredentials | None, s: Settings) -> Caller:
    if s.auth_disabled:
        return LOCAL_ADMIN
    try:
        check_config(s)
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized("Sign in: send an access token as 'Authorization: Bearer <token>'.")
    verifier = getattr(request.app.state, "verifier", None)
    if verifier is None:
        verifier = request.app.state.verifier = Verifier(s)
    try:
        caller = verifier.verify(creds.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("The access token has expired.") from exc
    except (jwt.PyJWTError, KeyError) as exc:
        log.info("token refused: %s", exc)
        raise _unauthorized("The access token is not valid for this API.") from exc
    except httpx.HTTPError as exc:
        log.warning("identity provider unreachable: %s", exc)
        raise HTTPException(status_code=503, detail="Cannot reach the identity provider.") from exc
    if not caller.roles:
        raise HTTPException(status_code=403, detail="Signed in, but no VeroMesh role is assigned.")
    request.state.caller = caller
    return caller


def forbid(caller: Caller, *roles: str) -> None:
    if not caller.has(*roles):
        need = ", ".join(("admin",) + roles)
        raise HTTPException(status_code=403, detail=f"Needs one of these roles: {need}.")
