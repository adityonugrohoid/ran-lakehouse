"""The HTTP API and its contract (rules A1 to A3, M6, G9)."""

import json
from pathlib import Path
from typing import Any

import pytest
from api_lake import SENTINEL, T0, lake
from fastapi.testclient import TestClient

from ran_lakehouse.api.app import Services, attach, create_app
from ran_lakehouse.api.contract import (
    CONTRACT_VERSION,
    OPENAPI_PATH,
    VERSION_HEADER,
    plan_schema,
    render,
)
from ran_lakehouse.api.whatif import WhatIfEngine
from ran_lakehouse.model import NetworkModel, default_model
from ran_lakehouse.planning.scenarios import constraint_set
from ran_lakehouse.planning.solver import BACKEND, solve_plan
from ran_lakehouse.world import build_world

API_SOURCES = [
    *sorted((Path(__file__).parents[1] / "src/ran_lakehouse/api").glob("*.py")),
    Path(__file__).parents[1] / "src/ran_lakehouse/lake/lineage.py",
]


@pytest.fixture(scope="module")
def tiny() -> NetworkModel:
    return default_model(build_world("tiny"))


@pytest.fixture(scope="module")
def client(tiny: NetworkModel, tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    status = tmp_path_factory.mktemp("runs") / "status.json"
    status.write_text(
        json.dumps(
            {
                "clock": "accelerated x96 (simulated dates)",
                "accelerated": True,
                "speedup": 96.0,
                "simulated_time": T0.isoformat(),
                "wall_time": T0.isoformat(),
                "collector": {},
            }
        )
    )
    services = Services.load(lake(), WhatIfEngine(tiny, 0), status)
    return TestClient(attach(create_app(), services), raise_server_exceptions=False)


def lte_cells(model: NetworkModel) -> list[str]:
    return [
        n for n, t in zip(model.state.cell_names, model.state.technology, strict=True) if t == "LTE"
    ]


def calls(tiny: NetworkModel) -> list[tuple[str, str, Any]]:
    """One call per route, with valid arguments."""
    cell = lte_cells(tiny)[0]
    t = "2026-01-05T17:00:00Z"
    return [
        ("GET", "/v1/clock", None),
        ("GET", "/v1/status", None),
        ("GET", "/status", None),
        ("GET", "/v1/topology", None),
        ("GET", "/v1/cells", None),
        ("GET", "/v1/cells/ENB0001_B3_1", None),
        ("GET", "/v1/kpi-catalog", None),
        (
            "GET",
            "/v1/kpis?cell=ENB0001_B3_1&kpi_id=LTE_ERAB_DROP&formula_version=1&granularity=hour"
            "&start=2026-01-05T17:00:00Z&end=2026-01-06T17:00:00Z",
            None,
        ),
        (
            "GET",
            "/v1/worst-cells?week_start=2026-01-05&kpi_id=LTE_ERAB_DROP&formula_version=1",
            None,
        ),
        ("GET", f"/v1/cm/snapshot?cell=ENB0007_B8_1&at={t}", None),
        ("GET", "/v1/cm/changes?start=2026-01-05T00:00:00Z&end=2026-01-07T00:00:00Z", None),
        ("GET", "/v1/alarms?start=2026-01-05T00:00:00Z&end=2026-01-07T00:00:00Z", None),
        ("GET", "/v1/quality-events?start=2026-01-05T00:00:00Z&end=2026-01-07T00:00:00Z", None),
        (
            "GET",
            "/v1/lineage?kpi_id=LTE_ERAB_DROP&formula_version=1&cell=ENB0001_B3_1&granularity=15m"
            f"&period_start={t}",
            None,
        ),
        ("GET", "/v1/planning/villages", None),
        ("GET", "/v1/planning/candidate-sites", None),
        ("GET", "/v1/planning/coverage", None),
        ("GET", "/v1/planning/backhaul-power-options", None),
        ("GET", "/v1/planning/scenarios", None),
        ("POST", "/v1/plan", constraint_set("whole", "max_persons", "LTE", max_sites=1)),
        (
            "POST",
            "/v1/what-if",
            {"changes": [{"kind": "tilt", "cell": cell, "delta": 1.0, "target": None}]},
        ),
    ]


def test_committed_contract_matches_the_code() -> None:
    assert OPENAPI_PATH.read_text() == render(create_app().openapi()), (
        "contract/openapi.json drifted from the code: "
        "run `uv run python -m ran_lakehouse.api.contract` and bump CONTRACT_VERSION if breaking"
    )
    document = json.loads(OPENAPI_PATH.read_text())
    assert document["info"]["version"] == CONTRACT_VERSION
    assert document["components"]["schemas"]["PlanConstraints"] == plan_schema()


def test_every_route_answers_with_the_version_and_no_evaluation_data(
    client: TestClient, tiny: NetworkModel
) -> None:
    paths = create_app().openapi()["paths"]
    routes = {(method.upper(), path) for path, item in paths.items() for method in item}
    made = calls(tiny)
    assert {
        (m, u.split("?")[0].replace("ENB0001_B3_1", "{cell_name}")) for m, u, _ in made
    } == routes
    for method, url, body in made:
        r = client.request(method, url, json=body)
        assert r.status_code == 200, (url, r.text)
        assert r.headers[VERSION_HEADER] == CONTRACT_VERSION
        assert SENTINEL not in r.text, url
        if r.headers["content-type"].startswith("text/html"):
            assert f"Contract version {CONTRACT_VERSION}" in r.text
            assert "synthetic data" in r.text
            continue
        payload = r.json()
        assert payload["contract_version"] == CONTRACT_VERSION
        assert payload["notice"].startswith("synthetic data")
        assert payload["data"], url
        assert SENTINEL not in r.text, url
        assert "difficulty" not in r.text, url
    cards = client.get("/v1/planning/scenarios").json()["data"]
    assert {tuple(sorted(c)) for c in cards} == {
        ("area", "request", "scenario_id", "x_max_km", "x_min_km", "y_max_km", "y_min_km")
    }


def test_the_api_never_names_the_evaluation_schema() -> None:
    for path in API_SOURCES:
        for line in path.read_text().splitlines():
            assert "lk.evaluation" not in line and "evaluation." not in line.replace(
                "evaluation schema", ""
            ), (path.name, line)


def test_errors_carry_the_contract(client: TestClient) -> None:
    cases = [
        ("/v1/cells/NOPE", 404, "unknown cell NOPE"),
        (
            "/v1/kpis?cell=BTS0011_G900_1&kpi_id=LTE_ERAB_DROP&formula_version=1&granularity=day"
            "&start=2026-01-01&end=2026-01-09",
            422,
            "LTE",
        ),
        (
            "/v1/kpis?cell=ENB0001_B3_1&kpi_id=LTE_ERAB_DROP&formula_version=1&granularity=hour"
            "&start=2026-01-05&end=2026-01-06",
            422,
            "UTC offset",
        ),
        ("/v1/kpis?cell=ENB0001_B3_1", 422, "kpi_id"),
        ("/v1/alarms?start=2026-01-01T00:00:00Z&end=2026-03-01T00:00:00Z", 400, "31 days"),
        (
            "/v1/worst-cells?week_start=2026-01-06&kpi_id=LTE_ERAB_DROP&formula_version=1",
            422,
            "Monday",
        ),
    ]
    for url, status, words in cases:
        r = client.get(url)
        assert r.status_code == status, (url, r.text)
        assert r.headers[VERSION_HEADER] == CONTRACT_VERSION
        body = r.json()
        assert body["contract_version"] == CONTRACT_VERSION
        assert body["error"]["status"] == status and words in body["error"]["error"], body


def test_reads_match_the_lake(client: TestClient) -> None:
    cell = client.get("/v1/cells/ENB0001_B3_1").json()["data"]
    assert (cell["vendor"], cell["band"], cell["n_rb"], cell["site"]) == (
        "huawei",
        "B3",
        100,
        "ENB0001",
    )
    series = client.get(
        "/v1/kpis?cell=ENB0001_B3_1&kpi_id=LTE_ERAB_DROP&formula_version=1&granularity=hour"
        "&start=2026-01-05T17:00:00Z&end=2026-01-05T19:00:00Z"
    ).json()["data"]["values"]
    assert [v["value"] for v in series] == [1.0, 2.0]
    # Nokia-style child DNs join their cell with "/", 3GPP-style ones with ",".
    snapshot = client.get("/v1/cm/snapshot?cell=ENB0007_B8_1&at=2026-01-06T00:00:00Z").json()[
        "data"
    ]
    assert {o["object_class"] for o in snapshot} == {"EUtranCellFDD", "EUtranRelation"}
    changes = client.get(
        "/v1/cm/changes?start=2026-01-05T00:00:00Z&end=2026-01-07T00:00:00Z"
    ).json()["data"]
    assert {c["cell_name"] for c in changes} == {"ENB0001_B3_1", "ENB0007_B8_1"}
    kinds = client.get(
        "/v1/quality-events?start=2026-01-05T00:00:00Z&end=2026-01-07T00:00:00Z"
    ).json()["data"]
    assert {q["kind"] for q in kinds} == {"missing_period", "late_file", "changed_redelivery"}
    trace = client.get(
        "/v1/lineage?kpi_id=LTE_ERAB_DROP&formula_version=1&cell=ENB0001_B3_1&granularity=15m"
        "&period_start=2026-01-05T17:00:00Z"
    ).json()["data"]
    assert {(t["measurement"], t["bronze_counter"], t["file_hash"][:1]) for t in trace} == {
        ("ERAB.RelActNbr.sum", "C1", "a"),
        ("ERAB.EstabInitSuccNbr.sum", "C2", "a"),
    }


def test_plan_solve_is_the_solver(client: TestClient) -> None:
    constraints = constraint_set("whole", "max_persons", "LTE", max_sites=1)
    served = client.post("/v1/plan", json=constraints).json()["data"]
    direct = solve_plan(client.app.state.services.planning, constraints, BACKEND)  # type: ignore[attr-defined]
    assert served["backend"] == BACKEND
    for key in ("status", "objective", "sites", "villages_covered", "capex_idr", "monthly_idr"):
        assert served[key] == direct[key], key
    bad = constraints | {"max_sites": -1}
    r = client.post("/v1/plan", json=bad)
    assert r.status_code == 422 and "max_sites" in r.json()["error"]["error"]


def change(kind: str, cell: str, delta: float, target: str | None) -> dict[str, Any]:
    return {"kind": kind, "cell": cell, "delta": delta, "target": target}


def test_what_if_refuses_changes_beyond_the_bounds(client: TestClient, tiny: NetworkModel) -> None:
    lte = lte_cells(tiny)
    names = tiny.state.cell_names
    source, target = next(
        (names[a], names[b]) for a, b in sorted(tiny.neighbours) if names[a] in lte
    )
    gsm = next(n for n, t in zip(names, tiny.state.technology, strict=True) if t == "GSM")
    refused = [
        ([change("tilt", lte[0], 2.5, None)], "tilt step 2.5 beyond"),
        ([change("power", lte[0], -3.5, None)], "power step -3.5 beyond"),
        ([change("cio", lte[0], 3.0, None)] * 3, "beyond the bounds"),
        ([change("cio", lte[0], 4.0, None)], "beyond the bounds"),
        ([change("add_neighbour", source, 0.0, target)], "already configured"),
        ([change("remove_neighbour", lte[0], 0.0, None)], "needs a target"),
        ([change("tilt", lte[0], 1.0, lte[1])], "takes no target"),
        ([change("tilt", gsm, 1.0, None)], "LTE KPIs only"),
        ([change("tilt", "NOPE", 1.0, None)], "unknown cell"),
        ([change("azimuth", lte[0], 1.0, None)], "kind"),
    ]
    for changes, words in refused:
        r = client.post("/v1/what-if", json={"changes": changes})
        assert r.status_code == 422, (changes, r.text)
        assert words in r.json()["error"]["error"], r.json()


def test_what_if_replays_with_common_noise(client: TestClient, tiny: NetworkModel) -> None:
    cell = lte_cells(tiny)[0]
    same = client.post("/v1/what-if", json={"changes": [change("tilt", cell, 0.0, None)]})
    data = same.json()["data"]
    assert data["touched_cells_total"]["before"] == data["touched_cells_total"]["after"]
    moved = client.post("/v1/what-if", json={"changes": [change("power", cell, -3.0, None)]})
    result = moved.json()["data"]
    changed = [c for c in result["cells"] if c["changed"]]
    assert [c["cell_name"] for c in changed] == [cell]
    assert changed[0]["before"] != changed[0]["after"]
    assert len(result["cells"]) > 1, "a power cut must touch the cells around it"


def test_status_flags_the_d_cases_by_kind_and_count(client: TestClient) -> None:
    status = client.get("/v1/status").json()["data"]
    counts = {(d["rule"], d["kind"]): d["count"] for d in status["d_cases"]}
    assert counts[("D1", "late files")] == 1
    assert counts[("D2", "files redelivered with changed content")] == 1
    assert counts[("D3", "missing periods per element")] == 1
    assert counts[("D5", "counters mapped across a rename")] == 1
    assert counts[("D6", "KPIs with more than one formula version")] == 0
    assert status["clock"].startswith("accelerated x96")
    assert {(f["ems"], f["kind"]) for f in status["files"]} == {("EMS-HW-01", "PM")}
    rows = {(x["layer"], x["table"]): x["rows"] for x in status["layers"]}
    assert rows[("silver", "pm_measurements")] == 2
    assert status["latest_quality_events"]


def test_plan_sets_of_any_minor_version_are_accepted(client: TestClient) -> None:
    constraints = constraint_set("whole", "max_persons", "LTE", max_sites=1)
    for version, status in (("1.0.0", 200), (CONTRACT_VERSION, 200), ("2.0.0", 422)):
        r = client.post("/v1/plan", json=constraints | {"schema_version": version})
        assert r.status_code == status, (version, r.text)
