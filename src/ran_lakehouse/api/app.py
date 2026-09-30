"""The HTTP API (rules A1 to A3), on FastAPI.

FastAPI generates the OpenAPI document from the route and body types, so
the committed contract (contract/openapi.json) is the code's own
description of itself, and a test fails when they drift. Every response,
errors included, carries the contract version in the X-Contract-Version
header and in its body, with the synthetic-data notice.

The API reads the lake through a DuckDB connection with the warehouse
attached as "lk" and never touches the evaluation schema (rule A3).
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import duckdb
import jsonschema
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ran_lakehouse.api import models as m
from ran_lakehouse.api import queries
from ran_lakehouse.api.contract import CONTRACT_VERSION, VERSION_HEADER, plan_schema
from ran_lakehouse.api.whatif import WhatIfEngine
from ran_lakehouse.lake.lineage import lineage
from ran_lakehouse.planning.solver import BACKEND, PlanningData, solve_plan

logger = logging.getLogger(__name__)

# Longest time range of one event query (ASSUMPTION); longer ranges are
# refused and asked for in pieces.
MAX_EVENT_RANGE = timedelta(days=31)
PLANNING_TABLES = ("villages", "candidate_sites", "coverage", "backhaul_power_options")


class ErrorEnvelope(m.Strict):
    """Every error response."""

    contract_version: str
    notice: str
    error: m.ErrorBody


ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Request outside what one answer serves"},
    404: {"model": ErrorEnvelope, "description": "No such cell, KPI or value"},
    422: {"model": ErrorEnvelope, "description": "Invalid or out-of-bound request"},
}


@dataclass
class Services:
    """What the routes read.

    Attributes:
        con: DuckDB with the warehouse attached as "lk".
        cells: Every cell (queries.cells), loaded once.
        relations: Neighbour relations of the latest CM snapshot.
        planning: Planning data for the plan solve.
        whatif: The what-if engine.
        status_path: The live run's status file (may not exist).
    """

    con: duckdb.DuckDBPyConnection
    cells: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    planning: PlanningData
    whatif: WhatIfEngine
    status_path: Path

    @classmethod
    def load(
        cls, con: duckdb.DuckDBPyConnection, whatif: WhatIfEngine, status_path: Path
    ) -> "Services":
        """Read what the routes hold in memory.

        Args:
            con: DuckDB with the warehouse attached as "lk".
            whatif: The what-if engine.
            status_path: The live run's status file.

        Returns:
            The services.
        """
        tables = {
            name: con.execute(f"SELECT * FROM lk.gold.{name}").to_arrow_table()
            for name in PLANNING_TABLES
        }
        return cls(
            con=con,
            cells=queries.cells(con),
            relations=queries.relations(con),
            planning=PlanningData.from_tables(tables),
            whatif=whatif,
            status_path=status_path,
        )

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """A cursor for one request (DuckDB connections are not shared by threads).

        Returns:
            The cursor, reading timestamps in UTC.
        """
        cur = self.con.cursor()
        cur.execute("SET TimeZone = 'UTC'")
        return cur

    def cell(self, name: str) -> dict[str, Any]:
        """One cell.

        Args:
            name: Cell name.

        Returns:
            The cell.

        Raises:
            HTTPException: 404 for an unknown cell.
        """
        for c in self.cells:
            if c["cell_name"] == name:
                return c
        raise HTTPException(404, f"unknown cell {name}")


def envelope[T](data: T) -> m.Envelope[T]:
    """Wrap data with the contract version and the notice.

    Args:
        data: The data.

    Returns:
        The response body.
    """
    return m.Envelope[T](contract_version=CONTRACT_VERSION, notice=m.SYNTHETIC_NOTICE, data=data)


def error_response(status: int, message: str) -> JSONResponse:
    """An error in the contract's shape.

    Args:
        status: HTTP status.
        message: What went wrong.

    Returns:
        The response.
    """
    body = ErrorEnvelope(
        contract_version=CONTRACT_VERSION,
        notice=m.SYNTHETIC_NOTICE,
        error=m.ErrorBody(status=status, error=message),
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


def services(request: Request) -> Services:
    """The app's services.

    Args:
        request: The request.

    Returns:
        The services.

    Raises:
        RuntimeError: If the app was created without services attached.
    """
    found = getattr(request.app.state, "services", None)
    if not isinstance(found, Services):
        raise RuntimeError("the app has no services attached (see attach())")
    return found


def aware(value: datetime, name: str) -> datetime:
    """Require a time with an offset.

    Args:
        value: The time.
        name: Parameter name, for the error.

    Returns:
        The time in UTC.

    Raises:
        HTTPException: 422 for a naive time.
    """
    if value.tzinfo is None:
        raise HTTPException(422, f"{name} needs a UTC offset, for example 2026-01-14T00:00:00Z")
    return value.astimezone(UTC)


def event_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """A checked event time range.

    Args:
        start: Start.
        end: End.

    Returns:
        (start, end) in UTC.

    Raises:
        HTTPException: 422 for an empty range, 400 for one too long.
    """
    lo, hi = aware(start, "start"), aware(end, "end")
    if hi <= lo:
        raise HTTPException(422, "end must be after start")
    if hi - lo > MAX_EVENT_RANGE:
        raise HTTPException(400, f"ranges are at most {MAX_EVENT_RANGE.days} days; ask in pieces")
    return lo, hi


def period(granularity: str, text: str, name: str) -> datetime | date:
    """A KPI period from its text.

    Args:
        granularity: "15m", "hour", "day" or "week".
        text: ISO datetime with offset (15m, hour) or ISO date (day, week).
        name: Parameter name, for the error.

    Returns:
        The period.

    Raises:
        HTTPException: 422 when the text does not fit the granularity.
    """
    try:
        if granularity in ("15m", "hour"):
            return aware(datetime.fromisoformat(text), name)
        return date.fromisoformat(text)
    except ValueError as e:
        kind = "an ISO datetime with offset" if granularity in ("15m", "hour") else "an ISO date"
        raise HTTPException(422, f"{name} must be {kind} for {granularity}: {e}") from e


def definition(s: Services, kpi_id: str, version: int, granularity: str) -> dict[str, Any]:
    """A KPI formula version that exists at a granularity.

    Args:
        s: Services.
        kpi_id: KPI id.
        version: Formula version.
        granularity: Granularity.

    Returns:
        The catalog row.

    Raises:
        HTTPException: 404 for an unknown KPI version, 422 for a granularity
            it does not have.
    """
    found = [
        k
        for k in queries.kpi_catalog(s.cursor())
        if k["kpi_id"] == kpi_id and k["formula_version"] == version
    ]
    if not found:
        raise HTTPException(404, f"no KPI {kpi_id} version {version}")
    if granularity not in found[0]["granularities"].split(","):
        raise HTTPException(422, f"{kpi_id} has no {granularity} values")
    return found[0]


Granularity = Literal["15m", "hour", "day", "week"]


def build_router() -> APIRouter:
    """The v1 routes.

    Returns:
        The router.
    """
    r = APIRouter(prefix="/v1", responses=ERRORS)

    @r.get("/clock", summary="Clock status: the live run and how far each layer has got")
    def clock(request: Request) -> m.Envelope[m.ClockStatus]:
        s = services(request)
        live = None
        if s.status_path.exists():
            status = json.loads(s.status_path.read_text())
            live = m.LiveRun(**{k: status[k] for k in m.LiveRun.model_fields})
        return envelope(
            m.ClockStatus(
                live_run=live,
                **queries.lake_clock(s.cursor()),
                what_if_week_start=s.whatif.week_start,
            )
        )

    @r.get("/topology", summary="Sites, their cells and the configured neighbour relations")
    def topology(request: Request) -> m.Envelope[m.Topology]:
        s = services(request)
        sites: dict[str, m.Site] = {}
        for c in s.cells:
            site = sites.setdefault(
                c["site"], m.Site(site=c["site"], ems=c["ems"], vendor=c["vendor"], cells=[])
            )
            site.cells.append(c["cell_name"])
        return envelope(
            m.Topology(
                sites=[sites[k] for k in sorted(sites)],
                relations=[m.Relation(**x) for x in s.relations],
            )
        )

    @r.get("/cells", summary="Cells with vendor, technology, band, N_RB and DN")
    def cells(
        request: Request,
        technology: Literal["LTE", "GSM"] | None = None,
        vendor: str | None = None,
        band: str | None = None,
    ) -> m.Envelope[list[m.Cell]]:
        chosen = [
            m.Cell(**c)
            for c in services(request).cells
            if (technology is None or c["technology"] == technology)
            and (vendor is None or c["vendor"] == vendor)
            and (band is None or c["band"] == band)
        ]
        return envelope(chosen)

    @r.get("/cells/{cell_name}", summary="One cell")
    def cell(request: Request, cell_name: str) -> m.Envelope[m.Cell]:
        return envelope(m.Cell(**services(request).cell(cell_name)))

    @r.get("/kpi-catalog", summary="Every KPI formula version (rule L3)")
    def catalog(request: Request) -> m.Envelope[list[m.KpiDefinition]]:
        rows = queries.kpi_catalog(services(request).cursor())
        return envelope([m.KpiDefinition(**k) for k in rows])

    @r.get("/kpis", summary="KPI values of one cell, KPI and formula version over a range")
    def kpis(
        request: Request,
        cell: str,
        kpi_id: str,
        formula_version: int,
        granularity: Granularity,
        start: Annotated[str, Query(description="First period, inclusive")],
        end: Annotated[str, Query(description="Last period, exclusive")],
    ) -> m.Envelope[m.KpiSeries]:
        s = services(request)
        c = s.cell(cell)
        k = definition(s, kpi_id, formula_version, granularity)
        if k["technology"] != c["technology"]:
            raise HTTPException(
                422, f"{kpi_id} is a {k['technology']} KPI; {cell} is {c['technology']}"
            )
        lo, hi = period(granularity, start, "start"), period(granularity, end, "end")
        if hi <= lo:
            raise HTTPException(422, "end must be after start")
        rows = queries.kpis(
            s.cursor(), c["technology"].lower(), granularity, cell, kpi_id, formula_version, lo, hi
        )
        if len(rows) > queries.MAX_KPI_ROWS:
            raise HTTPException(
                400, f"more than {queries.MAX_KPI_ROWS} values; narrow the range or coarsen"
            )
        return envelope(
            m.KpiSeries(
                cell_name=cell,
                kpi_id=kpi_id,
                formula_version=formula_version,
                granularity=granularity,
                values=[m.KpiValue(**v) for v in rows],
            )
        )

    @r.get("/worst-cells", summary="Worst-cell ranking of one week and KPI")
    def worst(
        request: Request,
        week_start: Annotated[date, Query(description="Monday of the week (WIB date)")],
        kpi_id: str,
        formula_version: int,
    ) -> m.Envelope[list[m.WorstCell]]:
        s = services(request)
        definition(s, kpi_id, formula_version, "week")
        if week_start.weekday() != 0:
            raise HTTPException(422, "week_start must be a Monday")
        rows = queries.worst_cells(s.cursor(), week_start, kpi_id, formula_version)
        return envelope([m.WorstCell(**w) for w in rows])

    @r.get("/cm/snapshot", summary="One cell's CM (and its relations) at a time")
    def cm_snapshot(request: Request, cell: str, at: datetime) -> m.Envelope[list[m.CmObject]]:
        s = services(request)
        s.cell(cell)
        rows = queries.cm_snapshot(s.cursor(), cell, aware(at, "at"))
        if not rows:
            raise HTTPException(404, f"no CM snapshot of {cell} at or before {at.isoformat()}")
        return envelope(
            [m.CmObject(**(x | {"attributes": json.loads(x["attributes"])})) for x in rows]
        )

    @r.get("/cm/changes", summary="CM change log in a time range")
    def cm_changes(
        request: Request, start: datetime, end: datetime, cell: str | None = None
    ) -> m.Envelope[list[m.CmChange]]:
        s = services(request)
        if cell is not None:
            s.cell(cell)
        lo, hi = event_range(start, end)
        return envelope([m.CmChange(**x) for x in queries.cm_changes(s.cursor(), lo, hi, cell)])

    @r.get("/alarms", summary="Alarm notifications in a time range")
    def alarms(
        request: Request, start: datetime, end: datetime, cell: str | None = None
    ) -> m.Envelope[list[m.Alarm]]:
        s = services(request)
        if cell is not None:
            s.cell(cell)
        lo, hi = event_range(start, end)
        return envelope([m.Alarm(**x) for x in queries.alarms(s.cursor(), lo, hi, cell)])

    @r.get("/quality-events", summary="Delivery problems the pipeline recorded")
    def quality(
        request: Request, start: datetime, end: datetime
    ) -> m.Envelope[list[m.QualityEvent]]:
        lo, hi = event_range(start, end)
        rows = queries.quality_events(services(request).cursor(), lo, hi)
        return envelope([m.QualityEvent(**x) for x in rows])

    @r.get("/lineage", summary="The silver rows and source files behind one KPI value")
    def trace(
        request: Request,
        kpi_id: str,
        formula_version: int,
        cell: str,
        granularity: Granularity,
        period_start: Annotated[
            str, Query(description="UTC start (ISO, with offset) for 15m and hour; WIB date else")
        ],
    ) -> m.Envelope[list[m.LineageRow]]:
        s = services(request)
        s.cell(cell)
        definition(s, kpi_id, formula_version, granularity)
        at = period(granularity, period_start, "period_start")
        try:
            rows = lineage(s.cursor(), kpi_id, formula_version, cell, granularity, at)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if not rows:
            raise HTTPException(404, "gold has no such value")
        return envelope([m.LineageRow(**x) for x in rows])

    def planning_route(name: str, model: type[m.Strict], summary: str) -> None:
        path = "/planning/" + name.replace("_", "-")

        def read(request: Request) -> m.Envelope[list[Any]]:
            rows = queries.planning_table(services(request).cursor(), name)
            return envelope([model(**x) for x in rows])

        r.add_api_route(
            path,
            read,
            methods=["GET"],
            summary=summary,
            response_model=m.Envelope[list[model]],  # type: ignore[valid-type]
            name=f"planning_{name}",
        )

    planning_route("villages", m.Village, "Villages of the expansion area")
    planning_route("candidate_sites", m.CandidateSite, "Candidate sites")
    planning_route("coverage", m.Coverage, "Indoor coverage of each village from each candidate")
    planning_route("backhaul_power_options", m.BackhaulPowerOption, "Backhaul and power options")

    @r.get("/planning/scenarios", summary="Scenario cards: id, area and the request as written")
    def scenarios(request: Request) -> m.Envelope[list[m.ScenarioCard]]:
        rows = queries.planning_table(services(request).cursor(), "planning_scenarios")
        return envelope([m.ScenarioCard(**x) for x in rows])

    @r.post(
        "/plan",
        summary="Solve a constraint set (contract/plan_constraints.schema.json)",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/PlanConstraints"}}
                },
            }
        },
    )
    async def plan(request: Request) -> m.Envelope[m.Plan]:
        s = services(request)
        try:
            constraints = await request.json()
        except json.JSONDecodeError as e:
            raise HTTPException(422, f"body is not JSON: {e}") from e
        try:
            jsonschema.validate(constraints, plan_schema())
        except jsonschema.ValidationError as e:
            where = "/".join(str(p) for p in e.absolute_path) or "body"
            raise HTTPException(422, f"{where}: {e.message}") from e
        result = await run_in_threadpool(solve_plan, s.planning, constraints, BACKEND)
        return envelope(m.Plan(**result))

    @r.post("/what-if", summary="Next week's KPIs before and after bounded changes (rule M6)")
    def what_if(request: Request, body: m.WhatIfRequest) -> m.Envelope[m.WhatIfResult]:
        engine = services(request).whatif
        try:
            changes = engine.changes([(c.kind, c.cell, c.delta, c.target) for c in body.changes])
            per_cell, total = engine.run(changes)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return envelope(
            m.WhatIfResult(
                week_start=engine.week_start,
                changes=body.changes,
                cells=[m.CellKpis(**x) for x in per_cell],  # type: ignore[arg-type]
                touched_cells_total=m.CellKpis(**total),  # type: ignore[arg-type]
            )
        )

    return r


def create_app() -> FastAPI:
    """The API, without services (attach() adds them; openapi() needs none).

    Returns:
        The app.
    """
    app = FastAPI(
        title="ran-lakehouse API",
        version=CONTRACT_VERSION,
        # No OpenTelemetry export, whatever OTEL_* variables the host sets.
        telemetry={"tracing": False, "metrics": False, "logs": False, "auto_configure": False},
        description=(
            "KPIs, configuration, alarms, topology, planning and what-if of a synthetic "
            "4G and 2G network. " + m.SYNTHETIC_NOTICE + "."
        ),
    )
    app.include_router(build_router())

    @app.middleware("http")
    async def version_header(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers[VERSION_HEADER] = CONTRACT_VERSION
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(exc.status_code, str(exc.detail))

    @app.exception_handler(Exception)
    async def failed(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("%s %s failed", request.method, request.url.path)
        return error_response(500, f"internal error: {type(exc).__name__}: {exc}")

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        parts = [f"{'/'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return error_response(422, "; ".join(parts))

    def document() -> dict[str, Any]:
        if app.openapi_schema is None:
            from fastapi.openapi.utils import get_openapi

            doc = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
            )
            doc["components"]["schemas"]["PlanConstraints"] = plan_schema()
            app.openapi_schema = doc
        return app.openapi_schema

    app.openapi = document  # type: ignore[method-assign]
    return app


def attach(app: FastAPI, s: Services) -> FastAPI:
    """Give an app its services.

    Args:
        app: The app.
        s: Services.

    Returns:
        The app.
    """
    app.state.services = s
    return app
