"""Planning scenarios and solver report (rules G8, G9).

Solves every scenario card with each back end (HiGHS, CP-SAT and SCIP,
all through OR-Tools MathOpt), compares time and proof of optimality, and
checks that the back end in solver.BACKEND is the measured choice. Writes
the public cards to gold.planning_scenarios and the exact optima to the
evaluation-only table evaluation.planning_answers (rule A3), and records one
worked scenario in full, the counts per difficulty and a map of one plan.
Writes results/planning_scenarios.json, results/planning_scenarios.md and
results/planning_plan.png.
Run: `uv run python -m ran_lakehouse.planning.scenario_report --warehouse demo`.
"""

import argparse
import json
import resource
import statistics
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ran_lakehouse.lake.catalog import connect, write
from ran_lakehouse.model import default_model
from ran_lakehouse.planning import solver
from ran_lakehouse.planning.build import Plan, build_plan, tables, write_gold
from ran_lakehouse.planning.report import hillshade
from ran_lakehouse.planning.scenarios import Scenario, answers_table, cards_table, scenarios
from ran_lakehouse.world import build_world

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "planning_scenarios.json"
RECORD_MD = RESULTS / "planning_scenarios.md"
FIG_PLAN = RESULTS / "planning_plan.png"
PROFILE = "demo"
REPEATS = 3  # timing runs per back end and card; the median is kept
WORKED = "S15"  # the scenario shown in full (hard: must-cover and opex both bind)
ANSWERS = "lk.evaluation.planning_answers"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
COVERED = "#1baf7a"
NOT_COVERED = "#b9b8b3"
SITE = "#0b0b0b"
MICROWAVE = "#2a78d6"
FIBER = "#7b3fbf"
SATELLITE = "#eb6834"


def compare(data: solver.PlanningData, cards: list[Scenario]) -> dict[str, Any]:
    """Solve every card with every back end.

    Args:
        data: Planning data.
        cards: The scenarios.

    Returns:
        Per back end: total and largest median seconds, cards proven
        (optimal or infeasible) and cards stopped at the time limit; and
        whether all back ends agree on every card's status and objective.
    """
    out: dict[str, Any] = {}
    answers: dict[str, list[tuple[str, Any]]] = {}
    for backend in solver.BACKENDS:
        seconds, proven, limited, results = [], 0, 0, []
        for card in cards:
            runs = [solver.solve_plan(data, card.constraints, backend) for _ in range(REPEATS)]
            seconds.append(statistics.median(r["solve_seconds"] for r in runs))
            status = runs[0]["status"]
            proven += status in ("optimal", "infeasible")
            limited += status == "time_limit"
            results.append((status, runs[0].get("objective")))
        answers[backend] = results
        out[backend] = {
            "total_s": round(sum(seconds), 3),
            "largest_s": round(max(seconds), 3),
            "proven": proven,
            "time_limit": limited,
        }
    first = next(iter(answers.values()))
    return {
        "backends": out,
        "agree": all(a == first for a in answers.values()),
        "repeats": REPEATS,
        "time_limit_s": solver.TIME_LIMIT_S,
    }


def chosen(comparison: dict[str, Any], cards: int) -> str:
    """The back end the numbers pick: every card proven, then the least total time.

    Args:
        comparison: compare()'s result.
        cards: Number of cards.

    Returns:
        The back end.

    Raises:
        RuntimeError: If no back end proves every card, or the pick is not
            solver.BACKEND.
    """
    proving = {b: v for b, v in comparison["backends"].items() if v["proven"] == cards}
    if not proving:
        raise RuntimeError("no back end proves every scenario")
    pick: str = min(proving, key=lambda b: proving[b]["total_s"])
    if pick != solver.BACKEND:
        raise RuntimeError(
            f"measurement picks {pick} but solver.BACKEND is {solver.BACKEND}; update it"
        )
    return pick


def plot_plan(
    plan: Plan, data: solver.PlanningData, card: Scenario, result: dict[str, Any]
) -> None:
    """One plan on the map: chosen sites, covered villages, backhaul links.

    Args:
        plan: The planning data as built (terrain, lines, hubs).
        data: Planning data.
        card: The scenario.
        result: Its solution.
    """
    fig, ax = plt.subplots(figsize=(6.4, 9.6), facecolor=SURFACE)
    hillshade(ax, plan)
    area = card.constraints["area"]
    covered = set(result["villages_covered"])
    inside = [v for v in data.villages if solver.inside(v, area)]
    for is_covered, color, label in (
        (False, NOT_COVERED, "village, not covered"),
        (True, COVERED, "village, covered"),
    ):
        pick = [v for v in inside if (v["village_id"] in covered) == is_covered]
        ax.scatter(
            [v["x_km"] for v in pick],
            [v["y_km"] for v in pick],
            s=[4 + v["population"] / 120 for v in pick],
            color=color,
            edgecolor="white",
            linewidth=0.5,
            label=label,
            zorder=3,
        )
    sites = {s["site_id"]: s for s in data.sites}
    hubs = {h[0]: h for h in plan.hubs}
    ax.plot(
        plan.fiber_route[:, 0],
        plan.fiber_route[:, 1],
        color=FIBER,
        linewidth=1.5,
        linestyle="--",
        label="fiber route",
    )
    drawn: set[str] = set()
    for chosen_site in result["sites"]:
        s = sites[chosen_site["site_id"]]
        kind = chosen_site["backhaul"]
        if kind == "microwave":
            hub = hubs[chosen_site["hub"]]
            ax.plot(
                [s["x_km"], hub[1]],
                [s["y_km"], hub[2]],
                color=MICROWAVE,
                linewidth=1.2,
                label=None if "mw" in drawn else "microwave hop",
                zorder=4,
            )
            ax.scatter([hub[1]], [hub[2]], marker="s", s=24, color=INK_SECONDARY, zorder=4)
            drawn.add("mw")
        color = {"microwave": MICROWAVE, "fiber": FIBER, "satellite": SATELLITE}[kind]
        ax.scatter(
            [s["x_km"]],
            [s["y_km"]],
            marker="^",
            s=70,
            color=color,
            edgecolor=SITE,
            linewidth=0.8,
            zorder=5,
            label=None if kind in drawn else f"site, {kind}",
        )
        drawn.add(kind)
    ax.plot(
        [area["x_min_km"], area["x_max_km"], area["x_max_km"], area["x_min_km"], area["x_min_km"]],
        [area["y_min_km"], area["y_min_km"], area["y_max_km"], area["y_max_km"], area["y_min_km"]],
        color=INK,
        linewidth=0.8,
        linestyle=":",
    )
    ax.set_xlabel("x (km)", color=INK_SECONDARY)
    ax.set_ylabel("y (km)", color=INK_SECONDARY)
    ax.set_title(
        f"Synthetic: plan for {card.scenario_id} ({card.constraints['technology']}),"
        f" {len(result['sites'])} sites,\n{result['persons_covered']:,} persons covered",
        color=INK,
        fontsize=10,
    )
    ax.legend(loc="lower right", fontsize=7, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(FIG_PLAN, facecolor=SURFACE, dpi=110)
    plt.close(fig)


def build(warehouse: str) -> dict[str, Any]:
    """Solve the scenarios, write gold and evaluation, and make the record.

    Args:
        warehouse: Warehouse for the tables.

    Returns:
        The record.
    """
    started = time.perf_counter()
    plan = build_plan(default_model(build_world(PROFILE)))
    planning_tables = tables(plan)
    data = solver.PlanningData.from_tables(planning_tables)
    cards = scenarios(data)
    comparison = compare(data, cards)
    backend = chosen(comparison, len(cards))
    answers, results = answers_table(data, cards, backend)
    con = connect(warehouse)
    write_gold(con, planning_tables | {"planning_scenarios": cards_table(cards)})
    con.execute("CREATE SCHEMA IF NOT EXISTS lk.evaluation")
    con.register("answers", answers)
    try:
        write(con, f"DROP TABLE IF EXISTS {ANSWERS}")
        write(con, f"CREATE TABLE {ANSWERS} AS SELECT * FROM answers")
    finally:
        con.unregister("answers")
        con.close()
    by_id = {c.scenario_id: (c, r) for c, r in zip(cards, results, strict=True)}
    card, worked = by_id[WORKED]
    plot_plan(plan, data, card, worked)
    counts = Counter((c.difficulty, r["status"]) for c, r in zip(cards, results, strict=True))
    return {
        "profile": PROFILE,
        "scenarios": len(cards),
        "solver_comparison": comparison,
        "backend": backend,
        "counts": {f"{d} / {s}": n for (d, s), n in sorted(counts.items())},
        "infeasible_answers": {
            c.scenario_id: r["relax_to_feasible"]
            for c, r in zip(cards, results, strict=True)
            if r["status"] == "infeasible"
        },
        "worked": {
            "scenario_id": card.scenario_id,
            "difficulty": card.difficulty,
            "request": card.request,
            "constraints": card.constraints,
            "plan": worked,
        },
        "cards": [
            {"scenario_id": c.scenario_id, "area": c.area, "request": c.request} for c in cards
        ],
        "solves_rerun_after_clock_step": solver.clock_step_reruns["count"],
        "timing_s": round(time.perf_counter() - started, 1),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
    }


def relaxation(entry: dict[str, Any]) -> str:
    """One way to make an infeasible card feasible, in words.

    Args:
        entry: A relax_to_feasible entry.

    Returns:
        The text.
    """
    if "relax_to" in entry:
        return f"relax {entry['constraint']} to {entry['relax_to']}"
    villages = entry["uncoverable_villages"]
    if villages:
        return (
            f"drop {entry['constraint']}: no candidate serves villages "
            f"{', '.join(str(v) for v in villages)}"
        )
    return f"relax {entry['constraint']}: its villages cannot all be served within the rest"


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    comparison = record["solver_comparison"]
    w = record["worked"]
    plan = w["plan"]
    lines = [
        "# Planning scenarios and solver report",
        "",
        f"Synthetic expansion area of the {record['profile']} profile (rules G8, G9). "
        f"{record['scenarios']} scenario cards: a written request and an area are public",
        "(gold.planning_scenarios); the constraint set each implies and its exact optimum are",
        "evaluation-only (evaluation.planning_answers, rule A3). Constraint schema:",
        "`contract/plan_constraints.schema.json` (version 1.0.0). Written by",
        "`python -m ran_lakehouse.planning.scenario_report` from `planning_scenarios.json`.",
        "",
        "## Solver comparison",
        "",
        "One 0-1 integer program, built once as an OR-Tools MathOpt model (OR-Tools",
        "9.15.6755) and solved to a zero gap by each bundled solver: the objective first, then",
        "the cheapest plan at that optimum, then each constraint relaxed in turn to price it.",
        "HiGHS and SCIP get each row divided by its largest coefficient (unscaled IDR rows",
        "near 1e10 made SCIP stop 40 persons short on S16); CP-SAT solves in integers. Every",
        "plan returned is checked against the unscaled integer rows.",
        f"Median of {comparison['repeats']} runs per card; time limit "
        f"{comparison['time_limit_s']:g} s per solve.",
        "",
        "| Back end | Total s | Largest card s | Proven (optimal or infeasible) | Time limit |",
        "|---|---|---|---|---|",
        *[
            f"| {b} | {v['total_s']} | {v['largest_s']} | {v['proven']} of "
            f"{record['scenarios']} | {v['time_limit']} |"
            for b, v in comparison["backends"].items()
        ],
        "",
        f"All back ends agree on every card's status and objective: "
        f"{'yes' if comparison['agree'] else 'NO'}. Chosen: **{record['backend']}** (every "
        "card proven, least total time); it serves both the stored optima and the plan solve.",
        "",
        "## Scenarios",
        "",
        "| Difficulty / status | Cards |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["counts"].items()],
        "",
        "| Card | Area | Request |",
        "|---|---|---|",
        *[f"| {c['scenario_id']} | {c['area']} | {c['request']} |" for c in record["cards"]],
        "",
        "Infeasible cards and what to relax:",
        "",
        *[
            f"- {sid}: " + "; ".join(relaxation(r) for r in relax)
            for sid, relax in record["infeasible_answers"].items()
        ],
        "",
        f"## Worked scenario: {w['scenario_id']} ({w['difficulty']})",
        "",
        f'Request: "{w["request"]}"',
        "",
        "Constraint set it implies:",
        "",
        "```json",
        json.dumps(w["constraints"], indent=2),
        "```",
        "",
        f"Plan: {len(plan['sites'])} sites, {len(plan['villages_covered'])} villages and "
        f"{plan['persons_covered']:,} persons covered, capital cost {plan['capex_idr']:,} IDR, "
        f"monthly cost {plan['monthly_idr']:,} IDR.",
        "",
        "| Site | Backhaul | Hub | Power |",
        "|---|---|---|---|",
        *[
            f"| {s['site_id']} | {s['backhaul']} | {s['hub']} | {s['power']} |"
            for s in plan["sites"]
        ],
        "",
        "Cost of each constraint (the objective gained by solving again without it; integer",
        "programs have no dual prices). A constraint is binding when that gain is not zero;",
        "with whole sites it can bind while the plan leaves some of it unused:",
        "",
        "| Constraint | Limit | Used | Binding | Objective gain without it |",
        "|---|---|---|---|---|",
        *[
            f"| {c['constraint']} | {c['limit']} | {c['used']} | {'yes' if c['binding'] else 'no'}"
            f" | {c['objective_gain_without_it']} |"
            for c in plan["constraints"]
        ],
        "",
        "![Plan](planning_plan.png)",
        "",
        "## Cost",
        "",
        f"Report run {record['timing_s']} s, peak RSS {record['peak_rss_mb']:,} MB (network "
        "model, planning data, every card solved by every back end). Solves rerun after a",
        f"wall-clock step of the build machine: {record['solves_rerun_after_clock_step']}.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Solve, then write the record, report and figure.

    Args:
        argv: Command-line arguments, or None for sys.argv.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True, help="warehouse for the tables")
    args = parser.parse_args(argv)
    RESULTS.mkdir(exist_ok=True)
    RECORD_JSON.write_text(json.dumps(build(args.warehouse), indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
