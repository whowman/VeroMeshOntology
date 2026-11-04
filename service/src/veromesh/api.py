"""HTTP API for the Veromesh ontology service."""

# No `from __future__ import annotations` here: FastAPI must resolve the
# dependency aliases defined inside create_app() at runtime.

from typing import Annotated

from datahub.configuration.common import OperationalError
from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel

from . import __version__
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


def build_service(settings: Settings, datahub: DataHubPort | None = None) -> VeromeshService:
    return VeromeshService(
        make_store(settings.rdf_store),
        datahub or DataHubSdkPort(settings.datahub_gms_url, settings.datahub_token),
        bindings_graph=settings.bindings_graph,
        glossary_root_name=settings.glossary_root_name,
    )


def create_app(service: VeromeshService | None = None) -> FastAPI:
    app = FastAPI(title="Veromesh Ontology Service", version=__version__)
    app.state.service = service

    def svc(request: Request) -> VeromeshService:
        if request.app.state.service is None:
            request.app.state.service = build_service(get_settings())
        return request.app.state.service

    Svc = Annotated[VeromeshService, Depends(svc)]

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    @app.exception_handler(SyntaxError)  # pyoxigraph raises SyntaxError on bad Turtle/SPARQL
    async def _bad_request(_: Request, exc: Exception):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(StoreRequestError)
    async def _store_error(_: Request, exc: StoreRequestError):
        from fastapi.responses import JSONResponse

        # 400 from the store = the caller sent invalid Turtle or SPARQL; anything else is ours.
        status = 400 if exc.status == 400 else 502
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.exception_handler(OperationalError)
    async def _datahub_error(_: Request, exc: OperationalError):
        from fastapi.responses import JSONResponse

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

    return app


app = create_app()
