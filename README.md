# ran-lakehouse

A mini telecom lakehouse and data generator for a synthetic Indonesian
multi-vendor operator running 4G (LTE) and 2G (GSM). Simulated vendor
systems drop 3GPP performance files every 15 minutes; a pipeline takes
them through bronze, silver and gold; an API serves KPIs, configuration,
alarms, topology and a what-if simulator. Every data-quality problem that
breaks real operator pipelines is planted on purpose and guarded by a
test. The network is synthetic, not a real place, operator or network.

## Quickstart

```bash
git clone https://github.com/adityonugrohoid/ran-lakehouse.git
cd ran-lakehouse
```

One command runs the demo from files to API: the stack starts, the app
generates two weeks of synthetic history, delivers and collects the files,
builds bronze, silver, gold and the planning tables, then serves the API on
`http://127.0.0.1:8000` (`/status` for the pipeline page). From an empty
warehouse this took 41 minutes on the build machine; a restart only builds
what is missing.

```bash
docker compose up -d
docker compose logs -f app   # "serving compose-demo on port 8000" when ready
curl "http://127.0.0.1:8000/v1/kpis?cell=ENB0001_B3_1&kpi_id=LTE_ERAB_DROP&formula_version=1&granularity=day&start=2026-01-05&end=2026-01-12"
```

To drive each step by hand instead, on the host: start the lake stack
without the app, then load the synthetic history into bronze, or run days
on a clock.

```bash
uv sync
docker compose up -d --wait lakekeeper
# 12 weeks of history, delivered and collected as fast as the machine allows
uv run ranlake backfill --profile demo --warehouse demo --weeks 12 --run-weeks 13
# then day 84 on a clock 96 times faster than real time (labelled accelerated)
uv run ranlake run --profile demo --warehouse demo --weeks 13 --first-day 84 --days 1 --speedup 96
```

`--speedup 1` is the real 15-minute cadence. A live run writes its clock
label and progress to `runs/<warehouse>/status.json`. Then build silver:
every UTC day whose files are in, per EMS, with late files merged into
their own hour:

```bash
uv run ranlake silver --warehouse demo
```

and gold: LTE and GSM KPIs per cell at 15 minutes, hour, day and week,
each with its coverage and suspect share, plus the weekly worst cells,
built by the dbt project in `transform/` one day at a time (formulas in
`results/kpi_catalog.md`):

```bash
uv run ranlake gold --warehouse demo
```

Trace any gold value back to its silver rows, bronze rows and source files
(name, hash, arrival):

```bash
uv run ranlake lineage --warehouse demo --kpi LTE_ERAB_DROP --version 1 \
  --cell ENB0001_B3_1 --granularity day --period 2026-02-10
```

Generate the planning data of the expansion area (terrain, villages,
candidate sites, coverage, backhaul and power options) into gold:

```bash
uv run ranlake planning --warehouse demo
```

Write the scenario cards to gold, their exact optima to the
evaluation-only table, and the solver report
(`results/planning_scenarios.md`); then solve any constraint set that
follows `contract/plan_constraints.schema.json`:

```bash
uv run python -m ran_lakehouse.planning.scenario_report --warehouse demo
uv run ranlake plan --warehouse demo --constraints my_constraints.json
```

Plans are solved by SCIP 10 (Apache-2.0) through OR-Tools 9.15 MathOpt
(Apache-2.0), chosen by the measurement in
`results/planning_scenarios.md`.

## API

Serve the lake over HTTP. Startup builds the run's network once, for
what-if:

```bash
uv run ranlake serve --warehouse demo --profile demo --run-weeks 13 --host 127.0.0.1 --port 8000
```

The contract is `contract/openapi.json` (generated from the code; a test
fails when they drift) with `contract/plan_constraints.schema.json`, both
at one semver version. Every response, errors included, carries it in the
`X-Contract-Version` header and the `contract_version` field, beside the
synthetic-data notice. Additions bump the minor version and only a
breaking change bumps the major; a constraint set written for any 1.x
version is accepted. Endpoints, all under `/v1` (the pipeline status is
also a plain HTML page at `/status`):

| Endpoint | What it serves |
|---|---|
| `GET /clock` | the live run's clock (if one is running) and how far bronze, silver and gold have got |
| `GET /status` | pipeline status: files per EMS, rows per layer, the planted data-quality cases as the pipeline flagged them (kind and count), the latest quality events and the clock label |
| `GET /topology` | sites, their cells and the neighbour relations of the latest CM snapshot |
| `GET /cells`, `GET /cells/{cell_name}` | cells with vendor, technology, band, N_RB and DN |
| `GET /kpi-catalog` | every KPI formula version |
| `GET /kpis` | one cell's KPI values by KPI id, formula version and range, at 15m, hour, day or week, with coverage and suspect share |
| `GET /worst-cells` | a week's worst-cell ranking for one KPI |
| `GET /cm/snapshot`, `GET /cm/changes` | one cell's CM at a time, and the change log |
| `GET /alarms` | alarm notifications |
| `GET /quality-events` | missing periods, late files and redeliveries the pipeline recorded |
| `GET /lineage` | the silver rows and source files behind one KPI value |
| `GET /planning/villages`, `/candidate-sites`, `/coverage`, `/backhaul-power-options` | the planning tables |
| `GET /planning/scenarios` | scenario cards: id, area and the request as written |
| `POST /plan` | a constraint set in, a plan out, from the same solver as the stored optima |
| `POST /what-if` | bounded changes in (rule M6), next week's LTE KPIs before and after for the changed and touched cells, replayed with common random numbers |

Out-of-bound what-if changes are refused with a 422 that names the bound.
The evaluation-only tables (planted answers) are never served; a test
walks every route to prove it.

Write the recorded sample for downstream tests:

```bash
uv run ranlake export-sample --warehouse demo --profile demo --run-weeks 13 --out data/sample
```

It holds about 30 cells around three planted faults of different kinds
over two weeks (daily and hourly KPIs, CM snapshot and changes, alarms,
worst cells) as Parquet, plus recorded responses at the current contract
version: every read route, five scenario cards, five plan solves on
constraint sets written for the sample, and five what-if calls (one out of
bound). Nothing in it says which cells are faulty or what fixes them, and
no plan-solve call uses a card's implied constraints; the export fails if
either would. `manifest.json` lists every file with its rows, size and
SHA-256.

## License

MIT, see [LICENSE](LICENSE).

## Author

Adityo Nugroho ([adityonugroho.com](https://adityonugroho.com)),
building with a Claude Code agentic workflow.
