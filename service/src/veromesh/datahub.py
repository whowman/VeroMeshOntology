"""DataHub access.

``DataHubPort`` is the narrow interface the rest of the service depends on, so
tests can swap in a fake and the SDK stays at the edge.

Writes go to three places in DataHub:

* Glossary nodes/terms – ontology classes published as the business glossary,
  so they show up natively in DataHub search and the UI.
* ``glossaryTerms`` on the asset – the standard DataHub link from asset to term.
* ``veromeshOntologyBindings`` – Veromesh's custom aspect (see ``datahub-model/``)
  holding the exact class IRIs, which the glossary alone cannot express.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

BINDINGS_ASPECT = "veromeshOntologyBindings"


def term_id_for(iri: str) -> str:
    """Deterministic DataHub glossary id for an ontology class IRI."""
    return "veromesh." + hashlib.sha1(iri.encode("utf-8")).hexdigest()[:20]


def term_urn_for(iri: str) -> str:
    return f"urn:li:glossaryTerm:{term_id_for(iri)}"


def node_urn_for(node_id: str) -> str:
    return f"urn:li:glossaryNode:{node_id}"


@dataclass
class Binding:
    class_iri: str
    ontology_iri: str
    label: str | None = None
    ontology_version: str | None = None
    relation: str = "represents"
    source: str = "veromesh"

    def to_aspect_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "classIri": self.class_iri,
            "ontologyIri": self.ontology_iri,
            "relation": self.relation,
            "source": self.source,
        }
        if self.label:
            out["label"] = self.label
        if self.ontology_version:
            out["ontologyVersion"] = self.ontology_version
        return out

    @classmethod
    def from_aspect_dict(cls, d: dict[str, Any]) -> Binding:
        return cls(
            class_iri=d["classIri"],
            ontology_iri=d["ontologyIri"],
            label=d.get("label"),
            ontology_version=d.get("ontologyVersion"),
            relation=d.get("relation", "represents"),
            source=d.get("source", "veromesh"),
        )


class DataHubPort(Protocol):
    def get_bindings(self, urn: str) -> list[Binding]: ...

    def put_bindings(self, urn: str, bindings: list[Binding]) -> None: ...

    def upsert_glossary_node(
        self, node_id: str, name: str, definition: str, parent_node_urn: str | None = None
    ) -> str: ...

    def upsert_glossary_term(
        self,
        iri: str,
        name: str,
        definition: str,
        parent_node_urn: str | None,
    ) -> str: ...

    def set_asset_terms(self, urn: str, add: list[str], remove: list[str]) -> None: ...


class DataHubSdkPort:
    """``DataHubPort`` backed by the ``acryl-datahub`` Python SDK."""

    def __init__(self, server: str, token: str | None = None) -> None:
        from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph

        self._graph = DataHubGraph(DatahubClientConfig(server=server, token=token))

    # -- custom aspect -----------------------------------------------------

    def get_bindings(self, urn: str) -> list[Binding]:
        import requests

        try:
            # No aspect filter: on DataHub v1.8.0, entitiesV2 with ?aspects=List(...)
            # naming a plugin aspect returns only the key aspect, so the bindings
            # would silently read as empty and the next write would drop them.
            raw = self._graph.get_entity_raw(urn)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return []
            raise
        aspect = (raw or {}).get("aspects", {}).get(BINDINGS_ASPECT)
        if not aspect:
            return []
        value = aspect.get("value", aspect)
        if isinstance(value, str):  # some GMS versions return custom aspects as a JSON string
            value = json.loads(value)
        return [Binding.from_aspect_dict(b) for b in value.get("bindings", [])]

    def put_bindings(self, urn: str, bindings: list[Binding]) -> None:
        from datahub.metadata.schema_classes import (
            ChangeTypeClass,
            GenericAspectClass,
            MetadataChangeProposalClass,
        )

        payload = {"bindings": [b.to_aspect_dict() for b in bindings]}
        self._graph.emit_mcp(
            MetadataChangeProposalClass(
                entityType=urn.split(":")[2],
                entityUrn=urn,
                changeType=ChangeTypeClass.UPSERT,
                aspectName=BINDINGS_ASPECT,
                aspect=GenericAspectClass(
                    contentType="application/json",
                    value=json.dumps(payload).encode("utf-8"),
                ),
            )
        )

    # -- glossary ----------------------------------------------------------

    def upsert_glossary_node(
        self, node_id: str, name: str, definition: str, parent_node_urn: str | None = None
    ) -> str:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import GlossaryNodeInfoClass

        urn = node_urn_for(node_id)
        self._graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=GlossaryNodeInfoClass(definition=definition, name=name, parentNode=parent_node_urn),
            )
        )
        return urn

    def upsert_glossary_term(
        self,
        iri: str,
        name: str,
        definition: str,
        parent_node_urn: str | None,
    ) -> str:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import GlossaryTermInfoClass

        urn = term_urn_for(iri)
        self._graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=GlossaryTermInfoClass(
                    name=name,
                    definition=definition,
                    termSource="EXTERNAL",
                    sourceRef="Veromesh",
                    sourceUrl=iri,
                    parentNode=parent_node_urn,
                    customProperties={"veromesh.iri": iri},
                ),
            )
        )
        return urn

    def set_asset_terms(self, urn: str, add: list[str], remove: list[str]) -> None:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import (
            AuditStampClass,
            GlossaryTermAssociationClass,
            GlossaryTermsClass,
        )

        current = self._graph.get_aspect(urn, GlossaryTermsClass)
        existing = [t.urn for t in current.terms] if current else []
        wanted = [t for t in existing if t not in remove]
        wanted += [t for t in add if t not in wanted]
        if wanted == existing:
            return
        self._graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=GlossaryTermsClass(
                    terms=[GlossaryTermAssociationClass(urn=t) for t in wanted],
                    auditStamp=AuditStampClass(
                        time=int(time.time() * 1000), actor="urn:li:corpuser:veromesh"
                    ),
                ),
            )
        )
