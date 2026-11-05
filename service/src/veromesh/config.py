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

    # Named graph that holds asset-to-class bindings.
    bindings_graph: str = "https://veromesh.ai/graph/bindings"

    # Glossary node under which ontology classes are published in DataHub.
    glossary_root_name: str = "Veromesh Ontology"

    # IRI prefix for model instances imported from workbooks (one per customer install).
    instance_base: str = "urn:veromesh:org:"

    # Folder with the meta-model, validation rules and examples. Defaults to the
    # repository's ontology/ folder; the Docker image sets /app/ontology.
    ontology_dir: str = str(Path(__file__).resolve().parents[3] / "ontology")


@lru_cache
def get_settings() -> Settings:
    return Settings()
