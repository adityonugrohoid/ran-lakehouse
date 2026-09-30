"""Request and response bodies of the API (rule A2), as they appear in the
OpenAPI contract. Every response wraps its data with the contract version
and the synthetic-data notice.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ran_lakehouse.faults.whatif import (
    CIO_RANGE_DB,
    MAX_CIO_STEP_DB,
    MAX_POWER_STEP_DB,
    MAX_TILT_STEP_DEG,
)

SYNTHETIC_NOTICE = "synthetic data: a generated network, not a real operator"


class Strict(BaseModel):
    """A body with no extra fields."""

    model_config = ConfigDict(extra="forbid")


class Envelope[T](Strict):
    """Every response: the contract version, the notice and the data."""

    contract_version: str
    notice: str
    data: T


class ErrorBody(Strict):
    """What went wrong."""

    status: int
    error: str


class Cell(Strict):
    """A cell as its CM and gold describe it."""

    cell_name: str
    site: str
    ems: str
    vendor: str | None
    technology: Literal["LTE", "GSM"]
    band: str | None
    bandwidth_mhz: float | None
    n_rb: int | None = Field(description="LTE downlink resource blocks (TS 36.101 Table 5.6-1)")
    dn: str


class Site(Strict):
    """A site and its cells."""

    site: str
    ems: str
    vendor: str | None
    cells: list[str]


class Relation(Strict):
    """A configured neighbour relation."""

    source: str
    target: str


class Topology(Strict):
    """Sites, cells and the neighbour relations of the latest CM snapshot."""

    sites: list[Site]
    relations: list[Relation]


class KpiDefinition(Strict):
    """One KPI formula version (rule L3)."""

    kpi_id: str
    formula_version: int
    name: str
    technology: str
    formula: str
    source: str
    unit: str
    granularities: str
    vendors: str
    better: str
    breach_threshold: float | None
    effective_from: date | None
    operators_differ: str | None


class KpiValue(Strict):
    """One KPI value with how complete and trustworthy its inputs were."""

    period: datetime | date = Field(
        description="UTC period start for 15m and hour; WIB date (Monday for week) otherwise"
    )
    vendor: str
    value: float | None
    numerator: float | None
    denominator: float | None
    periods_expected: int
    periods_reported: int
    coverage: float
    suspect_share: float


class KpiSeries(Strict):
    """KPI values of one cell over a range."""

    cell_name: str
    kpi_id: str
    formula_version: int
    granularity: Literal["15m", "hour", "day", "week"]
    values: list[KpiValue]


class WorstCell(Strict):
    """One row of a week's worst-cell ranking."""

    rank: int
    cell_name: str
    week_value: float | None
    breach_threshold: float | None
    better: str
    days_judged: int
    breach_days: int
    persistence_n: int
    persistence_m: int


class CmObject(Strict):
    """A managed object's attributes in a CM snapshot."""

    snapshot_time: datetime
    object_class: str
    dn: str
    attributes: dict[str, Any]


class CmChange(Strict):
    """One CM change log entry."""

    time: datetime
    cell_name: str
    ems: str
    object_class: str
    dn: str
    attribute: str
    old_value: str | None
    new_value: str | None


class Alarm(Strict):
    """One alarm notification."""

    notification_id: str
    notification_type: str
    alarm_id: str
    event_time: datetime
    raised_time: datetime | None
    cleared_time: datetime | None
    alarm_type: str | None
    perceived_severity: str | None
    probable_cause: str | None
    specific_problem: str | None
    cell_name: str | None
    ems: str
    object_instance: str


class QualityEvent(Strict):
    """A delivery problem the pipeline recorded."""

    kind: Literal["missing_period", "late_file", "duplicate_delivery", "changed_redelivery"]
    ems: str
    managed_element: str | None
    period_start: datetime
    period_end: datetime
    file_name: str | None
    detail: str


class LineageRow(Strict):
    """One silver row and source counter behind a gold KPI value."""

    kpi_value: float | None
    coverage: float | None
    suspect_share: float | None
    period_start: datetime
    object_dn: str
    measurement: str
    bin: int | None
    silver_value: float | None
    derived: bool | None
    suspect: bool | None
    late: bool | None
    conflict: bool | None
    versions: int | None
    dictionary_release: str | None
    bronze_counter: str | None
    bronze_value: float | None
    managed_element: str | None
    parser_version: str | None
    load_id: str | None
    file_name: str | None
    file_hash: str | None
    arrival_time: datetime | None
    size_bytes: int | None


class LiveRun(Strict):
    """A live run's clock, from its status file."""

    clock: str
    accelerated: bool
    speedup: float
    simulated_time: datetime
    wall_time: datetime


class ClockStatus(Strict):
    """Where the run and each lake layer have got."""

    live_run: LiveRun | None
    bronze_latest_arrival: datetime | None
    silver_complete_to: datetime | None
    gold_latest_day: date | None
    what_if_week_start: date | None = Field(
        description="First WIB day of the week a what-if replays (the day after gold ends)"
    )


class Village(Strict):
    """A village of the expansion area (rule G7)."""

    village_id: int
    x_km: float
    y_km: float
    population: int
    schools: int
    elevation_m: float
    served_lte_rsrp_dbm: float | None
    served_gsm_rxlev_dbm: float | None
    covered_today_lte: bool
    covered_today_gsm: bool


class CandidateSite(Strict):
    """A candidate site (rule G7)."""

    site_id: str
    x_km: float
    y_km: float
    elevation_m: float
    nearest_village_km: float
    persons_nearby: int
    build_cost_idr: int
    grid_distance_km: float
    fiber_distance_km: float


class Coverage(Strict):
    """Indoor service of a village from a candidate site (rule G7)."""

    site_id: str
    village_id: int
    technology: str
    distance_km: float
    hata_db: float | None
    diffraction_db: float | None
    signal_dbm: float | None
    covered: bool


class BackhaulPowerOption(Strict):
    """A backhaul or power option of a candidate site (rule G7)."""

    site_id: str
    kind: str
    available: bool
    distance_km: float | None
    capacity_mbps: float | None
    capex_idr: int
    monthly_idr: int
    detail: str
    clears_full_fresnel: bool


class ScenarioCard(Strict):
    """A planning request as written, with its area (rule G8)."""

    scenario_id: str
    area: str
    x_min_km: float
    x_max_km: float
    y_min_km: float
    y_max_km: float
    request: str


class PlanSite(Strict):
    """A chosen site with its backhaul and power."""

    site_id: str
    backhaul: str
    hub: str
    power: str


class ConstraintCost(Strict):
    """What one constraint costs the objective (by solving again without it)."""

    constraint: str
    limit: float | None
    used: float | None
    binding: bool
    objective_gain_without_it: float | None


class Relaxation(Strict):
    """A constraint whose relaxation makes an infeasible set feasible."""

    constraint: str
    relax_to: float | None = None
    uncoverable_villages: list[int] | None = None


class Plan(Strict):
    """A solved plan, or why there is none (rule G9)."""

    backend: str
    status: Literal["optimal", "infeasible", "time_limit"]
    objective: float | None = None
    sites: list[PlanSite] = []
    villages_covered: list[int] = []
    persons_covered: int | None = None
    capex_idr: int | None = None
    monthly_idr: int | None = None
    constraints: list[ConstraintCost] = []
    relax_to_feasible: list[Relaxation] = []
    solve_seconds: float


class ChangeIn(Strict):
    """One bounded change (rule M6)."""

    kind: Literal["tilt", "power", "cio", "add_neighbour", "remove_neighbour"]
    cell: str
    delta: float = Field(
        description=(
            f"tilt within +/-{MAX_TILT_STEP_DEG:g} deg, power within +/-{MAX_POWER_STEP_DB:g} dB, "
            f"cio moved at most {MAX_CIO_STEP_DB:g} dB and kept within +/-{CIO_RANGE_DB:g} dB; "
            "0 for neighbour changes"
        )
    )
    target: str | None = Field(description="Neighbour target for neighbour changes, else null")


class WhatIfRequest(Strict):
    """Changes to replay."""

    changes: list[ChangeIn] = Field(min_length=1, max_length=10)


class CellKpis(Strict):
    """Next-week LTE KPIs of one cell (or all touched cells), before and after."""

    cell_name: str
    changed: bool
    before: dict[str, float | None]
    after: dict[str, float | None]


class WhatIfResult(Strict):
    """The replayed week before and after the changes."""

    week_start: date
    changes: list[ChangeIn]
    cells: list[CellKpis]
    touched_cells_total: CellKpis
