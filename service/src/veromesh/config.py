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
    # Who a signed-in person is in the model, for attestations and corrections:
    # the first of these token claims that is present, matched against the
    # People field below (case-insensitively for email).
    oidc_person_claims: str = "email,preferred_username,upn"
    person_match_field: str = "email"  # or "employeeId" (with a claim that carries it)


@lru_cache
def get_settings() -> Settings:
    return Settings()
