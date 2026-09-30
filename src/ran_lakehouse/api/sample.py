"""Recorded sample export (rule A4): a small fixed dataset and recorded
API responses at the current contract version, for downstream tests.

The cells are about 30 around three planted faults of different kinds in
one two-week window, but nothing in the sample says which cells or which
kinds (rule A3): no fault, cause or right fix is written. Plan-solve calls
use constraint sets written for the sample, never a card's implied set,
and what-if calls change only cells no fault touches, never with a right
fix; the export fails if either rule breaks.
"""

import copy
import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from ran_lakehouse.api import queries
from ran_lakehouse.api.app import attach, create_app
from ran_lakehouse.api.contract import CONTRACT_VERSION, VERSION_HEADER
from ran_lakehouse.api.models import SYNTHETIC_NOTICE
from ran_lakehouse.api.serve import live_services
from ran_lakehouse.faults.evaluate import right_fix
from ran_lakehouse.faults.plant import Fault, plan_faults
from ran_lakehouse.model import RUN_START, NetworkModel, default_model
from ran_lakehouse.planning.scenarios import BILLION, MILLION, constraint_set, scenarios
from ran_lakehouse.planning.solver import BACKEND, PlanningData, solve_plan
from ran_lakehouse.world import build_world

logger = logging.getLogger(__name__)

WINDOW_DAYS = 14
FAULTS = 3
CELLS_PER_FAULT = 10  # the faulty cell and the 9 nearest cells (START)
CARDS = ("S01", "S05", "S09", "S13", "S17")
MAX_BYTES = 20_000_000  # rule A4 START: about 20 MB
LTE_KPI = "LTE_ERAB_DROP"
GSM_KPI = "GSM_TCH_BLOCK"
WORST_KPIS = ("LTE_ERAB_DROP", "LTE_PRB_UTIL")
# Constraint sets written for the sample (never a card's implied set).
PLANS = (
    constraint_set("south-west", "max_persons", "LTE", max_sites=4),
    constraint_set("north-east", "min_capex", "GSM", share=0.6),
    constraint_set("whole", "max_villages", "both", max_sites=7, opex=90 * MILLION),
    constraint_set("central", "max_persons", "GSM", capex=12 * BILLION, fiber_km=2.5),
    constraint_set("south", "min_capex", "LTE", share=0.97, max_sites=2),
)


def window_faults(faults: list[Fault], last_day: int) -> tuple[int, list[Fault]]:
    """The first two-week window (Monday start) holding faults of three kinds.

    Args:
        faults: The run's fault plan.
        last_day: Last day index gold holds.

    Returns:
        (first day index, the earliest fault of each of the first three
        kinds to start in the window).

    Raises:
        RuntimeError: If no window inside gold holds three kinds.
    """
    for first in range(0, last_day - WINDOW_DAYS + 2, 7):
        lo = RUN_START + timedelta(days=first)
        hi = lo + timedelta(days=WINDOW_DAYS)
        chosen: dict[str, Fault] = {}
        for f in sorted(faults, key=lambda f: f.start):
            if lo <= f.start < hi and f.end <= hi and f.kind not in chosen:
                chosen[f.kind] = f
        if len(chosen) >= FAULTS:
            return first, list(chosen.values())[:FAULTS]
    raise RuntimeError(f"no {WINDOW_DAYS}-day window in gold holds {FAULTS} fault kinds")


def sample_cells(base: NetworkModel, faults: list[Fault]) -> list[int]:
    """The faulty cells and the nearest cells around each, of both technologies.

    Args:
        base: The network as built.
        faults: The sample's faults.

    Returns:
        Global cell indices, ascending.
    """
    x, y = base.state.x_km, base.state.y_km
    cells: set[int] = set()
    for f in faults:
        distance = np.hypot(x - x[f.cell], y - y[f.cell])
        # Stable order: by distance, then index, so ties never reorder.
        nearest = np.lexsort((np.arange(len(distance)), distance))
        cells |= {int(i) for i in nearest[:CELLS_PER_FAULT]}
    return sorted(cells)


def what_if_changes(
    base: NetworkModel, faults: list[Fault], cells: list[int]
) -> list[list[dict[str, Any]]]:
    """What-if calls on cells no fault touches.

    Args:
        base: The network as built.
        faults: Every fault of the run.
        cells: The sample's cells.

    Returns:
        Change lists, the last one out of bound on purpose.

    Raises:
        RuntimeError: If the sample has too few untouched LTE cells.
    """
    touched = {f.cell for f in faults} | {f.target for f in faults if f.target >= 0}
    names = base.state.cell_names
    free = [i for i in cells if i not in touched and base.state.technology[i] == "LTE"]
    if len(free) < 4:
        raise RuntimeError("the sample has fewer than 4 LTE cells no fault touches")
    pair = next(
        ((a, b) for a in free for b in free if a != b and (a, b) not in base.neighbours),
        None,
    )
    if pair is None:
        raise RuntimeError("no unconfigured relation between untouched sample cells")

    def change(kind: str, cell: int, delta: float, target: int | None) -> dict[str, Any]:
        return {
            "kind": kind,
            "cell": names[cell],
            "delta": delta,
            "target": None if target is None else names[target],
        }

    return [
        [change("tilt", free[0], 1.0, None)],
        [change("power", free[1], -2.0, None)],
        [change("cio", free[2], 2.0, None), change("cio", free[2], 2.0, None)],
        [change("add_neighbour", pair[0], 0.0, pair[1])],
        [change("tilt", free[3], 3.0, None)],
    ]


def check_no_answers(
    base: NetworkModel,
    faults: list[Fault],
    changes: list[list[dict[str, Any]]],
    planning: PlanningData,
    constraint_sets: list[dict[str, Any]],
    plans: list[dict[str, Any]],
) -> None:
    """Fail if a recorded call equals a planted answer.

    Args:
        base: The network as built.
        faults: Every fault of the run.
        changes: The what-if calls.
        planning: Planning data.
        constraint_sets: The plan-solve calls' constraint sets.
        plans: Their results.

    Raises:
        RuntimeError: If a what-if call carries a step of a right fix, or a
            constraint set or plan equals a card's implied set or optimum.
    """
    names = base.state.cell_names
    fixes = {
        (c.kind, names[c.cell], names[c.target] if c.target >= 0 else None)
        for f in faults
        for c in right_fix(base, f)
    }
    for call in changes:
        for c in call:
            if (c["kind"], c["cell"], c["target"]) in fixes:
                raise RuntimeError(f"what-if call {c} is a step of a planted right fix")
    cards = scenarios(planning)
    implied = [card.constraints for card in cards]
    optima = [solve_plan(planning, card.constraints, BACKEND) for card in cards]
    for constraints, plan in zip(constraint_sets, plans, strict=True):
        if constraints in implied:
            raise RuntimeError(f"sample constraint set equals a card's: {constraints}")
        for optimum in optima:
            if plan["status"] == "optimal" and plan.get("sites") == optimum.get("sites"):
                raise RuntimeError("a sample plan equals a card's optimum")


def write_table(rows: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    """Write rows as Parquet.

    Args:
        rows: Rows (at least one).
        path: Output file.

    Returns:
        Rows, bytes and SHA-256 of the file.

    Raises:
        RuntimeError: For an empty table.
    """
    if not rows:
        raise RuntimeError(f"{path.name} would be empty")
    pq.write_table(pa.Table.from_pylist(rows), path)
    data = path.read_bytes()
    return {"rows": len(rows), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def gold_rows(
    con: duckdb.DuckDBPyConnection, table: str, column: str, cells: list[str], lo: Any, hi: Any
) -> list[dict[str, Any]]:
    """Rows of a gold KPI table for some cells over a range.

    Args:
        con: DuckDB with the lake as "lk".
        table: Gold table.
        column: Period column.
        cells: Cell names.
        lo: First period, inclusive.
        hi: Last period, exclusive.

    Returns:
        Rows.
    """
    sql = (
        f"SELECT * FROM lk.gold.{table} WHERE list_contains($cells, cell_name) "
        f"AND {column} >= $lo AND {column} < $hi ORDER BY cell_name, kpi_id, {column}"
    )
    return queries.rows(con, sql, {"cells": cells, "lo": lo, "hi": hi})


def export_sample(
    warehouse: str, profile: str, run_weeks: int, runs: Path, out: Path
) -> dict[str, Any]:
    """Write the sample.

    Args:
        warehouse: Lakekeeper warehouse name.
        profile: World profile of the run.
        run_weeks: Weeks of the run.
        runs: Directory of run status files.
        out: Output directory (created; must be empty if it exists).

    Returns:
        The manifest.

    Raises:
        RuntimeError: If out is not empty or the sample exceeds MAX_BYTES.
    """
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f"{out} is not empty")
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "responses").mkdir()
    base = default_model(build_world(profile))
    services = live_services(warehouse, base, run_weeks, runs)
    con = services.cursor()
    last = queries.lake_clock(con)["gold_latest_day"]
    faults = plan_faults(base, run_weeks)
    first, chosen = window_faults(faults, (last - RUN_START.date()).days)
    names = base.state.cell_names
    cells = sorted(names[i] for i in sample_cells(base, chosen))
    changes = what_if_changes(base, faults, sample_cells(base, chosen))
    day0 = (RUN_START + timedelta(days=first)).date()
    day1 = day0 + timedelta(days=WINDOW_DAYS)
    t0 = queries_start(day0)
    t1 = queries_start(day1)
    logger.info("sample: %d cells, %s to %s", len(cells), day0, day1)

    in_sample = [c for c in services.cells if c["cell_name"] in cells]
    tables: dict[str, list[dict[str, Any]]] = {
        "cells": in_sample,
        "relations": [
            r for r in services.relations if r["source"] in cells and r["target"] in cells
        ],
        "kpi_catalog": queries.kpi_catalog(con),
        "lte_kpi_day": gold_rows(con, "lte_kpi_day", "day", cells, day0, day1),
        "lte_kpi_hour": gold_rows(con, "lte_kpi_hour", "period_start", cells, t0, t1),
        "gsm_kpi_day": gold_rows(con, "gsm_kpi_day", "day", cells, day0, day1),
        "gsm_kpi_hour": gold_rows(con, "gsm_kpi_hour", "period_start", cells, t0, t1),
        "worst_cells_week": queries.rows(
            con,
            "SELECT * FROM lk.gold.worst_cells_week WHERE week_start >= $lo AND week_start < $hi "
            "ORDER BY week_start, kpi_id, rank",
            {"lo": day0, "hi": day1},
        ),
        "cm_snapshot": [
            {"cell_name": c} | x for c in cells for x in queries.cm_snapshot(con, c, t0)
        ],
        "cm_changes": [x for x in queries.cm_changes(con, t0, t1, None) if x["cell_name"] in cells],
        "alarms": [x for x in queries.alarms(con, t0, t1, None) if x["cell_name"] in cells],
        "scenario_cards": [
            x
            for x in queries.planning_table(con, "planning_scenarios")
            if x["scenario_id"] in CARDS
        ],
    }
    written = {
        name: write_table(rows, out / "data" / f"{name}.parquet") for name, rows in tables.items()
    }

    client = TestClient(attach(create_app(), services), raise_server_exceptions=True)
    by_tech = {c["cell_name"]: c["technology"] for c in in_sample}
    requests: list[tuple[str, str, str, Any]] = [
        ("clock", "GET", "/v1/clock", None),
        ("kpi_catalog", "GET", "/v1/kpi-catalog", None),
        ("cells", "GET", "/v1/cells", None),
        ("topology", "GET", "/v1/topology", None),
    ]
    for c in cells:
        kpi = LTE_KPI if by_tech[c] == "LTE" else GSM_KPI
        requests.append(("cell_" + c, "GET", f"/v1/cells/{c}", None))
        requests.append(
            (
                f"kpis_day_{c}",
                "GET",
                f"/v1/kpis?cell={c}&kpi_id={kpi}&formula_version=1&granularity=day"
                f"&start={day0}&end={day1}",
                None,
            )
        )
        requests.append(
            (
                f"kpis_hour_{c}",
                "GET",
                f"/v1/kpis?cell={c}&kpi_id={kpi}&formula_version=1&granularity=hour"
                f"&start={iso(t0)}&end={iso(t1)}",
                None,
            )
        )
        requests.append(("cm_snapshot_" + c, "GET", f"/v1/cm/snapshot?cell={c}&at={iso(t0)}", None))
    for week in (day0, day0 + timedelta(days=7)):
        for kpi in WORST_KPIS:
            requests.append(
                (
                    f"worst_{kpi}_{week}",
                    "GET",
                    f"/v1/worst-cells?week_start={week}&kpi_id={kpi}&formula_version=1",
                    None,
                )
            )
    requests += [
        ("cm_changes", "GET", f"/v1/cm/changes?start={iso(t0)}&end={iso(t1)}", None),
        ("alarms", "GET", f"/v1/alarms?start={iso(t0)}&end={iso(t1)}", None),
        (
            "quality_events_day1",
            "GET",
            f"/v1/quality-events?start={iso(t0)}&end={iso(t0 + timedelta(days=1))}",
            None,
        ),
        (
            "lineage",
            "GET",
            f"/v1/lineage?kpi_id={LTE_KPI}&formula_version=1&cell={first_lte(cells, by_tech)}"
            f"&granularity=hour&period_start={iso(t0 + timedelta(hours=12))}",
            None,
        ),
        ("planning_villages", "GET", "/v1/planning/villages", None),
        ("planning_candidate_sites", "GET", "/v1/planning/candidate-sites", None),
        ("planning_coverage", "GET", "/v1/planning/coverage", None),
        ("planning_backhaul_power_options", "GET", "/v1/planning/backhaul-power-options", None),
        ("planning_scenarios", "GET", "/v1/planning/scenarios", None),
    ]
    requests += [
        (f"plan_{k + 1}", "POST", "/v1/plan", copy.deepcopy(p)) for k, p in enumerate(PLANS)
    ]
    requests += [
        (f"what_if_{k + 1}", "POST", "/v1/what-if", {"changes": c}) for k, c in enumerate(changes)
    ]

    recorded = []
    plans = []
    for k, (name, method, path, body) in enumerate(requests):
        r = client.request(method, path, json=body)
        if r.headers[VERSION_HEADER] != CONTRACT_VERSION:
            raise RuntimeError(f"{path} answered without the contract version")
        response = r.json()
        if name.startswith("plan_"):
            plans.append(response["data"])
        file = out / "responses" / f"{k:03d}_{safe(name)}.json"
        record = {
            "request": {"method": method, "path": path, "body": body},
            "status": r.status_code,
            "headers": {VERSION_HEADER: r.headers[VERSION_HEADER]},
            "response": response,
        }
        file.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
        recorded.append(
            {"file": file.name, "method": method, "path": path, "status": r.status_code}
        )
    check_no_answers(base, faults, changes, services.planning, list(PLANS), plans)

    manifest: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "notice": SYNTHETIC_NOTICE,
        "contains": (
            "a fixed dataset of about 30 cells over two weeks (daily and hourly KPIs, CM, "
            "alarms, worst cells) and recorded API responses; no planted answers"
        ),
        "warehouse": warehouse,
        "profile": profile,
        "window_wib": {"first_day": str(day0), "end_day_exclusive": str(day1)},
        "cells": cells,
        "tables": written,
        "responses": recorded,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    manifest["bytes"] = total
    if total > MAX_BYTES:
        raise RuntimeError(f"sample is {total} bytes, over {MAX_BYTES}")
    return {k: manifest[k] for k in ("contract_version", "window_wib", "bytes")} | {
        "cells": len(cells),
        "tables": {k: v["rows"] for k, v in written.items()},
        "responses": len(recorded),
    }


def queries_start(day: date) -> datetime:
    """The UTC instant a WIB day starts.

    Args:
        day: WIB date.

    Returns:
        Aware UTC datetime.
    """
    return datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(hours=7)


def iso(t: datetime) -> str:
    """A UTC time as the API takes it.

    Args:
        t: Aware time.

    Returns:
        ISO text ending in Z.
    """
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def first_lte(cells: list[str], technology: dict[str, str]) -> str:
    """The first LTE cell of the sample.

    Args:
        cells: Cell names.
        technology: Cell name to technology.

    Returns:
        The cell.

    Raises:
        RuntimeError: If the sample has no LTE cell.
    """
    for c in cells:
        if technology[c] == "LTE":
            return c
    raise RuntimeError("the sample has no LTE cell")


def safe(name: str) -> str:
    """A name fit for a file.

    Args:
        name: Name.

    Returns:
        Letters, digits, "-" and "_" only.
    """
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
