"""The compose app service (rule S3): build the demo from files to the API, then serve.

On start it brings one warehouse up to date, step by step, and then serves
the API (rule A). Each step only does what is missing, so a restart builds
nothing twice:
- bronze: if the warehouse has no delivered files yet, backfill the history
  (generate, deliver and collect the files);
- silver and gold: build every partition and day not yet built (both are
  incremental);
- planning: write the planning tables, the public scenario cards and the
  evaluation-only answers if the cards are missing;
- serve the API on the configured port.

Settings come from the environment and are all required (compose sets them):
RANLAKE_WAREHOUSE, RANLAKE_PROFILE, RANLAKE_HISTORY_WEEKS, RANLAKE_RUN_WEEKS
and RANLAKE_PORT.

    python -m ran_lakehouse.bootstrap
"""

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from ran_lakehouse.api.serve import app_for
from ran_lakehouse.collect.backfill import drive
from ran_lakehouse.lake import silver
from ran_lakehouse.lake.catalog import connect
from ran_lakehouse.lake.gold import KPI_REVISION, GoldBuild, Target
from ran_lakehouse.model import default_model
from ran_lakehouse.planning import solver
from ran_lakehouse.planning.build import build_plan, tables
from ran_lakehouse.planning.scenario_report import write_tables
from ran_lakehouse.planning.scenarios import answers_table, scenarios
from ran_lakehouse.world import build_world

logger = logging.getLogger(__name__)

LANDING = Path("landing")
RUNS = Path("runs")
# The API listens on every interface of the container; compose publishes the
# port on the host's loopback only.
HOST = "0.0.0.0"


def setting(name: str) -> str:
    """A required setting from the environment.

    Args:
        name: Variable name.

    Returns:
        Its value.

    Raises:
        RuntimeError: If it is not set.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; compose.yaml sets it for the app service")
    return value


def table_rows(warehouse: str, table: str) -> int:
    """Rows of a lake table, or 0 when the table does not exist yet.

    Args:
        warehouse: Warehouse name.
        table: Schema-qualified table under "lk".

    Returns:
        Row count.
    """
    con = connect(warehouse)
    schema, name = table.split(".")
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_catalog = 'lk' AND table_schema = $s AND table_name = $t",
        {"s": schema, "t": name},
    ).fetchone()
    if exists is None or exists[0] == 0:
        con.close()
        return 0
    count = con.execute(f"SELECT count(*) FROM lk.{table}").fetchone()
    con.close()
    return 0 if count is None else int(count[0])


def bring_up(warehouse: str, profile: str, history_weeks: int, run_weeks: int) -> None:
    """Build what is missing in the warehouse, from files to gold and planning.

    Args:
        warehouse: Warehouse name.
        profile: World profile.
        history_weeks: Weeks of history to backfill.
        run_weeks: Weeks of the run (the plans' length).
    """
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    if table_rows(warehouse, "bronze.file_arrivals") == 0:
        logger.info("backfilling %d weeks of %s into %s", history_weeks, profile, warehouse)
        drive(profile, run_weeks, 0, 7 * history_weeks, warehouse, LANDING, None)
    else:
        logger.info("bronze already holds the history; no backfill")
    con = connect(warehouse)
    silver.create_tables(con, True)
    logger.info("silver: %s", silver.SilverBuild(con, f"silver-{stamp}", silver.GRACE).run(None))
    con.close()
    gold_build = GoldBuild(Target("lake", warehouse), f"gold-{stamp}", KPI_REVISION)
    logger.info("gold: %s", gold_build.run())
    if table_rows(warehouse, "gold.planning_scenarios") == 0:
        logger.info("writing the planning tables and scenario cards")
        planning_tables = tables(build_plan(default_model(build_world(profile))))
        data = solver.PlanningData.from_tables(planning_tables)
        cards = scenarios(data)
        answers, _ = answers_table(data, cards, solver.BACKEND)
        con = connect(warehouse)
        write_tables(con, planning_tables, cards, answers)
        con.close()


def main() -> None:
    """Bring the warehouse up, then serve the API."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    warehouse = setting("RANLAKE_WAREHOUSE")
    profile = setting("RANLAKE_PROFILE")
    run_weeks = int(setting("RANLAKE_RUN_WEEKS"))
    bring_up(warehouse, profile, int(setting("RANLAKE_HISTORY_WEEKS")), run_weeks)
    port = int(setting("RANLAKE_PORT"))
    logger.info("serving %s on port %d", warehouse, port)
    uvicorn.run(app_for(warehouse, profile, run_weeks, RUNS), host=HOST, port=port)


if __name__ == "__main__":
    main()
