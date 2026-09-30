"""The planning data of the expansion area and its gold tables (rules G1 to G7).

Everything is generated from the world's seed: terrain, villages with what
the served network delivers there today, the utility lines, candidate
sites, coverage from every candidate to every village, and backhaul and
power options. Service coverage is indoor (radio.INDOOR_MARGIN_DB above
the threshold); the outdoor levels are kept. The gold tables are written
directly (they are generated reference data, not transforms of silver).
"""

from dataclasses import asdict, dataclass

import duckdb
import numpy as np
import pyarrow as pa

from ran_lakehouse.lake.catalog import write
from ran_lakehouse.model import NetworkModel
from ran_lakehouse.planning import backhaul, geography, radio
from ran_lakehouse.planning.served import served_levels
from ran_lakehouse.planning.terrain import Terrain, build_terrain

GOLD = "lk.gold"
TECHNOLOGIES = {
    "LTE": ("lte_rsrp_dbm", radio.LTE_RSRP_THRESHOLD_DBM),
    "GSM": ("gsm_rxlev_dbm", radio.GSM_RXLEV_THRESHOLD_DBM),
}


@dataclass(frozen=True)
class Plan:
    """The expansion area's planning data.

    Attributes:
        terrain: Height map.
        villages: Villages.
        served: Best served level per technology at each village, dBm.
        grid_line: Power grid vertices.
        fiber_route: Fiber route vertices.
        hubs: Microwave hubs (name, x, y).
        candidates: Candidate sites.
        levels: Per candidate, the radio.site_levels at every village.
        options: Backhaul and power options.
    """

    terrain: Terrain
    villages: list[geography.Village]
    served: dict[str, np.ndarray]
    grid_line: np.ndarray
    fiber_route: np.ndarray
    hubs: list[tuple[str, float, float]]
    candidates: list[geography.Candidate]
    levels: list[dict[str, np.ndarray]]
    options: list[backhaul.Option]


def serves(level_dbm: np.ndarray, threshold_dbm: float) -> np.ndarray:
    """Indoor service: the outdoor level clears the threshold plus the indoor margin.

    Args:
        level_dbm: Outdoor levels.
        threshold_dbm: Coverage threshold.

    Returns:
        Mask.
    """
    result: np.ndarray = np.asarray(level_dbm) >= threshold_dbm + radio.INDOOR_MARGIN_DB
    return result


def build_plan(model: NetworkModel) -> Plan:
    """Generate the expansion area's planning data.

    Args:
        model: The network as built (its world gives the expansion area).

    Returns:
        The plan.
    """
    world = model.world
    terrain = build_terrain(world.profile)
    villages = geography.villages(world, terrain)
    vx = np.array([v.x_km for v in villages])
    vy = np.array([v.y_km for v in villages])
    grid_line = geography.utility_line(world, 0, geography.GRID_LINE_KM)
    fiber_route = geography.utility_line(world, 1, geography.FIBER_ROUTE_KM)
    candidates = geography.candidates(terrain, villages)
    return Plan(
        terrain=terrain,
        villages=villages,
        served=served_levels(model, terrain, vx, vy),
        grid_line=grid_line,
        fiber_route=fiber_route,
        hubs=backhaul.hubs(world),
        candidates=candidates,
        levels=[radio.site_levels(terrain, c.x_km, c.y_km, vx, vy) for c in candidates],
        options=backhaul.options(terrain, world, candidates, grid_line, fiber_route),
    )


def tables(plan: Plan) -> dict[str, pa.Table]:
    """The gold tables of rule G7.

    Args:
        plan: The plan.

    Returns:
        Table name to rows: villages, candidate_sites, coverage and
        backhaul_power_options.
    """
    lte, gsm = plan.served["LTE"], plan.served["GSM"]
    villages = pa.Table.from_pylist(
        [
            asdict(v)
            | {
                "served_lte_rsrp_dbm": round(float(lte[i]), 2),
                "served_gsm_rxlev_dbm": round(float(gsm[i]), 2),
                "covered_today_lte": bool(serves(lte[i], radio.LTE_RSRP_THRESHOLD_DBM)),
                "covered_today_gsm": bool(serves(gsm[i], radio.GSM_RXLEV_THRESHOLD_DBM)),
            }
            for i, v in enumerate(plan.villages)
        ]
    )
    cx = np.array([c.x_km for c in plan.candidates])
    cy = np.array([c.y_km for c in plan.candidates])
    grid_km = geography.distance_to_line(cx, cy, plan.grid_line)
    fiber_km = geography.distance_to_line(cx, cy, plan.fiber_route)
    sites = pa.Table.from_pylist(
        [
            asdict(c)
            | {
                "grid_distance_km": round(float(grid_km[k]), 3),
                "fiber_distance_km": round(float(fiber_km[k]), 3),
            }
            for k, c in enumerate(plan.candidates)
        ]
    )
    coverage = []
    for c, levels in zip(plan.candidates, plan.levels, strict=True):
        for i, v in enumerate(plan.villages):
            for technology, (column, threshold) in TECHNOLOGIES.items():
                level = float(levels[column][i])
                coverage.append(
                    {
                        "site_id": c.site_id,
                        "village_id": v.village_id,
                        "technology": technology,
                        "distance_km": round(float(levels["distance_km"][i]), 3),
                        "hata_db": round(float(levels["hata_db"][i]), 2),
                        "diffraction_db": round(float(levels["diffraction_db"][i]), 2),
                        "signal_dbm": round(level, 2),
                        "covered": bool(serves(np.array(level), threshold)),
                    }
                )
    return {
        "villages": villages,
        "candidate_sites": sites,
        "coverage": pa.Table.from_pylist(coverage),
        "backhaul_power_options": pa.Table.from_pylist([asdict(o) for o in plan.options]),
    }


def write_gold(con: duckdb.DuckDBPyConnection, data: dict[str, pa.Table]) -> None:
    """Replace the planning gold tables (generated data, rewritten whole).

    Args:
        con: DuckDB with the lake attached as "lk".
        data: Tables from tables().
    """
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")
    for name, table in data.items():
        con.register("planning_rows", table)
        try:
            write(con, f"DROP TABLE IF EXISTS {GOLD}.{name}")
            write(con, f"CREATE TABLE {GOLD}.{name} AS SELECT * FROM planning_rows")
        finally:
            con.unregister("planning_rows")
