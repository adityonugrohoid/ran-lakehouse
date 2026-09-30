"""Planning scenarios (rule G8): written requests and the constraints they imply.

Each card is a request as an engineer would write it (mostly English, a
few in mixed Indonesian and English), an area of the expansion region, and
the constraint set it implies (contract/plan_constraints.schema.json). The
cards range from easy to hard; a few are infeasible on purpose, where the
right answer is "infeasible, relax this". Only the card (id, area, request)
is public; the difficulty, the implied constraint set and the exact
optimum are evaluation-only (rule A3). Numbers in the requests are START
values chosen so each card behaves as its difficulty says.
"""

import json
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from ran_lakehouse.planning.solver import PlanningData, solve_plan

AREAS = {
    "whole": (40.0, 60.0, 0.0, 40.0),
    "north": (40.0, 60.0, 20.0, 40.0),
    "south": (40.0, 60.0, 0.0, 20.0),
    "west": (40.0, 50.0, 0.0, 40.0),
    "east": (50.0, 60.0, 0.0, 40.0),
    "north-east": (50.0, 60.0, 20.0, 40.0),
    "south-west": (40.0, 50.0, 0.0, 20.0),
    "central": (45.0, 55.0, 10.0, 30.0),
}
# Defaults of the rules a request does not mention (rule G6, START).
GRID_WITHIN_KM = 5.0
FIBER_WITHIN_KM = 3.0
BILLION = 1_000_000_000
MILLION = 1_000_000


@dataclass(frozen=True)
class Scenario:
    """One planning scenario.

    Attributes:
        scenario_id: "S01" on.
        difficulty: "easy", "medium", "hard" or "infeasible" (evaluation-only).
        area: Area name (AREAS).
        request: The written request.
        constraints: The constraint set it implies (evaluation-only).
    """

    scenario_id: str
    difficulty: str
    area: str
    request: str
    constraints: dict[str, Any]


def constraint_set(area: str, objective: str, technology: str, **given: Any) -> dict[str, Any]:
    """A full constraint set: the given fields over the defaults.

    Args:
        area: Area name.
        objective: Objective.
        technology: LTE, GSM or both.
        **given: schools, min_persons, village_ids, max_sites, capex, opex,
            share, grid_km, fiber_km, clearance.

    Returns:
        The set (schema version 1.0.0).
    """
    x0, x1, y0, y1 = AREAS[area]
    return {
        "schema_version": "1.0.0",
        "area": {"x_min_km": x0, "x_max_km": x1, "y_min_km": y0, "y_max_km": y1},
        "objective": objective,
        "technology": technology,
        "must_cover": {
            "schools": given.get("schools", False),
            "min_persons": given.get("min_persons"),
            "village_ids": given.get("village_ids", []),
        },
        "max_sites": given.get("max_sites"),
        "capex_budget_idr": given.get("capex"),
        "monthly_opex_limit_idr": given.get("opex"),
        "power_rule": {"grid_within_km": given.get("grid_km", GRID_WITHIN_KM)},
        "backhaul_rule": {
            "fiber_within_km": given.get("fiber_km", FIBER_WITHIN_KM),
            "microwave_clearance": given.get("clearance", 0.6),
        },
        "min_persons_share": given.get("share"),
    }


def largest_villages(data: PlanningData, area: str, technology: str, count: int) -> list[int]:
    """The largest villages of an area that some candidate can serve.

    Args:
        data: Planning data.
        area: Area name.
        technology: LTE or GSM.
        count: How many.

    Returns:
        Village ids, largest first.
    """
    x0, x1, y0, y1 = AREAS[area]
    servable = data.cover[technology].any(axis=0)
    inside = [
        v
        for k, v in enumerate(data.villages)
        if servable[k] and x0 <= v["x_km"] <= x1 and y0 <= v["y_km"] <= y1
    ]
    return [v["village_id"] for v in sorted(inside, key=lambda v: -v["population"])[:count]]


def scenarios(data: PlanningData) -> list[Scenario]:
    """The scenario cards.

    Args:
        data: Planning data (named villages are taken from it).

    Returns:
        The cards.
    """
    a, b = largest_villages(data, "south-west", "GSM", 2)
    c = largest_villages(data, "east", "GSM", 1)[0]
    cards = [
        (
            "easy",
            "north",
            "Plan LTE for the northern half. Use at most 6 sites and cover as many people "
            "as possible.",
            constraint_set("north", "max_persons", "LTE", max_sites=6),
        ),
        (
            "easy",
            "south",
            "In the south, which 5 sites would reach the most villages with GSM?",
            constraint_set("south", "max_villages", "GSM", max_sites=5),
        ),
        (
            "easy",
            "whole",
            "Find the cheapest set of sites that gives indoor 2G to every village with a school.",
            constraint_set("whole", "min_capex", "GSM", schools=True),
        ),
        (
            "easy",
            "west",
            "West strip: cover at least 80% of the population with LTE at the lowest capital cost.",
            constraint_set("west", "min_capex", "LTE", share=0.8),
        ),
        (
            "easy",
            "whole",
            "Where should 3 LTE sites go to reach the most people?",
            constraint_set("whole", "max_persons", "LTE", max_sites=3),
        ),
        (
            "medium",
            "east",
            "Tolong buat plan 4G untuk area timur, budget capex maksimal 15 miliar rupiah, "
            "target populasi sebanyak mungkin.",
            constraint_set("east", "max_persons", "LTE", capex=15 * BILLION),
        ),
        (
            "medium",
            "north-east",
            "North-east corner: both 2G and 4G, no more than 5 sites, most people covered, "
            "and opex must stay under 40 juta per month.",
            constraint_set("north-east", "max_persons", "both", max_sites=5, opex=40 * MILLION),
        ),
        (
            "medium",
            "whole",
            "Every village above 3,000 people needs indoor LTE. Minimize capex, and use "
            "fiber only where the route is within 2 km.",
            constraint_set("whole", "min_capex", "LTE", min_persons=3000, fiber_km=2.0),
        ),
        (
            "medium",
            "south-west",
            f"GSM for the south-west: villages {a} and {b} must be covered, then as many "
            "other people as possible with 3 sites.",
            constraint_set("south-west", "max_persons", "GSM", village_ids=[a, b], max_sites=3),
        ),
        (
            "medium",
            "central",
            "Central area LTE: at least 70% of people, microwave only where the full first "
            "Fresnel zone clears, lowest capex.",
            constraint_set("central", "min_capex", "LTE", share=0.7, clearance=1.0),
        ),
        (
            "medium",
            "whole",
            "Semua desa dengan sekolah harus dapat 2G. Maksimal 14 site, sisanya maximize "
            "jumlah desa yang tercover.",
            constraint_set("whole", "max_villages", "GSM", schools=True, max_sites=14),
        ),
        (
            "medium",
            "west",
            "West strip, GSM: the cheapest plan that reaches 95% of the people.",
            constraint_set("west", "min_capex", "GSM", share=0.95),
        ),
        (
            "hard",
            "whole",
            "2G for the whole area within 40 billion rupiah capex and 150 million a month "
            "opex. Every school village covered; maximize people.",
            constraint_set(
                "whole",
                "max_persons",
                "GSM",
                schools=True,
                capex=40 * BILLION,
                opex=150 * MILLION,
            ),
        ),
        (
            "hard",
            "north",
            "Northern half, both technologies, at least 90% of people, at most 12 sites, "
            "capex under 30 billion. Minimize capex.",
            constraint_set(
                "north", "min_capex", "both", share=0.9, max_sites=12, capex=30 * BILLION
            ),
        ),
        (
            "hard",
            "east",
            "East strip: grid power only within 2 km (solar otherwise), fiber within 4 km. "
            f"All villages above 1,000 people plus village {c} get 2G; opex under "
            "60 juta/bulan; minimize capex.",
            constraint_set(
                "east",
                "min_capex",
                "GSM",
                min_persons=1000,
                village_ids=[c],
                grid_km=2.0,
                fiber_km=4.0,
                opex=60 * MILLION,
            ),
        ),
        (
            "hard",
            "whole",
            "Kita ada budget 45 miliar dan max 20 site. Target: 4G indoor untuk sebanyak "
            "mungkin orang, tapi desa di atas 3.000 jiwa wajib tercover.",
            constraint_set(
                "whole",
                "max_persons",
                "LTE",
                min_persons=3000,
                capex=45 * BILLION,
                max_sites=20,
            ),
        ),
        (
            "hard",
            "south",
            "South half, both 2G and 4G: maximize villages covered with at most 8 sites, "
            "monthly cost under 80 million, and full Fresnel clearance for any microwave hop.",
            constraint_set(
                "south", "max_villages", "both", max_sites=8, opex=80 * MILLION, clearance=1.0
            ),
        ),
        (
            "infeasible",
            "whole",
            "Cover every village with a school with 2G using only 5 sites.",
            constraint_set("whole", "min_capex", "GSM", schools=True, max_sites=5),
        ),
        (
            "infeasible",
            "north",
            "Utara: 4G untuk 99% penduduk dengan capex maksimal 10 miliar.",
            constraint_set("north", "min_capex", "LTE", share=0.99, capex=10 * BILLION),
        ),
        (
            "infeasible",
            "whole",
            "Every village with a school must get indoor 4G. Find the cheapest plan.",
            constraint_set("whole", "min_capex", "LTE", schools=True),
        ),
    ]
    return [
        Scenario(f"S{k + 1:02d}", difficulty, area, request, constraints)
        for k, (difficulty, area, request, constraints) in enumerate(cards)
    ]


def cards_table(cards: list[Scenario]) -> pa.Table:
    """The public cards (gold.planning_scenarios): id, area and request only.

    Args:
        cards: The scenarios.

    Returns:
        Rows.
    """
    return pa.Table.from_pylist(
        [
            {
                "scenario_id": s.scenario_id,
                "area": s.area,
                "x_min_km": AREAS[s.area][0],
                "x_max_km": AREAS[s.area][1],
                "y_min_km": AREAS[s.area][2],
                "y_max_km": AREAS[s.area][3],
                "request": s.request,
            }
            for s in cards
        ]
    )


def answers_table(
    data: PlanningData, cards: list[Scenario], backend: str
) -> tuple[pa.Table, list[dict[str, Any]]]:
    """The exact optimum of every card (evaluation.planning_answers, rule A3).

    Args:
        data: Planning data.
        cards: The scenarios.
        backend: Solver back end.

    Returns:
        (rows, the full solve results).
    """
    results = [solve_plan(data, s.constraints, backend) for s in cards]
    rows = [
        {
            "scenario_id": s.scenario_id,
            "difficulty": s.difficulty,
            "constraints": json.dumps(s.constraints, sort_keys=True),
            "status": r["status"],
            "objective": r.get("objective"),
            "capex_idr": r.get("capex_idr"),
            "monthly_idr": r.get("monthly_idr"),
            "persons_covered": r.get("persons_covered"),
            "villages_covered": len(r.get("villages_covered", [])),
            "sites": json.dumps(r.get("sites", [])),
            "relax_to_feasible": json.dumps(r.get("relax_to_feasible", [])),
            "backend": backend,
        }
        for s, r in zip(cards, results, strict=True)
    ]
    return pa.Table.from_pylist(rows), results
