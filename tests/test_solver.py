"""Plan solver and scenarios (rules G8, G9)."""

import copy
import itertools
from typing import Any

import jsonschema
import numpy as np
import pytest

from ran_lakehouse.model import default_model
from ran_lakehouse.planning.build import build_plan, tables
from ran_lakehouse.planning.scenarios import Scenario, constraint_set, scenarios
from ran_lakehouse.planning.solver import BACKENDS, PlanningData, solve_plan, validate
from ran_lakehouse.world import build_world

RELAXABLE = {
    "max_sites": "max_sites",
    "capex_budget": "capex_budget_idr",
    "monthly_opex": "monthly_opex_limit_idr",
    "min_persons_share": "min_persons_share",
}


def small_data(seed: int) -> PlanningData:
    """A random instance small enough to enumerate: 4 sites, 6 villages."""
    rng = np.random.default_rng(seed)
    villages = [
        {
            "village_id": 100 + j,
            "x_km": 45.0 + j,
            "y_km": 10.0,
            "population": int(p),
            "schools": int(s),
        }
        for j, (p, s) in enumerate(
            zip(rng.integers(200, 3000, 6), rng.integers(0, 3, 6), strict=True)
        )
    ]
    sites: list[dict[str, Any]] = [
        {
            "site_id": f"C{k + 1:02d}",
            "x_km": 45.0 + k,
            "y_km": 11.0,
            "build_cost_idr": int(rng.integers(1_500, 2_500)) * 1_000_000,
            "grid_distance_km": float(rng.uniform(0, 10)),
            "fiber_distance_km": float(rng.uniform(0, 8)),
        }
        for k in range(4)
    ]
    cover = {t: rng.random((4, 6)) < 0.45 for t in ("LTE", "GSM")}
    options: dict[str, dict[str, dict[str, Any]]] = {}
    for s in sites:
        clear = bool(rng.random() < 0.6)
        options[s["site_id"]] = {
            "fiber": {
                "available": True,
                "capex_idr": int(rng.integers(50, 900)) * 1_000_000,
                "monthly_idr": 3_000_000,
                "clears_full_fresnel": False,
                "detail": "",
            },
            "microwave": {
                "available": clear,
                "capex_idr": 350_000_000 if clear else 0,
                "monthly_idr": 2_000_000 if clear else 0,
                "clears_full_fresnel": clear and bool(rng.random() < 0.5),
                "detail": "SITE0001" if clear else "none",
            },
            "satellite": {
                "available": True,
                "capex_idr": 150_000_000,
                "monthly_idr": 25_000_000,
                "clears_full_fresnel": False,
                "detail": "",
            },
            "grid_power": {
                "available": True,
                "capex_idr": int(rng.integers(100, 2000)) * 1_000_000,
                "monthly_idr": 6_000_000,
                "clears_full_fresnel": False,
                "detail": "",
            },
            "solar_power": {
                "available": True,
                "capex_idr": 650_000_000,
                "monthly_idr": 1_500_000,
                "clears_full_fresnel": False,
                "detail": "",
            },
        }
    return PlanningData(villages, sites, cover, options)


def brute_force(data: PlanningData, c: dict[str, Any]) -> float | None:
    """The optimum by enumerating every site subset and backhaul choice."""
    best: float | None = None
    technologies = ("LTE", "GSM") if c["technology"] == "both" else (c["technology"],)
    rule = c["must_cover"]
    must = {
        j
        for j, v in enumerate(data.villages)
        if (rule["schools"] and v["schools"] > 0)
        or (rule["min_persons"] is not None and v["population"] >= rule["min_persons"])
        or v["village_id"] in rule["village_ids"]
    }
    total = sum(v["population"] for v in data.villages)
    for chosen in itertools.product([0, 1], repeat=len(data.sites)):
        built = [k for k in range(len(data.sites)) if chosen[k]]
        allowed = []
        for k in built:
            s, o = data.sites[k], data.options[data.sites[k]["site_id"]]
            kinds = ["satellite"]
            if s["fiber_distance_km"] <= c["backhaul_rule"]["fiber_within_km"]:
                kinds.append("fiber")
            mw = o["microwave"]
            if (
                mw["available"]
                if c["backhaul_rule"]["microwave_clearance"] == 0.6
                else mw["clears_full_fresnel"]
            ):
                kinds.append("microwave")
            allowed.append(kinds)
        served = {
            j
            for j in range(len(data.villages))
            if all(any(data.cover[t][k, j] for k in built) for t in technologies)
        }
        if not must <= served:
            continue
        persons = sum(data.villages[j]["population"] for j in served)
        if c["min_persons_share"] is not None and persons < np.ceil(c["min_persons_share"] * total):
            continue
        if c["max_sites"] is not None and len(built) > c["max_sites"]:
            continue
        for choice in itertools.product(*allowed):
            capex = monthly = 0
            for k, kind in zip(built, choice, strict=True):
                s, o = data.sites[k], data.options[data.sites[k]["site_id"]]
                power = o[
                    "grid_power"
                    if s["grid_distance_km"] <= c["power_rule"]["grid_within_km"]
                    else "solar_power"
                ]
                capex += s["build_cost_idr"] + power["capex_idr"] + o[kind]["capex_idr"]
                monthly += power["monthly_idr"] + o[kind]["monthly_idr"]
            if c["capex_budget_idr"] is not None and capex > c["capex_budget_idr"]:
                continue
            if c["monthly_opex_limit_idr"] is not None and monthly > c["monthly_opex_limit_idr"]:
                continue
            value = {"max_persons": persons, "max_villages": len(served), "min_capex": -capex}[
                c["objective"]
            ]
            best = value if best is None else max(best, value)
    if best is None:
        return None
    return -best if c["objective"] == "min_capex" else best


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_solver_matches_enumeration(seed: int, backend: str) -> None:
    data = small_data(seed)
    rng = np.random.default_rng(1000 + seed)
    for objective in ("max_persons", "max_villages", "min_capex"):
        c = constraint_set(
            "whole",
            objective,
            str(rng.choice(["LTE", "GSM", "both"])),
            max_sites=int(rng.integers(1, 4)),
            capex=int(rng.integers(3, 9)) * 1_000_000_000,
            opex=int(rng.integers(10, 80)) * 1_000_000,
            share=0.3 if objective == "min_capex" else None,
            fiber_km=float(rng.uniform(1, 6)),
            clearance=float(rng.choice([0.6, 1.0])),
        )
        expected = brute_force(data, c)
        result = solve_plan(data, c, backend)
        if expected is None:
            assert result["status"] == "infeasible"
        else:
            assert result["status"] == "optimal"
            assert result["objective"] == pytest.approx(expected)


@pytest.fixture(scope="module")
def demo() -> tuple[PlanningData, list[Scenario]]:
    data = PlanningData.from_tables(tables(build_plan(default_model(build_world("demo")))))
    return data, scenarios(data)


def test_cards_validate_and_bad_sets_are_rejected(
    demo: tuple[PlanningData, list[Scenario]],
) -> None:
    _, cards = demo
    assert len(cards) == 20
    for card in cards:
        validate(card.constraints)
    bad = copy.deepcopy(cards[0].constraints)
    bad["objective"] = "max_profit"
    with pytest.raises(jsonschema.ValidationError):
        validate(bad)


def test_each_card_behaves_as_its_difficulty_says(
    demo: tuple[PlanningData, list[Scenario]],
) -> None:
    data, cards = demo
    for card in cards:
        results = {b: solve_plan(data, card.constraints, b) for b in ("highs", "cpsat")}
        statuses = {r["status"] for r in results.values()}
        assert statuses == {"infeasible" if card.difficulty == "infeasible" else "optimal"}, (
            card.scenario_id
        )
        assert len({r.get("objective") for r in results.values()}) == 1, card.scenario_id


def test_plans_satisfy_their_constraints(demo: tuple[PlanningData, list[Scenario]]) -> None:
    data, cards = demo
    by_site = {s["site_id"]: k for k, s in enumerate(data.sites)}
    by_village = {v["village_id"]: j for j, v in enumerate(data.villages)}
    for card in cards:
        c = card.constraints
        r = solve_plan(data, c, "highs")
        if r["status"] != "optimal":
            continue
        built = [by_site[s["site_id"]] for s in r["sites"]]
        technologies: tuple[str, ...] = (
            ("LTE", "GSM") if c["technology"] == "both" else (c["technology"],)
        )
        for village in r["villages_covered"]:
            j = by_village[village]
            assert all(data.cover[t][built, j].any() for t in technologies), card.scenario_id
        if c["max_sites"] is not None:
            assert len(built) <= c["max_sites"]
        if c["capex_budget_idr"] is not None:
            assert r["capex_idr"] <= c["capex_budget_idr"]
        if c["monthly_opex_limit_idr"] is not None:
            assert r["monthly_idr"] <= c["monthly_opex_limit_idr"]
        assert r["persons_covered"] == sum(
            data.villages[by_village[v]]["population"] for v in r["villages_covered"]
        )
        assert all(
            x["objective_gain_without_it"] is None or x["objective_gain_without_it"] >= 0
            for x in r["constraints"]
        )


def test_relaxing_as_told_restores_a_plan(demo: tuple[PlanningData, list[Scenario]]) -> None:
    data, cards = demo
    infeasible = [c for c in cards if c.difficulty == "infeasible"]
    assert infeasible
    for card in infeasible:
        r = solve_plan(data, card.constraints, "highs")
        assert r["relax_to_feasible"], card.scenario_id
        for entry in r["relax_to_feasible"]:
            relaxed = copy.deepcopy(card.constraints)
            if entry["constraint"] == "must_cover":
                relaxed["must_cover"] = {"schools": False, "min_persons": None, "village_ids": []}
            else:
                relaxed[RELAXABLE[entry["constraint"]]] = entry["relax_to"]
            assert solve_plan(data, relaxed, "highs")["status"] == "optimal", (
                card.scenario_id,
                entry,
            )


def test_a_solve_broken_by_a_clock_step_is_run_again(monkeypatch: pytest.MonkeyPatch) -> None:
    from ran_lakehouse.planning import solver

    calls: list[int] = []

    def fails_once(message: str) -> Any:
        def solve(*args: Any, **kwargs: Any) -> str:
            calls.append(1)
            if len(calls) == 1:
                try:
                    raise RuntimeError(message)
                except RuntimeError:
                    raise AttributeError("status conversion failed") from None
            return "solved"

        return solve

    monkeypatch.setattr(
        "ortools.math_opt.python.mathopt.solve", fails_once(solver.CLOCK_STEP_ERROR)
    )
    assert solver.solve_through_clock_steps(None, None, None) == "solved"
    calls.clear()
    monkeypatch.setattr("ortools.math_opt.python.mathopt.solve", fails_once("something else"))
    with pytest.raises(AttributeError):
        solver.solve_through_clock_steps(None, None, None)
