"""The recorded sample carries no planted answer (rules A3, A4)."""

import copy

import pytest

from ran_lakehouse.api.sample import (
    FAULTS,
    PLANS,
    check_no_answers,
    sample_cells,
    what_if_changes,
    window_faults,
)
from ran_lakehouse.faults.evaluate import right_fix
from ran_lakehouse.faults.plant import Fault, plan_faults
from ran_lakehouse.model import NetworkModel, default_model
from ran_lakehouse.planning.build import build_plan, tables
from ran_lakehouse.planning.scenarios import scenarios
from ran_lakehouse.planning.solver import BACKEND, PlanningData, solve_plan
from ran_lakehouse.world import build_world

RUN_WEEKS = 13
LAST_GOLD_DAY = 83


@pytest.fixture(scope="module")
def demo() -> tuple[NetworkModel, list[Fault], PlanningData]:
    base = default_model(build_world("demo"))
    planning = PlanningData.from_tables(tables(build_plan(base)))
    return base, plan_faults(base, RUN_WEEKS), planning


def test_the_sample_records_no_answer(demo: tuple[NetworkModel, list[Fault], PlanningData]) -> None:
    base, faults, planning = demo
    _, chosen = window_faults(faults, LAST_GOLD_DAY)
    assert len({f.kind for f in chosen}) == FAULTS
    cells = sample_cells(base, chosen)
    assert 25 <= len(cells) <= 30
    assert {base.state.technology[i] for i in cells} == {"LTE", "GSM"}
    changes = what_if_changes(base, faults, cells)
    plans = [solve_plan(planning, p, BACKEND) for p in PLANS]
    check_no_answers(base, faults, changes, planning, list(PLANS), plans)
    touched = {base.state.cell_names[f.cell] for f in faults}
    assert not {c["cell"] for call in changes for c in call} & touched


def test_the_check_refuses_a_card_or_a_right_fix(
    demo: tuple[NetworkModel, list[Fault], PlanningData],
) -> None:
    base, faults, planning = demo
    card = scenarios(planning)[0].constraints
    with pytest.raises(RuntimeError, match="equals a card"):
        check_no_answers(base, faults, [], planning, [card], [solve_plan(planning, card, BACKEND)])
    fixed = next(f for f in faults if right_fix(base, f))
    step = right_fix(base, fixed)[0]
    names = base.state.cell_names
    call = [
        {
            "kind": step.kind,
            "cell": names[step.cell],
            "delta": step.delta,
            "target": names[step.target] if step.target >= 0 else None,
        }
    ]
    plan = copy.deepcopy(PLANS[0])
    with pytest.raises(RuntimeError, match="right fix"):
        check_no_answers(
            base, faults, [call], planning, [plan], [solve_plan(planning, plan, BACKEND)]
        )
