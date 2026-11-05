"""HTTP API for the Veromesh ontology service."""

# No `from __future__ import annotations` here: FastAPI must resolve the
# dependency aliases defined inside create_app() at runtime.

from typing import Annotated

from datahub.configuration.common import OperationalError
from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__, changesets, entities
from .config import Settings, get_settings
from .datahub import DataHubPort, DataHubSdkPort
from .rdf_store import StoreRequestError, make_store
from .service import NotFound, VeromeshService


class ClassOut(BaseModel):
    iri: str
    label: str | None
    definition: str | None
    parents: list[str]
    graph: str | None


class BindingIn(BaseModel):
    datahub_urn: str
    class_iri: str


class PublishIn(BaseModel):
    graph: str
    name: str | None = None


class SparqlIn(BaseModel):
    query: str


class ChangesetIn(BaseModel):
    source: str
    mode: str = "replace"
    kind: str = "api"
    description: str | None = None
    file: str | None = None


class ItemsIn(BaseModel):
    items: list[dict]


def build_service(settings: Settings, datahub: DataHubPort | None = None) -> VeromeshService:
    return VeromeshService(
        make_store(settings.rdf_store),
        datahub or DataHubSdkPort(settings.datahub_gms_url, settings.datahub_token),
        bindings_graph=settings.bindings_graph,
        glossary_root_name=settings.glossary_root_name,
    )


def create_app(service: VeromeshService | None = None, settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Veromesh Ontology Service", version=__version__)
    app.state.service = service
    app.state.settings = settings

    def svc(request: Request) -> VeromeshService:
        if request.app.state.service is None:
            request.app.state.service = build_service(get_settings())
        return request.app.state.service

    Svc = Annotated[VeromeshService, Depends(svc)]

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    @app.exception_handler(SyntaxError)  # pyoxigraph raises SyntaxError on bad Turtle/SPARQL
    async def _bad_request(_: Request, exc: Exception):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(StoreRequestError)
    async def _store_error(_: Request, exc: StoreRequestError):
        # 400 from the store = the caller sent invalid Turtle or SPARQL; anything else is ours.
        status = 400 if exc.status == 400 else 502
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.exception_handler(OperationalError)
    async def _datahub_error(_: Request, exc: OperationalError):
        return JSONResponse(status_code=502, content={"detail": f"DataHub request failed: {exc}"})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/ontologies")
    def list_ontologies(s: Svc) -> list[str]:
        return s.list_ontologies()

    @app.put(
        "/ontologies",
        status_code=204,
        openapi_extra={
            "requestBody": {"required": True, "content": {"text/turtle": {"schema": {"type": "string"}}}}
        },
    )
    async def load_ontology(
        request: Request,
        s: Svc,
        graph: Annotated[str, Query(description="Named graph IRI for this ontology")],
        replace: bool = True,
    ) -> None:
        """Load a Turtle document (request body) into the named graph ``graph``."""
        turtle = (await request.body()).decode("utf-8")
        s.load_ontology(graph, turtle, replace=replace)

    @app.get("/classes", response_model=list[ClassOut])
    def list_classes(s: Svc, graph: str | None = None):
        return [ClassOut(**c.__dict__) for c in s.list_classes(graph)]

    @app.get("/classes/assets")
    def assets_for_class(s: Svc, class_iri: str, include_subclasses: bool = True) -> list[str]:
        return s.assets_for_class(class_iri, include_subclasses=include_subclasses)

    @app.post("/glossary/publish")
    def publish_glossary(s: Svc, body: PublishIn) -> dict:
        result = s.publish_glossary(body.graph, body.name)
        return result.__dict__

    @app.get("/bindings")
    def get_bindings(s: Svc, datahub_urn: str) -> list[str]:
        return s.classes_for_asset(datahub_urn)

    @app.post("/bindings", status_code=201)
    def bind(s: Svc, body: BindingIn) -> dict:
        return s.bind(body.datahub_urn, body.class_iri).to_aspect_dict()

    @app.delete("/bindings", status_code=204)
    def unbind(s: Svc, body: BindingIn) -> None:
        s.unbind(body.datahub_urn, body.class_iri)

    @app.post("/sparql")
    def sparql(s: Svc, body: SparqlIn) -> dict:
        """Read-only SPARQL (SELECT / ASK)."""
        return s.sparql(body.query)

    def cfg(request: Request) -> Settings:
        return request.app.state.settings or get_settings()

    Cfg = Annotated[Settings, Depends(cfg)]

    # ------------------------------------------------------------------ model API (v1)
    # The only way facts enter the organizational model. Clients such as the
    # spreadsheet loader build changesets; single-entity writes run as one-item
    # merge changesets.

    @app.exception_handler(changesets.Conflict)
    async def _conflict(_: Request, exc: changesets.Conflict):
        body = {"detail": str(exc)}
        if exc.report is not None:
            body["report"] = exc.report
        return JSONResponse(status_code=409, content=body)

    @app.exception_handler(LookupError)
    async def _unknown(_: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/api/v1/schema", tags=["model"])
    def model_schema() -> list[dict]:
        """Every entity type: its fields, kinds, required fields, reference targets and help text."""
        return entities.schema()

    @app.get("/api/v1/sources", tags=["model"])
    def model_sources(s: Svc) -> list[dict]:
        """Sources in the model, with kind, file, last update and the changeset that wrote them."""
        return changesets.list_sources(s.store)

    @app.get("/api/v1/examples/procure-to-pay", tags=["model"])
    def model_example(c: Cfg) -> dict:
        """The worked example as changeset items, ready to submit."""
        from pathlib import Path

        from rdflib import Graph

        g = Graph().parse(Path(c.ontology_dir) / "examples" / "org-procure-to-pay.ttl")
        base = "https://example.com/org#"
        items = []
        for t in entities.TYPES:
            for subj in entities.subjects_of_type(g, t):
                ent = entities.to_entity(g, subj, t, base)
                if not entities.TYPES[t].keyed:
                    ent.pop("id")
                items.append({"type": t, "op": "upsert", "entity": ent})
        return {"items": items}

    @app.post("/api/v1/changesets", status_code=201, tags=["changesets"])
    def changeset_create(s: Svc, body: ChangesetIn) -> dict:
        """Open a changeset for one source. `replace`: its items become the whole source. `merge`: upsert/delete within it."""
        return changesets.create(
            s.store,
            source=body.source,
            mode=body.mode,
            kind=body.kind,
            description=body.description,
            file=body.file,
        )

    @app.get("/api/v1/changesets/{cs}", tags=["changesets"])
    def changeset_get(s: Svc, cs: str) -> dict:
        return changesets.get(s.store, cs)

    @app.post("/api/v1/changesets/{cs}/items", tags=["changesets"])
    def changeset_items(s: Svc, cs: str, body: ItemsIn) -> dict:
        """Add items: `{"type": "kpis", "op": "upsert", "entity": {...}}`. Re-adding a type and ID replaces it."""
        return changesets.add_items(s.store, cs, body.items)

    @app.post("/api/v1/changesets/{cs}/validate", tags=["changesets"])
    def changeset_validate(s: Svc, c: Cfg, cs: str) -> dict:
        """Check the changeset against the whole model. Nothing is written."""
        return changesets.validate(s.store, cs, base=c.instance_base, ontology_dir=c.ontology_dir)

    @app.post("/api/v1/changesets/{cs}/commit", tags=["changesets"])
    def changeset_commit(s: Svc, c: Cfg, cs: str) -> dict:
        """Re-validate and write. 409 with the report when it is not valid."""
        return changesets.commit(s.store, cs, base=c.instance_base, ontology_dir=c.ontology_dir)

    @app.delete("/api/v1/changesets/{cs}", tags=["changesets"])
    def changeset_discard(s: Svc, cs: str) -> dict:
        return changesets.discard(s.store, cs)

    def _single(s: VeromeshService, c: Settings, source: str, items: list[dict], status: int = 200):
        try:
            report = changesets.apply(
                s.store, source=source, items=items, base=c.instance_base, ontology_dir=c.ontology_dir
            )
        except changesets.Conflict as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc), "report": exc.report})
        return JSONResponse(status_code=status, content=report)

    @app.get("/api/v1/{type_}", tags=["entities"])
    def entity_list(s: Svc, c: Cfg, type_: str, source: str | None = None) -> list[dict]:
        """All entities of a type across the model, or in one source."""
        return changesets.list_entities(s.store, type_, c.instance_base, source)

    @app.get("/api/v1/{type_}/{rid}", tags=["entities"])
    def entity_get(s: Svc, c: Cfg, type_: str, rid: str) -> dict:
        return changesets.get_entity(s.store, type_, rid, c.instance_base)

    @app.post("/api/v1/{type_}", tags=["entities"])
    def entity_create(s: Svc, c: Cfg, type_: str, source: str, entity: Annotated[dict, Body()]):
        """Create one entity in a source (validated against the whole model, then committed)."""
        entities.check_type(type_)
        rid = entities.entity_id(type_, entity)
        if rid and any(
            e["id"] == rid for e in changesets.list_entities(s.store, type_, c.instance_base, source)
        ):
            raise changesets.Conflict(
                f"{type_} '{rid}' already exists in source '{source}'; use PUT to replace it."
            )
        return _single(s, c, source, [{"type": type_, "op": "upsert", "entity": entity}], status=201)

    @app.put("/api/v1/{type_}/{rid}", tags=["entities"])
    def entity_put(s: Svc, c: Cfg, type_: str, rid: str, source: str, entity: Annotated[dict, Body()]):
        """Create or replace one entity in a source."""
        return _single(s, c, source, [{"type": type_, "op": "upsert", "entity": {**entity, "id": rid}}])

    @app.delete("/api/v1/{type_}/{rid}", tags=["entities"])
    def entity_delete(s: Svc, c: Cfg, type_: str, rid: str, source: str):
        """Delete one entity from a source; refused if something still references it."""
        return _single(s, c, source, [{"type": type_, "op": "delete", "entity": {"id": rid}}])

    return app


app = create_app()
