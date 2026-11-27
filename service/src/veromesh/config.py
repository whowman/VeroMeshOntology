"""Runtime configuration, read from environment variables (prefix ``VEROMESH_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VEROMESH_", env_file=".env", extra="ignore")

    # DataHub GMS (metadata service). Token is optional for local quickstart.
    datahub_gms_url: str = "http://localhost:8080"
    datahub_token: str | None = None

    # RDF store: "memory" (embedded, not persisted; dev/tests),
    # "fuseki:<dataset URL>" (production default, e.g. fuseki:http://fuseki:3030/veromesh)
    # or "oxigraph:<server URL>".
    rdf_store: str = "memory"

    # Optional Fuseki dataset URL used for reads when rdf_store is unavailable:
    # the read replicas of a replicated install (Helm chart sets it).
    rdf_store_read_fallback: str | None = None

    # Named graph that holds asset-to-class bindings.
    bindings_graph: str = "https://veromesh.ai/graph/bindings"

    # Glossary node under which ontology classes are published in DataHub.
    glossary_root_name: str = "Veromesh Ontology"

    # IRI prefix for model instances imported from workbooks (one per customer install).
    instance_base: str = "urn:veromesh:org:"

    # Build the planner state in the background at startup, so the first edit or check
    # after a start does not (VEROMESH_WARM_ON_START=false to leave it to the first one).
    warm_on_start: bool = True

    # Folder with the meta-model, validation rules and examples. Defaults to the
    # repository's ontology/ folder; the Docker image sets /app/ontology.
    ontology_dir: str = str(Path(__file__).resolve().parents[3] / "ontology")

    # Logos and palette for the API's docs pages. Defaults to the repository's
    # branding/ folder; the Docker image sets /app/branding.
    branding_dir: str = str(Path(__file__).resolve().parents[3] / "branding")

    # Sign-in (see auth.py). The issuer is the identity provider's issuer URL:
    # Entra ID https://login.microsoftonline.com/<tenant ID>/v2.0 (v2 access
    # tokens), Keycloak https://<host>/realms/<realm>. The audience is what the
    # tokens are issued for: the API's application (client) ID or its
    # Application ID URI; several may be given, comma-separated.
    auth_disabled: bool = False  # local development and tests only: every request acts as an administrator
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None  # default: from the issuer's discovery document
    oidc_algorithms: str = "RS256"
    # Claim holding the caller's roles, dotted for nested claims: "roles" for
    # Entra app roles, "realm_access.roles" for Keycloak realm roles.
    oidc_roles_claim: str = "roles"
    oidc_role_prefix: str = ""  # e.g. "VeroMesh." for Entra app roles named VeroMesh.Admin
    # The steward screen (/steward) signs people in in the browser (authorization code + PKCE):
    # the client ID of a public (single-page application) registration, and the scope to ask for,
    # e.g. "api://veromesh-acme/access_as_user" for Entra ID, "openid" for Keycloak.
    oidc_ui_client_id: str | None = None
    oidc_ui_scope: str = "openid profile email"
    # Who a signed-in person is in the model, for attestations and corrections:
    # the first of these token claims that is present, matched against the
    # People field below (case-insensitively for email).
    oidc_person_claims: str = "email,preferred_username,upn"
    person_match_field: str = "email"  # or "employeeId" (with a claim that carries it)

    # The decision log (decisions.py): "memory" (development and tests; lost on
    # restart) or postgresql://user:password@host:5432/database[?sslmode=require],
    # its own database, append-only and hash-chained.
    decision_log: str = "memory"
    # The decision check refuses when the facts behind the agent's authority are not
    # all attested and current (decision 3). Off only for demonstrations on unreviewed data.
    decision_requires_attested: bool = True
    # Refuse an agent no current approved AI use case covers for the action (the board's
    # decision gates the agent). Off by default: turn it on once the use cases are loaded.
    decision_requires_use_case: bool = False
    # An action with no mapped impact: "consult" the owners of its process (the white paper:
    # unknown impact is a reason to consult) or "allow" (the behaviour before 24 November).
    decision_unmapped_impact: str = "consult"
    # Log reads with the caller, agent, principal and stated purpose on the decision log's
    # hash chain: "agents" (callers signed in with the agent role), "all", or "off".
    query_log: str = "agents"
    # The calendar the dates people reason about follow (validity dates, per-day
    # limits, measures periods): an IANA time zone such as America/New_York.
    # Timestamps are stored in UTC either way (clock.py).
    time_zone: str = "UTC"

    # The usage report (usage.py): made by every install each month, signed and kept
    # beside the decision log, sent to VeroMesh when a mail adapter is set up.
    install_id: str | None = None  # default: one generated and kept on first use
    customer_name: str = ""
    hosting: str = "self-hosted"  # or "hosted" (VeroMesh runs it)
    # The licensed tier until the licence file is built: small, medium, large or
    # extra-large (empty: not set; the report then shows no share of tier).
    licensed_tier: str = ""
    # The install's Ed25519 signing key (PEM, PKCS#8), from a secret. Without one, a key
    # is generated on first use and kept in the usage tables.
    usage_signing_key: str | None = None
    # Mail secrets (the rest of the mail settings is set on the Usage page or through the API).
    usage_smtp_password: str | None = None
    usage_m365_client_secret: str | None = None
    # The licence (licence.py): a signed licence document (JSON), from a Secret. Licences
    # can also be uploaded by an administrator (Administration > Licence).
    licence: str | None = None
    usage_job: bool = True  # count every hour and make the reports in the background
    usage_interval_s: float = 3600.0
    # Where people reach this install (https://ontology.example.com), for links in notices.
    public_url: str | None = None
    # Notices to owners of attestations recorded for them in meetings (notify.py): sent in
    # the background after each recording and every hour (reminders, retries).
    notify_job: bool = True
    notify_interval_s: float = 3600.0
    # The MCP gateway (gateway.py, gap G3): held tool calls whose owners approved are sent
    # right after the answer, and by a sweep this often (answers given in Teams, restarts).
    gateway_job: bool = True
    gateway_interval_s: float = 60.0
    # The Teams channel (teams.py): the VeroMesh bot's Entra application ID and client
    # secret, and the customer's tenant. Unset: no Teams channel.
    teams_app_id: str | None = None
    teams_app_password: str | None = None
    teams_tenant_id: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
