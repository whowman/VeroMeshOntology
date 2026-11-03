"""Runtime configuration, read from environment variables (prefix ``VEROMESH_``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VEROMESH_", env_file=".env", extra="ignore")

    # DataHub GMS (metadata service). Token is optional for local quickstart.
    datahub_gms_url: str = "http://localhost:8080"
    datahub_token: str | None = None

    # RDF store. "memory" uses an embedded Oxigraph store (dev/tests);
    # any http(s) URL is treated as an Oxigraph server base URL.
    rdf_store: str = "memory"

    # Named graph that holds asset-to-class bindings.
    bindings_graph: str = "https://veromesh.ai/graph/bindings"

    # Glossary node under which ontology classes are published in DataHub.
    glossary_root_name: str = "Veromesh Ontology"


@lru_cache
def get_settings() -> Settings:
    return Settings()
