"""Services for a warehouse, and `ranlake serve`."""

import logging
from pathlib import Path

from fastapi import FastAPI

from ran_lakehouse.api import queries
from ran_lakehouse.api.app import Services, attach, create_app
from ran_lakehouse.api.whatif import WhatIfEngine
from ran_lakehouse.lake.catalog import connect
from ran_lakehouse.model import RUN_START, NetworkModel, default_model
from ran_lakehouse.world import build_world

logger = logging.getLogger(__name__)


def live_services(warehouse: str, base: NetworkModel, run_weeks: int, runs: Path) -> Services:
    """Services reading a warehouse, with what-if on the run's network.

    The what-if week is the one after gold's last day.

    Args:
        warehouse: Lakekeeper warehouse name.
        base: The run's network as built.
        run_weeks: Weeks of the run.
        runs: Directory of run status files (runs/<warehouse>/status.json).

    Returns:
        The services.

    Raises:
        RuntimeError: If gold has no KPI days yet.
    """
    con = connect(warehouse)
    last = queries.lake_clock(con)["gold_latest_day"]
    if last is None:
        raise RuntimeError(f"warehouse {warehouse} has no gold KPI days; build gold first")
    first_day = (last - RUN_START.date()).days + 1
    logger.info("what-if replays from day %d", first_day)
    engine = WhatIfEngine.for_run(base, run_weeks, first_day)
    return Services.load(con, engine, runs / warehouse / "status.json")


def app_for(warehouse: str, profile: str, run_weeks: int, runs: Path) -> FastAPI:
    """The API over a warehouse.

    Args:
        warehouse: Lakekeeper warehouse name.
        profile: World profile of the run.
        run_weeks: Weeks of the run.
        runs: Directory of run status files.

    Returns:
        The app.
    """
    logger.info("building the %s network", profile)
    base = default_model(build_world(profile))
    return attach(create_app(), live_services(warehouse, base, run_weeks, runs))
