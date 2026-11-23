"""HTTP API for the Veromesh ontology service."""

# No `from __future__ import annotations` here: FastAPI must resolve the
# dependency aliases defined inside create_app() at runtime.

import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

from datahub.configuration.common import OperationalError
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from pydantic import BaseModel
from starlette.routing import Route

from . import (
    __version__,
    auth,
    branding,
    catalog,
    changesets,
    clock,
    coverage,
    decide,
    decisions,
    domains,
    entities,
    followup,
    gcsetup,
    incremental,
    licence,
    mcp_server,
    suggest,
    usage,
    validation,
)
from . import provenance as prov
from .config import Settings, get_settings
from .datahub import DataHubPort, DataHubSdkPort
from .rdf_store import StoreRequestError, make_store
from .service import NotFound, VeromeshService

log = logging.getLogger("veromesh.api")


class ClassOut(BaseModel):
    iri: str
    label: str | None
    definition: str | None
    parents: list[str]
    graph: str | None


class SuggestionsIn(BaseModel):
    items: list[dict]  # [{dataElement, column}]
    note: str | None = None


class CatalogPublishIn(BaseModel):
    ids: list[str] | None = None  # only these data products; default all


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
    connector: str | None = None
    idempotencyKey: str | None = None


class ItemsIn(BaseModel):
    items: list[dict]


class SourceIn(BaseModel):
    category: str
    ownerRole: str | None = None
    description: str | None = None


class AttestationIn(BaseModel):
    verdict: str  # attested | disputed
    # With sign-in, the signed-in person (leave it out); with sign-in off, their person ID.
    attester: str | None = None
    role: str  # role ID the person acts in, which they must fill
    fields: list[str] | None = None
    source: str | None = None
    note: str | None = None


class CheckIn(BaseModel):
    action: str  # action ID, e.g. ApproveSupplierInvoice
    amount: str | float | None = None
    currency: str | None = None
    note: str | None = None
    # Only with sign-in off, or for an administrator testing a check on an agent's behalf:
    agent: str | None = None
    onDate: str | None = None  # YYYY-MM-DD; default today


class OverrideIn(BaseModel):
    verdict: str  # approved | rejected
    role: str | None = None  # the consulted role you answer as, when you fill several
    note: str | None = None
    person: str | None = None  # only with sign-in off: who is answering


class OutcomeIn(BaseModel):
    outcome: str  # done | not_done
    note: str | None = None


class CorrectionIn(BaseModel):
    values: dict
    attester: str | None = None  # as for AttestationIn
    role: str
    note: str | None = None


def build_service(settings: Settings, datahub: DataHubPort | None = None) -> VeromeshService:
    return VeromeshService(
        make_store(settings.rdf_store, settings.rdf_store_read_fallback),
        datahub or DataHubSdkPort(settings.datahub_gms_url, settings.datahub_token),
        bindings_graph=settings.bindings_graph,
        glossary_root_name=settings.glossary_root_name,
    )


def create_app(
    service: VeromeshService | None = None,
    settings: Settings | None = None,
    decision_log: decisions.DecisionLog | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        s = settings or get_settings()
        # Refuse to serve with sign-in neither configured nor explicitly turned off.
        auth.check_config(s)
        if s.decision_log == "memory" and not s.auth_disabled:
            log.warning(
                "The decision log is in memory: decisions are lost on restart. Set VEROMESH_DECISION_LOG."
            )
        validation.start_worker(s.ontology_dir)  # so the first edit does not wait for it
        gcsetup.after_startup()  # libraries loaded: full collections need not visit them again
        if s.warm_on_start:
            threading.Thread(target=warm_planner, args=(app, s), name="veromesh-warm", daemon=True).start()
        job = usage.Job(lambda: usage_of(app), s.usage_interval_s).start() if s.usage_job else None
        try:
            async with app.state.mcp.session_manager.run():
                yield
        finally:
            if job is not None:
                job.stop()

    app = FastAPI(
        lifespan=lifespan,
        title="VeroMesh API",
        version=__version__,
        description="Organizational ontology, model changesets and DataHub bindings.",
        docs_url=None,
        redoc_url=None,
    )
    app.state.service = service
    app.state.settings = settings
    app.state.decision_log = decision_log
    app.state.usage = None
    app.state.planner = "not started"

    def warm_planner(a: FastAPI, c: Settings) -> None:
        """Build the planner state edits and checks share, so the first of them after a
        start does not build it (about 30 s at 20,000 people, minutes at 500,000). An edit
        that arrives meanwhile waits for this build rather than starting another."""
        a.state.planner = "warming"
        t0 = time.perf_counter()
        try:
            if a.state.service is None:
                a.state.service = build_service(c)
            incremental.warm(a.state.service.store, c.instance_base)
        except Exception:  # the first edit or check builds it instead
            a.state.planner = "not warmed"
            log.exception("Could not build the planner state at startup")
            return
        a.state.planner = "ready"
        log.info("Planner state ready %.1f s after startup", time.perf_counter() - t0)

    def usage_of(a: FastAPI) -> usage.Usage:
        """The install's usage reports (built on first use, kept with the app)."""
        if a.state.usage is None:
            c = a.state.settings or get_settings()
            if a.state.decision_log is None:
                a.state.decision_log = decisions.make_log(c.decision_log)
            if a.state.service is None:
                a.state.service = build_service(c)

            def model():
                return a.state.service.store, c.instance_base

            u = usage.Usage(usage.make_store(c.decision_log), a.state.decision_log, c, model)
            u.licensing = licence.Licensing(u, c)
            a.state.usage = u
        return a.state.usage

    @app.middleware("http")
    async def read_only_after_expiry(request: Request, call_next):
        """After the licence's grace period (or 30 days unlicensed), writes that load or
        change the model are refused; reads, decision checks, attestations and the rest go on."""
        if licence.blocked(request.method, request.url.path):
            try:
                st = usage_of(request.app).licensing.status()
            except Exception:  # noqa: BLE001 — licensing never takes the product down
                log.exception("licence status unavailable; the request goes on")
                st = {"readOnly": False}
            if st["readOnly"]:
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"licence expired: {st['message']}", "licence": st["stage"]},
                )
        return await call_next(request)

    branding.install(app, Path((settings or get_settings()).branding_dir))
    # Dates people reason about follow the customer's calendar (clock.py).
    clock.configure((settings or get_settings()).time_zone)
    # The steward screen: plain HTML and JavaScript, signed in in the browser (sign-in config below).
    app.mount(
        "/steward",
        StaticFiles(directory=Path(__file__).parent / "static" / "steward", html=True),
        name="steward",
    )
    # Administration: the install's licence and usage reports (administrators; the page asks
    # the API, which checks the role). Both screens share /common.
    app.mount(
        "/admin",
        StaticFiles(directory=Path(__file__).parent / "static" / "admin", html=True),
        name="admin",
    )
    app.mount("/common", StaticFiles(directory=Path(__file__).parent / "static" / "common"), name="common")

    def svc(request: Request) -> VeromeshService:
        if request.app.state.service is None:
            request.app.state.service = build_service(get_settings())
        return request.app.state.service

    Svc = Annotated[VeromeshService, Depends(svc)]

    def cfg(request: Request) -> Settings:
        return request.app.state.settings or get_settings()

    Cfg = Annotated[Settings, Depends(cfg)]

    def dlog(request: Request, c: Cfg) -> decisions.DecisionLog:
        if request.app.state.decision_log is None:
            request.app.state.decision_log = decisions.make_log(c.decision_log)
        return request.app.state.decision_log

    Log = Annotated[decisions.DecisionLog, Depends(dlog)]

    def caller(
        request: Request, c: Cfg, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(auth.bearer)]
    ) -> auth.Caller:
        return auth.current_caller(request, creds, c)

    Who = Annotated[auth.Caller, Depends(caller)]

    def need(roles: tuple[str, ...]):
        """Route dependency: the caller holds admin or one of ``roles``."""

        def check(who: Who) -> auth.Caller:
            auth.forbid(who, *roles)
            return who

        return [Depends(check)]

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(prov.NotYourRole)
    async def _not_your_role(_: Request, exc: prov.NotYourRole):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(PermissionError)
    async def _not_allowed(_: Request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(decisions.InvalidEntry)
    async def _bad_entry(_: Request, exc: decisions.InvalidEntry):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

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

    @app.get("/api/v1/sign-in/config", tags=["sign-in"])
    def sign_in_config(c: Cfg) -> dict:
        """For the steward screen in the browser: how to sign in (no secrets)."""
        if c.auth_disabled:
            return {"disabled": True}
        out = {
            "disabled": False,
            "issuer": c.oidc_issuer,
            "clientId": c.oidc_ui_client_id,
            "scope": c.oidc_ui_scope,
        }
        if c.oidc_issuer and c.oidc_ui_client_id:
            try:
                doc = auth.discovery(c.oidc_issuer)
                out.update(
                    authorizationEndpoint=doc["authorization_endpoint"], tokenEndpoint=doc["token_endpoint"]
                )
            except Exception as exc:  # the page shows it; the API itself keeps working
                out["problem"] = f"Cannot read the identity provider's configuration: {exc}"
        else:
            out["problem"] = "The steward screen's sign-in is not configured (VEROMESH_OIDC_UI_CLIENT_ID)."
        return out

    @app.get("/health")
    def health() -> dict:
        # planner: whether this copy has built the state edits and checks use (warming,
        # ready). It does not change the status: requests are served while it builds.
        return {"status": "ok", "version": __version__, "planner": app.state.planner}

    @app.get("/ontologies", dependencies=need(auth.READ))
    def list_ontologies(s: Svc) -> list[str]:
        return s.list_ontologies()

    @app.put(
        "/ontologies",
        dependencies=need(auth.ADMIN),
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

    @app.get("/classes", dependencies=need(auth.READ), response_model=list[ClassOut])
    def list_classes(s: Svc, graph: str | None = None):
        return [ClassOut(**c.__dict__) for c in s.list_classes(graph)]

    @app.get("/classes/assets", dependencies=need(auth.READ))
    def assets_for_class(s: Svc, class_iri: str, include_subclasses: bool = True) -> list[str]:
        return s.assets_for_class(class_iri, include_subclasses=include_subclasses)

    @app.post("/glossary/publish", dependencies=need(auth.ADMIN))
    def publish_glossary(s: Svc, body: PublishIn) -> dict:
        result = s.publish_glossary(body.graph, body.name)
        return result.__dict__

    @app.get("/bindings", dependencies=need(auth.READ))
    def get_bindings(s: Svc, datahub_urn: str) -> list[str]:
        return s.classes_for_asset(datahub_urn)

    @app.post("/bindings", dependencies=need(auth.STEWARD), status_code=201)
    def bind(s: Svc, body: BindingIn) -> dict:
        return s.bind(body.datahub_urn, body.class_iri).to_aspect_dict()

    @app.delete("/bindings", dependencies=need(auth.STEWARD), status_code=204)
    def unbind(s: Svc, body: BindingIn) -> None:
        s.unbind(body.datahub_urn, body.class_iri)

    @app.post("/sparql", dependencies=need(auth.ADMIN))
    def sparql(s: Svc, body: SparqlIn) -> dict:
        """Read-only SPARQL (SELECT / ASK)."""
        return s.sparql(body.query)

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

    @app.get("/api/v1/schema", dependencies=need(auth.READ), tags=["model"])
    def model_schema() -> list[dict]:
        """Every entity type: its fields, kinds, required fields, reference targets and help text."""
        return entities.schema()

    @app.get("/api/v1/sources", dependencies=need(auth.READ), tags=["model"])
    def model_sources(s: Svc) -> list[dict]:
        """Sources in the model, with kind, file, last update and the changeset that wrote them."""
        return changesets.list_sources(s.store)

    @app.get("/api/v1/examples/procure-to-pay", dependencies=need(auth.READ), tags=["model"])
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

    @app.post("/api/v1/changesets", dependencies=need(auth.CONNECTOR), status_code=201, tags=["changesets"])
    def changeset_create(s: Svc, body: ChangesetIn):
        """Open a changeset for one source. `replace`: its items become the whole source. `merge`: upsert/delete within it."""
        cs = changesets.create(
            s.store,
            source=body.source,
            mode=body.mode,
            kind=body.kind,
            description=body.description,
            file=body.file,
            connector=body.connector,
            idempotency_key=body.idempotencyKey,
        )
        return JSONResponse(status_code=200 if cs["reused"] else 201, content=cs)

    @app.get("/api/v1/changesets", dependencies=need(auth.STEWARD_OR_CONNECTOR), tags=["changesets"])
    def changeset_list(
        s: Svc, source: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Run history, newest first; filter by source and status (e.g. open, committed)."""
        return changesets.list_changesets(s.store, source=source, status=status, limit=limit)

    @app.get("/api/v1/changesets/{cs}", dependencies=need(auth.STEWARD_OR_CONNECTOR), tags=["changesets"])
    def changeset_get(s: Svc, cs: str) -> dict:
        return changesets.get(s.store, cs)

    @app.post("/api/v1/changesets/{cs}/items", dependencies=need(auth.CONNECTOR), tags=["changesets"])
    def changeset_items(s: Svc, cs: str, body: ItemsIn) -> dict:
        """Add items: `{"type": "kpis", "op": "upsert", "entity": {...}}`. Re-adding a type and ID replaces it."""
        return changesets.add_items(s.store, cs, body.items)

    @app.post("/api/v1/changesets/{cs}/validate", dependencies=need(auth.CONNECTOR), tags=["changesets"])
    def changeset_validate(s: Svc, c: Cfg, cs: str) -> dict:
        """Check the changeset against the whole model. Nothing is written."""
        return changesets.validate(s.store, cs, base=c.instance_base, ontology_dir=c.ontology_dir)

    @app.post("/api/v1/changesets/{cs}/commit", dependencies=need(auth.CONNECTOR), tags=["changesets"])
    def changeset_commit(s: Svc, c: Cfg, cs: str) -> dict:
        """Re-validate and write. 409 with the report when it is not valid."""
        return changesets.commit(s.store, cs, base=c.instance_base, ontology_dir=c.ontology_dir)

    @app.delete("/api/v1/changesets/{cs}", dependencies=need(auth.CONNECTOR), tags=["changesets"])
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

    # ------------------------------------------------------------------ provenance and governance
    # (declared before the per-type routes, which would otherwise match these paths)

    @app.get("/api/v1/governance", dependencies=need(auth.READ), tags=["governance"])
    def governance() -> dict:
        """Source rankings, which fields a steward may correct, and re-confirmation cadence."""
        return prov.governance()

    @app.put("/api/v1/sources/{name}", dependencies=need(auth.ADMIN), tags=["governance"])
    def source_register(s: Svc, c: Cfg, name: str, body: SourceIn) -> dict:
        """Register a source's category (hr, iam, cmdb, erp, catalog, governance, spreadsheet), owner role and description."""
        out = prov.register_source(s.store, name, body.category, body.ownerRole, body.description)
        prov.refresh_effective(s.store, c.instance_base)
        return out

    @app.get("/api/v1/domains", dependencies=need(auth.READ), tags=["governance"])
    def domains_list(s: Svc) -> dict:
        """Which source category decides each question (source weighting): per domain, the
        authoritative and evidence categories, the fields it covers, and whether this
        organization changed it from the default."""
        return domains.current(s.store)

    @app.put("/api/v1/domains/{name}", dependencies=need(auth.ADMIN), tags=["governance"])
    def domain_set(s: Svc, c: Cfg, dl: Log, who: Who, name: str, body: dict) -> dict:
        """Change one domain (only the keys given: authoritative, evidence, contractors, union,
        fields). Recorded on the decision log's hash chain; the effective model is rebuilt."""
        try:
            out = domains.set_domain(s.store, name, body)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\"")) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        dl.append(
            {
                "kind": "configuration",
                "setting": f"domain:{name}",
                "value": body,
                "outcome": "changed",
                "subject": who.subject,
            }
        )
        domains.activate(s.store)
        rebuilt = prov.rebuild(s.store, c.instance_base)
        return {"domain": name, **out, "rebuilt": rebuilt}

    @app.delete("/api/v1/domains/{name}", dependencies=need(auth.ADMIN), tags=["governance"])
    def domain_reset(s: Svc, c: Cfg, dl: Log, who: Who, name: str) -> dict:
        """Return one domain to the shipped default. Recorded; the effective model is rebuilt."""
        try:
            out = domains.reset_domain(s.store, name)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\"")) from e
        dl.append(
            {
                "kind": "configuration",
                "setting": f"domain:{name}",
                "value": {"reset": True},
                "outcome": "reset",
                "subject": who.subject,
            }
        )
        domains.activate(s.store)
        rebuilt = prov.rebuild(s.store, c.instance_base)
        return {"domain": name, **out, "rebuilt": rebuilt}

    @app.get("/api/v1/conflicts", dependencies=need(auth.STEWARD), tags=["governance"])
    def conflicts(s: Svc, type: str | None = None) -> list[dict]:
        """Fields where sources state different values, with the value that wins."""
        return prov.conflicts(s.store, type)

    @app.get("/api/v1/attestations", dependencies=need(auth.STEWARD), tags=["governance"])
    def review_queue(
        s: Svc,
        status: str = "unconfirmed",
        type: str | None = None,
        ownerRole: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Review queue: entities with fields in a status (unconfirmed, attested, disputed, expired)."""
        return prov.review_queue(s.store, status=status, type_=type, owner_role=ownerRole, limit=limit)

    @app.post("/api/v1/effective/rebuild", dependencies=need(auth.ADMIN), tags=["governance"])
    def effective_rebuild(s: Svc, c: Cfg) -> dict:
        """Recompute the effective model (e.g. after valid-until dates pass)."""
        return prov.rebuild(s.store, c.instance_base)

    @app.post("/api/v1/catalog/data-products", dependencies=need(auth.STEWARD), tags=["catalog"])
    def catalog_publish_data_products(s: Svc, c: Cfg, body: CatalogPublishIn | None = None) -> dict:
        """Write each data product's organizational context (owner and steward roles, capabilities,
        KPIs, consuming agents, lineage, systems, data elements, sensitivity, policies) onto its
        catalog entry, as veromesh.* custom properties. Roles only, never people."""
        try:
            return catalog.publish_data_products(
                s.store, c.instance_base, s.datahub, body.ids if body else None
            )
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.post("/api/v1/catalog/columns", dependencies=need(auth.STEWARD), tags=["catalog"])
    def catalog_publish_columns(s: Svc, c: Cfg, body: CatalogPublishIn | None = None) -> dict:
        """Write each column binding onto its DataHub column, so the catalog shows the business
        data element a column holds. `{"ids": [...]}` limits it to those data elements."""
        try:
            return catalog.publish_column_bindings(
                s.store, c.instance_base, s.datahub, body.ids if body else None
            )
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/api/v1/data-elements/suggestions", dependencies=need(auth.STEWARD), tags=["governance"])
    def data_element_suggestions(s: Svc, c: Cfg) -> dict:
        """Columns the catalog marks with a data element's catalog terms (glossary terms or tags, often
        set by a classifier) that are not yet bound to it, for a steward to accept or reject."""
        return suggest.suggestions(s.store, c.instance_base, s.datahub)

    @app.post(
        "/api/v1/data-elements/suggestions/accept", dependencies=need(auth.STEWARD), tags=["governance"]
    )
    def data_element_suggestions_accept(s: Svc, c: Cfg, body: SuggestionsIn) -> dict:
        """Bind the given columns to their data elements (source `catalog-suggestions`)."""
        try:
            return suggest.accept(s.store, c.instance_base, c.ontology_dir, body.items)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post(
        "/api/v1/data-elements/suggestions/reject", dependencies=need(auth.STEWARD), tags=["governance"]
    )
    def data_element_suggestions_reject(s: Svc, c: Cfg, who: Who, body: SuggestionsIn) -> dict:
        """Record that the given columns do not hold those data elements; they are not suggested again."""
        try:
            return suggest.reject(s.store, body.items, by=who.name or who.subject, note=body.note)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/v1/data-elements/coverage", dependencies=need(auth.READ), tags=["governance"])
    def data_element_coverage(
        s: Svc, c: Cfg, dataProduct: str | None = None, criticalOnly: bool = False
    ) -> dict:
        """The data element inventory: for each element, whether it is defined, owned (owner and
        steward roles), sourced, classified, bound to catalog columns and attested; totals for all
        elements and for critical ones. `dataProduct` limits it to one product's elements."""
        try:
            return coverage.inventory(
                s.store, c.instance_base, data_product=dataProduct, critical_only=criticalOnly
            )
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/api/v1/authorities/{rid}/confirmation", dependencies=need(auth.READ), tags=["governance"])
    def authority_confirmation(s: Svc, rid: str) -> dict:
        """Whether an agent may rely on this authority: every fact behind it attested and current."""
        return prov.authority_basis(s.store, rid)

    @app.get("/api/v1/{type_}/{rid}/provenance", dependencies=need(auth.STEWARD), tags=["governance"])
    def entity_provenance(s: Svc, type_: str, rid: str) -> dict:
        """Why we believe this: per field, the winning source, its times and status, and every other claim."""
        return prov.explain(s.store, type_, rid)

    def attester_for(
        s: VeromeshService, c: Settings, who: auth.Caller, given: str | None
    ) -> tuple[str, bool]:
        """Who is attesting: the signed-in person, found in the model; with sign-in off, the request's attester."""
        if c.auth_disabled:
            if not given:
                raise HTTPException(status_code=422, detail="Give the attester's person ID (sign-in is off).")
            return given, False
        if who.kind != "user":
            raise HTTPException(
                status_code=403, detail="Attestations and corrections are made by people, not services."
            )
        value = auth.person_claim(who, c)
        if not value:
            raise HTTPException(
                status_code=403,
                detail=f"Your sign-in carries none of the claims that identify a person ({c.oidc_person_claims}).",
            )
        pid = prov.person_matching(s.store, c.person_match_field, value)
        if pid is None:
            raise HTTPException(
                status_code=403,
                detail=f"Your sign-in ({value}) does not match exactly one person in the model by {c.person_match_field}.",
            )
        if given and given != pid:
            raise HTTPException(
                status_code=403, detail=f"You are signed in as {pid}; you cannot attest as {given}."
            )
        return pid, True

    def agent_for(s: VeromeshService, c: Settings, who: auth.Caller) -> tuple[str | None, str | None]:
        """(the Agent entity the caller is, None) or (None, why not).

        Two ways an agent signs in: as itself (an app-only token, client credentials), or as
        the person it acts for, through the agent's own app registration (a delegated token
        whose azp is the agent's client ID, e.g. a Copilot Studio agent with user sign-in).
        Delegated, the signed-in person must be the agent's principal."""
        if not who.client_id:
            if who.kind == "user":
                return None, "Signed in as a person, not through an agent."
            return None, "The token carries no client ID (azp, appid or client_id)."
        hits = prov.matching(s.store, "agents", "signInClientId", who.client_id)
        if not hits:
            if who.kind == "user":
                return None, "Signed in as a person, not through a registered agent."
            return (
                None,
                f"No agent in the model signs in with client ID {who.client_id}; register it on the agent.",
            )
        if len(hits) > 1:
            return None, f"Several agents sign in with client ID {who.client_id}: {', '.join(hits)}."
        agent = hits[0]
        if who.kind == "user":
            principal = (changesets.get_entity(s.store, "agents", agent, c.instance_base) or {}).get(
                "actsFor"
            )
            value = auth.person_claim(who, c)
            person = prov.person_matching(s.store, c.person_match_field, value) if value else None
            if person is None or person != principal:
                return None, (
                    f"Agent {agent} acts for {principal}; you are signed in as "
                    f"{person or 'nobody in the model'}, so you cannot use it."
                )
        return agent, None

    @app.get("/api/v1/me", dependencies=need(auth.READ), tags=["sign-in"])
    def me(s: Svc, c: Cfg, who: Who) -> dict:
        """Who the caller is: their VeroMesh roles and, in the model, the person they are or the agent
        they are and the one person it acts for, with the authorities the agent holds. Answers are
        need-to-know: an agent sees its principal's roles, not their personal details."""
        out: dict = {"subject": who.subject, "kind": who.kind, "name": who.name, "roles": sorted(who.roles)}
        if c.auth_disabled:
            out["signInDisabled"] = True
            return out

        def entity(type_: str, rid: str) -> dict:
            return auth.need_to_know(who, type_, changesets.get_entity(s.store, type_, rid, c.instance_base))

        if who.kind == "user":
            value = auth.person_claim(who, c)
            pid = prov.person_matching(s.store, c.person_match_field, value) if value else None
            # Your own record, in full: need to know does not hide you from yourself.
            out["person"] = changesets.get_entity(s.store, "people", pid, c.instance_base) if pid else None
            if pid is None:
                out["problem"] = (
                    f"Your sign-in ({value}) does not match exactly one person in the model by {c.person_match_field}."
                    if value
                    else f"Your sign-in carries none of the claims that identify a person ({c.oidc_person_claims})."
                )
                return out
            if not who.client_id or not prov.matching(s.store, "agents", "signInClientId", who.client_id):
                return out  # a person, not through an agent
        aid, why = agent_for(s, c, who)
        out["agent"] = entity("agents", aid) if aid else None
        if aid is None:
            out["problem"] = why
            return out
        principal = out["agent"].get("actsFor")
        out["actsFor"] = entity("people", principal) if principal else None
        out["authorities"] = [
            a
            for a in changesets.list_entities(s.store, "authorities", c.instance_base)
            if a.get("holder") == aid
        ]
        return out

    # ------------------------------------------------------------------ decision log

    @app.get("/api/v1/decisions", dependencies=need(auth.STEWARD), tags=["decisions"])
    def decisions_list(
        dl: Log,
        agent: str | None = None,
        principal: str | None = None,
        action: str | None = None,
        kind: str | None = None,
        refersTo: str | None = None,
        afterSeq: int = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[dict]:
        """Decision log entries in the order they were recorded: decisions (allow, consult, refuse),
        the overrides and outcomes that refer to them, and attestations. Page with afterSeq."""
        return dl.find(
            agent=agent,
            principal=principal,
            action=action,
            kind=kind,
            refers_to=refersTo,
            after_seq=afterSeq,
            limit=limit,
        )

    @app.post("/api/v1/decisions/check", dependencies=need(auth.READ), status_code=201, tags=["decisions"])
    def decisions_check(s: Svc, c: Cfg, dl: Log, who: Who, body: CheckIn) -> dict:
        """Before a consequential action: may this agent take it, for its principal, now?
        Answers allow, consult (with the owners to consult first) or refuse, with reasons, and
        appends the decision to the log. Ask as the agent (its own sign-in); the answer says
        whom to inform, whom to consult (above the agent's limit: the next authority up), and
        whom to escalate to when no authority covers the amount."""
        trusted = c.auth_disabled or "admin" in who.roles
        if c.auth_disabled or (who.kind != "app" and "admin" in who.roles):
            if not body.agent:
                raise HTTPException(
                    status_code=422, detail="Give the agent ID (only an agent's own sign-in implies it)."
                )
            agent, subject = body.agent, who.subject
        else:
            if who.kind == "app" and "agent" not in who.roles:
                raise HTTPException(
                    status_code=403, detail="Decision checks are asked by agents (the agent role)."
                )
            agent, why = agent_for(s, c, who)
            if agent is None and who.kind == "user":
                why = f"Decision checks are asked by agents. {why}"
            if agent is None:
                raise HTTPException(status_code=403, detail=why)
            if body.agent and body.agent != agent:
                raise HTTPException(
                    status_code=403, detail=f"You are signed in as agent {agent}, not {body.agent}."
                )
            subject = who.client_id or who.subject
        on = None
        if body.onDate:
            if not trusted:
                raise HTTPException(
                    status_code=403, detail="Agents are checked as of today; onDate is for administrators."
                )
            on = date.fromisoformat(body.onDate)
        return decide.check(
            s.store,
            c.instance_base,
            dl,
            agent=agent,
            action=body.action,
            subject=subject,
            amount=body.amount,
            currency=body.currency,
            on=on,
            note=body.note,
            require_attested=c.decision_requires_attested,
        )

    def use(request: Request) -> usage.Usage:
        return usage_of(request.app)

    Use = Annotated[usage.Usage, Depends(use)]

    @app.get("/api/v1/licence/status", dependencies=need(auth.READ), tags=["licence"])
    def licence_status(u: Use) -> dict:
        """Where the install stands (unlicensed, licensed, warning, grace, read-only) and the
        message to show: for the banner on every screen."""
        st = u.licensing.status()
        return {k: v for k, v in st.items() if k != "licence"}

    @app.get("/api/v1/licence", dependencies=need(auth.ADMIN), tags=["licence"])
    def licence_overview(u: Use) -> dict:
        """The install's ID and usage key (for ordering a licence), the licence in force and its
        stage, and every licence held with why any is not used. Administrators only."""
        return u.licensing.overview()

    @app.put("/api/v1/licence", dependencies=need(auth.ADMIN), tags=["licence"])
    def licence_load(u: Use, body: Annotated[dict, Body()]) -> dict:
        """Load a licence (the signed document VeroMesh issued). It is checked against the keys
        built into this release and this install's ID and usage key, and kept."""
        return u.licensing.load(body)

    @app.get("/api/v1/usage", dependencies=need(auth.ADMIN), tags=["usage"])
    def usage_overview(u: Use) -> dict:
        """The install, its tier, the usage settings, this month so far (not signed) and every
        report made, with how its sending went. Administrators only."""
        return u.overview()

    @app.get("/api/v1/usage/reports", dependencies=need(auth.ADMIN), tags=["usage"])
    def usage_reports(u: Use) -> list[dict]:
        """Every monthly report made (month, when, hash), oldest first."""
        return u.store.reports()

    @app.get("/api/v1/usage/reports/{month}", dependencies=need(auth.ADMIN), tags=["usage"])
    def usage_report(u: Use, month: str, download: bool = False):
        """A month's signed report, as stored (``download=true``: as a file)."""
        doc = u.store.report(month)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"There is no usage report for {month}.")
        headers = {}
        if download:
            name = f"veromesh-usage-{doc['report']['install']['id']}-{month}.json"
            headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return JSONResponse(content=doc, headers=headers)

    @app.post("/api/v1/usage/reports/{month}/send", dependencies=need(auth.ADMIN), tags=["usage"])
    def usage_send(u: Use, month: str) -> dict:
        """Send a month's report now through the mail adapter, and record how it went."""
        try:
            return u.send(month)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/usage/settings", dependencies=need(auth.ADMIN), tags=["usage"])
    def usage_settings(u: Use) -> dict:
        """The report's time zone (the month boundary) and the mail adapter. Secrets are
        never shown; ``secrets`` says which are set (they come from the environment)."""
        return u.get_settings()

    @app.put("/api/v1/usage/settings", dependencies=need(auth.ADMIN), tags=["usage"])
    def usage_put_settings(u: Use, body: Annotated[dict, Body()]) -> dict:
        """Change ``timeZone`` (an IANA name) and ``mail`` (adapter off, smtp, sendmail or m365;
        ``to``, ``from`` and the adapter's own settings). Only what is given changes."""
        return u.put_settings(body)

    @app.post("/api/v1/usage/settings/test", dependencies=need(auth.ADMIN), tags=["usage"])
    def usage_test_mail(u: Use) -> dict:
        """Send a test message through the mail adapter."""
        return u.send_test()

    @app.post("/api/v1/usage/run", dependencies=need(auth.ADMIN), tags=["usage"])
    def usage_run(u: Use) -> dict:
        """Count now, make the reports that are due and send what waits (the hourly job, now)."""
        return u.run()

    @app.get("/api/v1/decisions/verify", dependencies=need(auth.STEWARD), tags=["decisions"])
    def decisions_verify(dl: Log) -> dict:
        """Walk the whole hash chain: ok, or the first entry that was changed, removed or inserted."""
        return dl.verify().as_dict()

    @app.get("/api/v1/attestations/verify", dependencies=need(auth.STEWARD), tags=["governance"])
    def attestations_verify(s: Svc, dl: Log) -> dict:
        """Check the attestations the model relies on against the decision log's hash chain:
        ok, or the attestations altered, written around the service (unrecorded), or removed
        from the model (missing)."""
        return prov.verify_attestations(s.store, dl)

    @app.get("/api/v1/decisions/inbox", dependencies=need(auth.READ), tags=["decisions"])
    def decisions_inbox(
        s: Svc, c: Cfg, dl: Log, who: Who, person: str | None = None, days: int = 7
    ) -> list[dict]:
        """What waits for the signed-in person (with sign-in off, the one named): consultations to answer,
        escalations above their agent's limit, allowed actions they may stop. The steward screen's list."""
        pid, roles = person_for(s, c, who, person)
        return followup.inbox(dl, person=pid, roles=roles, days=max(1, min(days, 90)))

    @app.get("/api/v1/decisions/measures", dependencies=need(auth.STEWARD), tags=["decisions"])
    def decisions_measures(
        dl: Log, since: str | None = None, until: str | None = None, agent: str | None = None
    ) -> dict:
        """Override rate, consultation precision and delegation extended, from the decision log
        (dates YYYY-MM-DD in the install's time zone, VEROMESH_TIME_ZONE; working definitions in followup.py)."""
        return followup.measures(dl, since=since, until=until, agent=agent)

    def the_decision(dl: decisions.DecisionLog, entry_id: str) -> dict:
        d = dl.get(entry_id)
        if d is None or d["kind"] != "decision":
            raise HTTPException(status_code=404, detail=f"No decision {entry_id} in the log.")
        return d

    def person_for(
        s: VeromeshService, c: Settings, who: auth.Caller, given: str | None
    ) -> tuple[str, list[str]]:
        """The signed-in person (or, with sign-in off, the one named) and the roles they fill."""
        pid, _ = attester_for(s, c, who, given)
        p = changesets.get_entity(s.store, "people", pid, c.instance_base) or {}
        return pid, p.get("rolesFilled", [])

    def may_view(s: VeromeshService, c: Settings, who: auth.Caller, d: dict) -> None:
        """Stewards, the decision's agent, its principal and the people consulted or informed."""
        if c.auth_disabled or who.has(*auth.STEWARD):
            return
        if who.kind == "app" or who.client_id:
            agent, _ = agent_for(s, c, who)
            if agent and agent == d.get("agent"):
                return
        if who.kind == "user":
            value = auth.person_claim(who, c)
            pid = prov.person_matching(s.store, c.person_match_field, value) if value else None
            if pid:
                roles = (changesets.get_entity(s.store, "people", pid, c.instance_base) or {}).get(
                    "rolesFilled", []
                )
                involved = {x["role"] for x in d.get("consult", [])} | {
                    x["role"] for x in (d.get("basis") or {}).get("inform", [])
                }
                if pid == d.get("principal") or involved & set(roles):
                    return
        raise HTTPException(
            status_code=403,
            detail="Only the agent, its principal, the owners involved and stewards see this decision.",
        )

    @app.get("/api/v1/decisions/{entry_id}/status", dependencies=need(auth.READ), tags=["decisions"])
    def decisions_status(s: Svc, c: Cfg, dl: Log, who: Who, entry_id: str) -> dict:
        """Where a decision stands: allowed, pending (waiting for consulted owners), approved, rejected or
        refused; may the agent act; who answered; what the agent did."""
        d = the_decision(dl, entry_id)
        may_view(s, c, who, d)
        return followup.status_of(d, followup.followups(dl, d["id"]))

    @app.post(
        "/api/v1/decisions/{entry_id}/overrides",
        dependencies=need(auth.READ),
        status_code=201,
        tags=["decisions"],
    )
    def decisions_override(s: Svc, c: Cfg, dl: Log, who: Who, entry_id: str, body: OverrideIn) -> dict:
        """A person answers or overrules a decision, as themselves: a consulted owner approves or rejects;
        the principal approves an action above the agent's authority; the principal or an informed
        owner stops an allowed one. Appended to the log."""
        d = the_decision(dl, entry_id)
        if who.kind == "app" and not c.auth_disabled:
            raise HTTPException(
                status_code=403, detail="Answers and overrides are made by people, not services."
            )
        pid, roles = person_for(s, c, who, body.person)
        return followup.override(
            dl,
            d,
            person=pid,
            person_roles=roles,
            is_admin=c.auth_disabled or "admin" in who.roles,
            verdict=body.verdict,
            subject=who.subject,
            role=body.role,
            note=body.note,
        )

    @app.post(
        "/api/v1/decisions/{entry_id}/outcome",
        dependencies=need(auth.READ),
        status_code=201,
        tags=["decisions"],
    )
    def decisions_outcome(s: Svc, c: Cfg, dl: Log, who: Who, entry_id: str, body: OutcomeIn) -> dict:
        """The agent reports what it did (done or not_done), once the decision stands as allowed or approved."""
        d = the_decision(dl, entry_id)
        if not (c.auth_disabled or "admin" in who.roles):
            agent, why = agent_for(s, c, who)
            if agent != d.get("agent"):
                raise HTTPException(
                    status_code=403, detail=why or f"Only {d.get('agent')} reports this outcome."
                )
        return followup.report_outcome(
            dl, d, outcome=body.outcome, subject=who.client_id or who.subject, note=body.note
        )

    @app.get("/api/v1/decisions/{entry_id}", dependencies=need(auth.STEWARD), tags=["decisions"])
    def decisions_get(dl: Log, entry_id: str) -> dict:
        """One entry, with the entries that refer to it (its overrides and outcomes)."""
        e = dl.get(entry_id)
        if e is None:
            raise HTTPException(status_code=404, detail=f"No decision log entry {entry_id}.")
        return {**e, "followedBy": dl.find(refers_to=e["id"], limit=1000)}

    @app.post(
        "/api/v1/{type_}/{rid}/attestations",
        dependencies=need(auth.READ),
        status_code=201,
        tags=["governance"],
    )
    def entity_attest(s: Svc, c: Cfg, dl: Log, who: Who, type_: str, rid: str, body: AttestationIn) -> dict:
        """Confirm or dispute fields (default: all) of what VeroMesh believes, or of one source's values.
        The attester is the signed-in person, who must fill the role they act in. Stewards may attest
        anything; anyone else, only entities whose owner or steward role they act in."""
        attester, signed_in = attester_for(s, c, who, body.attester)
        owner_only = not who.has(*auth.STEWARD)
        return prov.attest(
            s.store,
            c.instance_base,
            type_,
            rid,
            verdict=body.verdict,
            attester=attester,
            role=body.role,
            fields=body.fields,
            source=body.source,
            note=body.note,
            signed_in=signed_in,
            owner_only=owner_only,
            log=dl,
        )

    @app.post(
        "/api/v1/{type_}/{rid}/corrections",
        dependencies=need(auth.STEWARD),
        status_code=201,
        tags=["governance"],
    )
    def entity_correct(s: Svc, c: Cfg, dl: Log, who: Who, type_: str, rid: str, body: CorrectionIn):
        """Steward correction of fields a steward may correct; recorded as the 'steward' source and attested
        by the signed-in person."""
        attester, signed_in = attester_for(s, c, who, body.attester)
        try:
            return prov.correct(
                s.store,
                c.instance_base,
                c.ontology_dir,
                type_,
                rid,
                values=body.values,
                attester=attester,
                role=body.role,
                note=body.note,
                signed_in=signed_in,
                log=dl,
            )
        except changesets.Conflict as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc), "report": exc.report})

    @app.get("/api/v1/{type_}", dependencies=need(auth.READ), tags=["entities"])
    def entity_list(s: Svc, c: Cfg, who: Who, type_: str, source: str | None = None) -> list[dict]:
        """All entities of a type across the model, or in one source. Personal details only to those who need them."""
        rows = changesets.list_entities(s.store, type_, c.instance_base, source)
        return [auth.need_to_know(who, type_, r) for r in rows]

    @app.get("/api/v1/{type_}/{rid}", dependencies=need(auth.READ), tags=["entities"])
    def entity_get(s: Svc, c: Cfg, who: Who, type_: str, rid: str, asOf: str | None = None) -> dict:
        """The entity as VeroMesh believes it now, or as of a date (YYYY-MM-DD) or time. Personal details only to those who need them."""
        return auth.need_to_know(
            who, type_, changesets.get_entity(s.store, type_, rid, c.instance_base, as_of=asOf)
        )

    @app.post("/api/v1/{type_}", dependencies=need(auth.CONNECTOR), tags=["entities"])
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

    @app.put("/api/v1/{type_}/{rid}", dependencies=need(auth.CONNECTOR), tags=["entities"])
    def entity_put(s: Svc, c: Cfg, type_: str, rid: str, source: str, entity: Annotated[dict, Body()]):
        """Create or replace one entity in a source."""
        return _single(s, c, source, [{"type": type_, "op": "upsert", "entity": {**entity, "id": rid}}])

    @app.delete("/api/v1/{type_}/{rid}", dependencies=need(auth.CONNECTOR), tags=["entities"])
    def entity_delete(s: Svc, c: Cfg, type_: str, rid: str, source: str):
        """Delete one entity from a source; refused if something still references it."""
        return _single(s, c, source, [{"type": type_, "op": "delete", "entity": {"id": rid}}])

    # ------------------------------------------------------------------ MCP, for agents

    def mcp_entity(st: dict, type_: str, rid: str) -> dict:
        entities.check_type(type_)
        e = changesets.get_entity(st["service"].store, type_, rid, st["settings"].instance_base)
        return auth.need_to_know(st["caller"], type_, e)

    mcp = mcp_server.build(
        {
            "who_am_i": lambda st: me(st["service"], st["settings"], st["caller"]),
            "check_action": lambda st, **kw: decisions_check(
                st["service"], st["settings"], st["log"], st["caller"], CheckIn(**kw)
            ),
            "impact_of_action": lambda st, action: decide.impact_of(
                st["service"].store, st["settings"].instance_base, action
            ),
            "who_owns": lambda st, type_, rid: decide.owners_of(
                st["service"].store, st["settings"].instance_base, type_, rid
            ),
            "get_entity": mcp_entity,
            "decision_status": lambda st, decision_id: decisions_status(
                st["service"], st["settings"], st["log"], st["caller"], decision_id
            ),
            "report_outcome": lambda st, decision_id, outcome, note: decisions_outcome(
                st["service"],
                st["settings"],
                st["log"],
                st["caller"],
                decision_id,
                OutcomeIn(outcome=outcome, note=note),
            ),
        }
    )
    mcp.streamable_http_app()  # creates the session manager the lifespan runs
    app.state.mcp = mcp

    class McpEndpoint:
        """/mcp: sign in as for every route (any VeroMesh role), then hand over to the MCP transport."""

        def __init__(self, inner) -> None:
            self.inner = inner

        async def __call__(self, scope, receive, send) -> None:
            request = Request(scope, receive)
            try:
                c = cfg(request)
                who = auth.current_caller(request, await auth.bearer(request), c)
                auth.forbid(who, *auth.READ)
                state = {"service": svc(request), "settings": c, "log": dlog(request, c), "caller": who}
            except HTTPException as exc:
                await JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)(
                    scope, receive, send
                )
                return
            scope.setdefault("state", {})["veromesh"] = state
            await self.inner(scope, receive, send)

    app.router.routes.append(
        Route(
            "/mcp",
            endpoint=McpEndpoint(StreamableHTTPASGIApp(mcp.session_manager)),
            methods=["GET", "POST", "DELETE"],
        )
    )
    return app


app = create_app()
