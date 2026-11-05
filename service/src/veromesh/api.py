"""HTTP API for the Veromesh ontology service."""

# No `from __future__ import annotations` here: FastAPI must resolve the
# dependency aliases defined inside create_app() at runtime.

from typing import Annotated

from datahub.configuration.common import OperationalError
from fastapi import Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import __version__, model_import
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


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _xlsx(data: bytes, filename: str) -> Response:
    return Response(
        data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
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

    @app.get("/model/template", tags=["model"])
    def model_template(c: Cfg, example: bool = False) -> Response:
        """Download the model workbook. `example=true` fills it with the procure-to-pay example."""
        from pathlib import Path

        from rdflib import Graph

        from .workbook import build_template, graph_to_workbook

        if not example:
            return _xlsx(build_template(), "veromesh-model-template.xlsx")
        g = Graph().parse(Path(c.ontology_dir) / "examples" / "org-procure-to-pay.ttl")
        return _xlsx(graph_to_workbook(g, "https://example.com/org#"), "veromesh-model-example.xlsx")

    @app.post("/model/import", tags=["model"])
    async def model_import_endpoint(
        s: Svc,
        c: Cfg,
        file: Annotated[UploadFile, File(description="Filled-in model workbook (.xlsx)")],
        source: Annotated[
            str | None, Query(description="Name for this source; defaults to the file name")
        ] = None,
        dry_run: bool = False,
    ):
        """Validate a workbook and, unless `dry_run`, replace that source's facts in the model.

        Returns 200 with the report when it passes (warnings allowed), 422 with the report when
        any row error or rule violation blocks it. Nothing is written on 422.
        """
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("Workbook is larger than 20 MB.")
        name = source or model_import.slugify_source((file.filename or "workbook").rsplit(".", 1)[0])
        report = model_import.import_workbook(
            s.store,
            data,
            source=name,
            filename=file.filename or "workbook.xlsx",
            base=c.instance_base,
            ontology_dir=c.ontology_dir,
            dry_run=dry_run,
        )
        blocked = report["summary"]["errors"] or report["summary"]["violations"]
        return JSONResponse(status_code=422 if blocked else 200, content=report)

    @app.get("/model/sources", tags=["model"])
    def model_sources(s: Svc) -> list[dict]:
        """Imported sources with when and from which file they were loaded."""
        return model_import.list_sources(s.store)

    @app.get("/model/export", tags=["model"])
    def model_export(s: Svc, c: Cfg, source: str) -> Response:
        """Download an imported source as a workbook, ready to edit and re-import."""
        return _xlsx(
            model_import.export_workbook(s.store, source, c.instance_base), f"veromesh-{source}.xlsx"
        )

    return app


app = create_app()
