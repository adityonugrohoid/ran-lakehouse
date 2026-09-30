"""Plan solver (rule G9): which candidate sites to build under a constraint set.

One integer program, built from the planning gold tables and a constraint
set (contract/plan_constraints.schema.json, rule A1):

- x_s = 1 builds candidate s; its power is set by the power rule (grid
  within grid_within_km, else solar) and its cost counts with x_s;
- y_sb = 1 gives site s backhaul b, one of the options the backhaul rule
  allows (fiber within fiber_within_km, microwave where the hop clears the
  asked share of the first Fresnel zone, satellite always); sum_b y_sb = x_s;
- c_v = 1 counts village v as covered, only if a built site serves it
  indoors with the asked technology (both: LTE and GSM);
- must_cover villages have c_v = 1; the site count, capital cost, monthly
  cost and covered share of persons are bounded as asked.

The objective is solved first; then, holding it at its optimum, capital
cost is minimized (monthly cost for min_capex), so the plan returned is
the cheapest optimal one. Integer programs have no dual prices, so each
constraint's cost is measured by solving again without it. An infeasible
set reports each constraint whose removal restores a solution and, for the
numeric ones, the value that would.

The model is built once as an OR-Tools MathOpt model (OR-Tools 9.15.6755)
and solved by one of its bundled solvers: HiGHS, CP-SAT or SCIP. BACKEND is
the one chosen by measurement (planning.scenario_report). The separate
highspy package cannot share a process with OR-Tools (both load their own
HiGHS build).
"""

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np
import pyarrow as pa
from ortools.math_opt.python import mathopt

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "contract" / "plan_constraints.schema.json"
BACKEND = "scip"  # chosen by measurement, see results/planning_scenarios.md
TIME_LIMIT_S = 60.0  # per solve (ASSUMPTION)
# One thread for the solvers that take a count (CP-SAT, SCIP), so the measured
# pick holds on any machine: CP-SAT with 16 workers proved every card, with
# 4 it ran out of time. HiGHS takes no thread count through MathOpt.
THREADS = 1
CLOCK_STEP_ERROR = "solve_time must be non-negative"
CLOCK_STEP_ATTEMPTS = 3
clock_step_reruns = {"count": 0}
logger = logging.getLogger(__name__)
GROUPS = ("must_cover", "max_sites", "capex_budget", "monthly_opex", "min_persons_share")
BACKHAUL = ("fiber", "microwave", "satellite")


@dataclass(frozen=True)
class PlanningData:
    """The planning gold tables, arranged for the solver.

    Attributes:
        villages: Village rows (id, x, y, population, schools).
        sites: Candidate rows (id, x, y, build cost, grid and fiber distance).
        cover: Technology to a (sites, villages) mask of indoor service.
        options: Site id to backhaul or power kind to its option row.
    """

    villages: list[dict[str, Any]]
    sites: list[dict[str, Any]]
    cover: dict[str, np.ndarray]
    options: dict[str, dict[str, dict[str, Any]]]

    @classmethod
    def from_tables(cls, tables: dict[str, pa.Table]) -> "PlanningData":
        """Arrange the planning gold tables.

        Args:
            tables: villages, candidate_sites, coverage and
                backhaul_power_options.

        Returns:
            The data.
        """
        villages = tables["villages"].to_pylist()
        sites = tables["candidate_sites"].to_pylist()
        v_index = {v["village_id"]: k for k, v in enumerate(villages)}
        s_index = {s["site_id"]: k for k, s in enumerate(sites)}
        cover = {t: np.zeros((len(sites), len(villages)), dtype=bool) for t in ("LTE", "GSM")}
        for row in tables["coverage"].to_pylist():
            if row["covered"]:
                cover[row["technology"]][s_index[row["site_id"]], v_index[row["village_id"]]] = True
        options: dict[str, dict[str, dict[str, Any]]] = {}
        for row in tables["backhaul_power_options"].to_pylist():
            options.setdefault(row["site_id"], {})[row["kind"]] = row
        return cls(villages, sites, cover, options)


@dataclass
class Model:
    """A 0-1 integer program in plain form.

    Attributes:
        names: Variable names (all binary).
        rows: (coefficients by variable index, lower, upper, group).
        objective: Coefficients by variable index.
        maximize: Direction.
    """

    names: list[str] = field(default_factory=list)
    rows: list[tuple[dict[int, int], float, float, str]] = field(default_factory=list)
    objective: dict[int, int] = field(default_factory=dict)
    maximize: bool = True

    def var(self, name: str) -> int:
        """Add a binary variable.

        Args:
            name: Its name.

        Returns:
            Its index.
        """
        self.names.append(name)
        return len(self.names) - 1

    def row(self, coefficients: dict[int, int], lower: float, upper: float, group: str) -> None:
        """Add lower <= sum(coefficients * x) <= upper.

        Args:
            coefficients: Integer coefficient per variable.
            lower: Lower bound (-inf for none).
            upper: Upper bound (inf for none).
            group: Constraint group (GROUPS, or "model" for structure).
        """
        self.rows.append((coefficients, lower, upper, group))


@dataclass(frozen=True)
class Solution:
    """One back-end solve.

    Attributes:
        status: "optimal", "infeasible" or "time_limit".
        objective: Objective value (NaN unless optimal).
        values: Variable values (empty unless a solution was found).
        seconds: Solve time.
    """

    status: str
    objective: float
    values: np.ndarray
    seconds: float


def solve_through_clock_steps(m: Any, solver_type: Any, parameters: Any) -> Any:
    """mathopt.solve, run again when a wall-clock step broke its timing.

    MathOpt times a solve with the wall clock; when the clock steps back
    during a solve (WSL2 time sync on the build machine, see
    lake.catalog.write) it fails with CLOCK_STEP_ERROR, and OR-Tools 9.15
    raises an AttributeError while converting that status. Nothing about the
    model is wrong, so the solve is run again, at most CLOCK_STEP_ATTEMPTS
    times; each rerun is logged and counted. Any other failure is raised.

    Args:
        m: The MathOpt model.
        solver_type: The solver.
        parameters: Solve parameters.

    Returns:
        The MathOpt result.

    Raises:
        AttributeError: If the failure is not a clock step, or keeps
            happening.
    """
    for attempt in range(1, CLOCK_STEP_ATTEMPTS + 1):
        try:
            return mathopt.solve(m, solver_type, params=parameters)
        except AttributeError as exc:
            cause = exc.__context__
            if cause is None or CLOCK_STEP_ERROR not in str(cause):
                raise
            if attempt == CLOCK_STEP_ATTEMPTS:
                raise
            clock_step_reruns["count"] += 1
            logger.warning("solve rerun after a wall-clock step (%s)", cause)
    raise AssertionError("unreachable")


def solve_mathopt(model: Model, solver_type: Any) -> Solution:
    """Solve with one of OR-Tools MathOpt's solvers, to a zero gap.

    Args:
        model: The model.
        solver_type: mathopt.SolverType (HIGHS, CP_SAT or GSCIP).

    Returns:
        The solution.
    """
    started = time.perf_counter()
    m = mathopt.Model(name="plan")
    x = [m.add_binary_variable(name=name) for name in model.names]
    exact = solver_type == mathopt.SolverType.CP_SAT
    for coefficients, lower, upper, _ in model.rows:
        # Floating-point solvers get each row divided by its largest
        # coefficient: unscaled IDR rows (about 1e10) made SCIP stop at a
        # plan 40 persons short of the optimum. CP-SAT solves in integers.
        scale = 1.0 if exact or not coefficients else float(max(map(abs, coefficients.values())))
        m.add_linear_constraint(
            lb=lower / scale,
            ub=upper / scale,
            expr=mathopt.fast_sum(c / scale * x[i] for i, c in coefficients.items()),
        )
    objective = mathopt.fast_sum(c * x[i] for i, c in model.objective.items())
    if model.maximize:
        m.maximize(objective)
    else:
        m.minimize(objective)
    parameters = mathopt.SolveParameters(
        time_limit=timedelta(seconds=TIME_LIMIT_S),
        relative_gap_tolerance=0.0,
        absolute_gap_tolerance=0.0,
        threads=None if solver_type == mathopt.SolverType.HIGHS else THREADS,
    )
    result = solve_through_clock_steps(m, solver_type, parameters)
    seconds = time.perf_counter() - started
    reason = result.termination.reason
    if reason == mathopt.TerminationReason.OPTIMAL:
        values = np.rint(np.array([result.variable_values(v) for v in x], dtype=float))
        check_rows(model, values, solver_type.name)
        return Solution("optimal", float(result.objective_value()), values, seconds)
    if reason == mathopt.TerminationReason.INFEASIBLE:
        return Solution("infeasible", math.nan, np.zeros(0), seconds)
    limit = result.termination.limit
    # MathOpt's CP-SAT reports its time limit as UNDETERMINED.
    timed_out = limit == mathopt.Limit.TIME or (
        limit == mathopt.Limit.UNDETERMINED and seconds >= TIME_LIMIT_S
    )
    if (
        reason in (mathopt.TerminationReason.FEASIBLE, mathopt.TerminationReason.NO_SOLUTION_FOUND)
        and timed_out
    ):
        return Solution("time_limit", math.nan, np.zeros(0), seconds)
    raise RuntimeError(
        f"{solver_type.name} ended with {reason.name} after {seconds:.1f} s "
        f"(limit {limit.name if limit is not None else None}): {result.termination.detail}"
    )


def check_rows(model: Model, values: np.ndarray, solver_name: str) -> None:
    """Check a rounded solution against the unscaled integer rows.

    Args:
        model: The model.
        values: Rounded variable values.
        solver_name: For the error message.

    Raises:
        RuntimeError: If a row does not hold exactly.
    """
    for coefficients, lower, upper, group in model.rows:
        total = sum(c * int(values[i]) for i, c in coefficients.items())
        if not lower <= total <= upper:
            raise RuntimeError(
                f"{solver_name} returned a plan that breaks a {group} row: "
                f"{total} outside [{lower}, {upper}]"
            )


BACKENDS = {
    "highs": mathopt.SolverType.HIGHS,
    "cpsat": mathopt.SolverType.CP_SAT,
    "scip": mathopt.SolverType.GSCIP,
}


def validate(constraints: dict[str, Any]) -> None:
    """Check a constraint set against the contract schema.

    Args:
        constraints: The set.

    Raises:
        jsonschema.ValidationError: If it does not match.
    """
    jsonschema.validate(constraints, json.loads(SCHEMA_PATH.read_text()))


@dataclass
class Built:
    """A model with the maps back to sites and villages.

    Attributes:
        model: The model.
        sites: Site indices in play.
        villages: Village indices in play.
        x: Site index to its build variable.
        y: (site index, backhaul kind) to its variable.
        c: Village index to its covered variable.
        expressions: capex, monthly, persons, villages and sites as
            coefficient maps.
        must: Village indices that must be covered.
        power: Site index to its power kind under the power rule.
    """

    model: Model
    sites: list[int]
    villages: list[int]
    x: dict[int, int]
    y: dict[tuple[int, str], int]
    c: dict[int, int]
    expressions: dict[str, dict[int, int]]
    must: list[int]
    power: dict[int, str]


def inside(row: dict[str, Any], area: dict[str, float]) -> bool:
    """Whether a site or village lies in the area box.

    Args:
        row: A row with x_km and y_km.
        area: The box.

    Returns:
        Whether it is inside.
    """
    return bool(
        area["x_min_km"] <= row["x_km"] <= area["x_max_km"]
        and area["y_min_km"] <= row["y_km"] <= area["y_max_km"]
    )


def build(
    data: PlanningData, constraints: dict[str, Any], drop: frozenset[str], objective: str
) -> Built:
    """Build the integer program.

    Args:
        data: Planning data.
        constraints: A valid constraint set.
        drop: Constraint groups left out.
        objective: max_persons, max_villages, min_capex, min_monthly or
            min_sites.

    Returns:
        The model and its maps.

    Raises:
        ValueError: If a named must-cover village is not in the area.
    """
    area = constraints["area"]
    sites = [k for k, s in enumerate(data.sites) if inside(s, area)]
    villages = [k for k, v in enumerate(data.villages) if inside(v, area)]
    m = Model()
    x = {k: m.var(f"x_{data.sites[k]['site_id']}") for k in sites}
    c = {k: m.var(f"c_{data.villages[k]['village_id']}") for k in villages}
    y: dict[tuple[int, str], int] = {}
    capex: dict[int, int] = {}
    monthly: dict[int, int] = {}
    power_kind: dict[int, str] = {}
    power_rule = constraints["power_rule"]["grid_within_km"]
    fiber_rule = constraints["backhaul_rule"]["fiber_within_km"]
    clearance = constraints["backhaul_rule"]["microwave_clearance"]
    for k in sites:
        site = data.sites[k]
        options = data.options[site["site_id"]]
        power_kind[k] = "grid" if site["grid_distance_km"] <= power_rule else "solar"
        power = options[f"{power_kind[k]}_power"]
        capex[x[k]] = site["build_cost_idr"] + power["capex_idr"]
        monthly[x[k]] = power["monthly_idr"]
        allowed = ["satellite"]
        if site["fiber_distance_km"] <= fiber_rule:
            allowed.append("fiber")
        microwave = options["microwave"]
        if microwave["available"] if clearance == 0.6 else microwave["clears_full_fresnel"]:
            allowed.append("microwave")
        link = {x[k]: -1}
        for kind in allowed:
            y[(k, kind)] = v = m.var(f"y_{site['site_id']}_{kind}")
            link[v] = 1
            capex[v] = options[kind]["capex_idr"]
            monthly[v] = options[kind]["monthly_idr"]
        m.row(link, 0, 0, "model")
    technologies = (
        ("LTE", "GSM") if constraints["technology"] == "both" else (constraints["technology"],)
    )
    for j in villages:
        for technology in technologies:
            row = {c[j]: 1}
            for k in sites:
                if data.cover[technology][k, j]:
                    row[x[k]] = -1
            m.row(row, -math.inf, 0, "model")
    rule = constraints["must_cover"]
    named = set(rule["village_ids"])
    in_area = {data.villages[j]["village_id"] for j in villages}
    if named - in_area:
        raise ValueError(f"must_cover villages outside the area: {sorted(named - in_area)}")
    must = [
        j
        for j in villages
        if (rule["schools"] and data.villages[j]["schools"] > 0)
        or (
            rule["min_persons"] is not None
            and data.villages[j]["population"] >= rule["min_persons"]
        )
        or data.villages[j]["village_id"] in named
    ]
    persons = {c[j]: int(data.villages[j]["population"]) for j in villages}
    expressions = {
        "capex": capex,
        "monthly": monthly,
        "persons": persons,
        "villages": {c[j]: 1 for j in villages},
        "sites": {x[k]: 1 for k in sites},
    }
    if "must_cover" not in drop:
        for j in must:
            m.row({c[j]: 1}, 1, 1, "must_cover")
    bounds = (
        ("max_sites", "sites", constraints["max_sites"]),
        ("capex_budget", "capex", constraints["capex_budget_idr"]),
        ("monthly_opex", "monthly", constraints["monthly_opex_limit_idr"]),
    )
    for group, expression, limit in bounds:
        if limit is not None and group not in drop:
            m.row(expressions[expression], -math.inf, limit, group)
    share = constraints["min_persons_share"]
    if share is not None and "min_persons_share" not in drop:
        m.row(persons, math.ceil(share * sum(persons.values())), math.inf, "min_persons_share")
    objectives = {
        "max_persons": ("persons", True),
        "max_villages": ("villages", True),
        "min_capex": ("capex", False),
        "min_monthly": ("monthly", False),
        "min_sites": ("sites", False),
    }
    expression, m.maximize = objectives[objective]
    m.objective = expressions[expression]
    return Built(m, sites, villages, x, y, c, expressions, must, power_kind)


def value(built: Built, expression: str, values: np.ndarray) -> int:
    """An expression's value in a solution.

    Args:
        built: The model.
        expression: Expression name.
        values: Variable values.

    Returns:
        The value.
    """
    total = float(sum(c * values[i] for i, c in built.expressions[expression].items()))
    return round(total)


def lexicographic(built: Built, solve: Any) -> tuple[Solution, Solution | None]:
    """Solve the objective, then the cheapest plan at that optimum.

    Args:
        built: The model.
        solve: Back-end solve function.

    Returns:
        (first-stage solution, second-stage solution or None if the first
        is not optimal).
    """
    first = solve(built.model)
    if first.status != "optimal":
        return first, None
    m = built.model
    second = Model(list(m.names), list(m.rows))
    optimum = round(first.objective)
    if m.maximize:
        second.row(m.objective, optimum, math.inf, "model")
    else:
        second.row(m.objective, -math.inf, optimum, "model")
    tie_break = "monthly" if m.objective is built.expressions["capex"] else "capex"
    second.objective = built.expressions[tie_break]
    second.maximize = False
    return first, solve(second)


def solve_plan(data: PlanningData, constraints: dict[str, Any], backend: str) -> dict[str, Any]:
    """Solve a constraint set: the plan, its binding constraints and their cost.

    Args:
        data: Planning data.
        constraints: A constraint set (validated here).
        backend: "highs" or "cpsat".

    Returns:
        status; for an optimal plan: objective, the chosen sites with their
        backhaul and power, villages and persons covered, capital and
        monthly cost, and per constraint its limit, use, whether it binds
        and the objective gained without it; for an infeasible set: the
        constraints whose removal restores a solution, with the value that
        would do for the numeric ones; and the solve seconds.
    """
    validate(constraints)
    solver_type = BACKENDS[backend]

    def solve(model: Model) -> Solution:
        return solve_mathopt(model, solver_type)

    objective = constraints["objective"]
    started = time.perf_counter()
    built = build(data, constraints, frozenset(), objective)
    first, second = lexicographic(built, solve)
    result: dict[str, Any] = {"backend": backend, "status": first.status}
    if first.status == "infeasible":
        result["relax_to_feasible"] = relax_to_feasible(data, constraints, solve)
    elif first.status == "optimal" and second is not None and second.status == "optimal":
        result |= describe(data, built, second.values, round(first.objective))
        result["constraints"] = constraint_costs(
            data, constraints, built, second.values, solve, round(first.objective)
        )
    result["solve_seconds"] = round(time.perf_counter() - started, 3)
    return result


def describe(
    data: PlanningData, built: Built, values: np.ndarray, objective: int
) -> dict[str, Any]:
    """The plan a solution describes.

    Args:
        data: Planning data.
        built: The model.
        values: Variable values.
        objective: First-stage objective value.

    Returns:
        objective, sites, villages, persons, capex and monthly cost.
    """
    sites = []
    for k in built.sites:
        if values[built.x[k]] < 0.5:
            continue
        site = data.sites[k]
        backhaul = next(kind for (s, kind), v in built.y.items() if s == k and values[v] > 0.5)
        options = data.options[site["site_id"]]
        sites.append(
            {
                "site_id": site["site_id"],
                "backhaul": backhaul,
                "hub": options["microwave"]["detail"] if backhaul == "microwave" else "",
                "power": built.power[k],
            }
        )
    covered = [data.villages[j]["village_id"] for j in built.villages if values[built.c[j]] > 0.5]
    return {
        "objective": objective,
        "sites": sites,
        "villages_covered": covered,
        "persons_covered": value(built, "persons", values),
        "capex_idr": value(built, "capex", values),
        "monthly_idr": value(built, "monthly", values),
    }


def constraint_costs(
    data: PlanningData,
    constraints: dict[str, Any],
    built: Built,
    values: np.ndarray,
    solve: Any,
    objective: int,
) -> list[dict[str, Any]]:
    """Each active constraint's use and the objective gained without it.

    Args:
        data: Planning data.
        constraints: The set.
        built: The solved model.
        values: The plan's variable values.
        solve: Back-end solve function.
        objective: The plan's objective value.

    Returns:
        One entry per active constraint group.
    """
    limits = {
        "must_cover": len(built.must) if built.must else None,
        "max_sites": constraints["max_sites"],
        "capex_budget": constraints["capex_budget_idr"],
        "monthly_opex": constraints["monthly_opex_limit_idr"],
        "min_persons_share": constraints["min_persons_share"],
    }
    total = sum(built.expressions["persons"].values())
    used = {
        "must_cover": len(built.must),
        "max_sites": value(built, "sites", values),
        "capex_budget": value(built, "capex", values),
        "monthly_opex": value(built, "monthly", values),
        "min_persons_share": round(value(built, "persons", values) / total, 4) if total else 0.0,
    }
    out = []
    maximize = built.model.maximize
    for group in GROUPS:
        if limits[group] is None:
            continue
        relaxed = solve(
            build(data, constraints, frozenset({group}), constraints["objective"]).model
        )
        gain = (
            (
                round(relaxed.objective) - objective
                if maximize
                else objective - round(relaxed.objective)
            )
            if relaxed.status == "optimal"
            else None
        )
        out.append(
            {
                "constraint": group,
                "limit": limits[group],
                "used": used[group],
                "binding": gain is not None and gain != 0,
                "objective_gain_without_it": gain,
            }
        )
    return out


def relax_to_feasible(
    data: PlanningData, constraints: dict[str, Any], solve: Any
) -> list[dict[str, Any]]:
    """For an infeasible set: which constraint to relax, and to what.

    Args:
        data: Planning data.
        constraints: The set.
        solve: Back-end solve function.

    Returns:
        One entry per active group whose removal restores a solution, with
        the value that would for the numeric ones (the fewest sites, the
        least capital or monthly cost, the largest share reachable).
    """
    needed = {
        "max_sites": ("min_sites", "sites"),
        "capex_budget": ("min_capex", "capex"),
        "monthly_opex": ("min_monthly", "monthly"),
        "min_persons_share": ("max_persons", "persons"),
    }
    active = {
        "must_cover": True,
        "max_sites": constraints["max_sites"] is not None,
        "capex_budget": constraints["capex_budget_idr"] is not None,
        "monthly_opex": constraints["monthly_opex_limit_idr"] is not None,
        "min_persons_share": constraints["min_persons_share"] is not None,
    }
    out = []
    for group in GROUPS:
        if not active[group]:
            continue
        if group in needed:
            objective, expression = needed[group]
            built = build(data, constraints, frozenset({group}), objective)
            result = solve(built.model)
            if result.status != "optimal":
                continue
            reach = value(built, expression, result.values)
            if group == "min_persons_share":
                total = sum(built.expressions["persons"].values())
                out.append({"constraint": group, "relax_to": round(reach / total, 4)})
            else:
                out.append({"constraint": group, "relax_to": reach})
        else:
            built = build(data, constraints, frozenset({group}), constraints["objective"])
            if solve(built.model).status == "optimal":
                uncoverable = [
                    data.villages[j]["village_id"]
                    for j in built.must
                    if not any(
                        data.cover[t][k, j]
                        for k in built.sites
                        for t in (
                            ("LTE", "GSM")
                            if constraints["technology"] == "both"
                            else (constraints["technology"],)
                        )
                    )
                ]
                out.append({"constraint": group, "uncoverable_villages": uncoverable})
    return out
