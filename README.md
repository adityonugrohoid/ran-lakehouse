# ran-lakehouse

A medallion lakehouse for telecom performance data: a synthetic 4G (LTE)
and 2G (GSM) multi-vendor operator generates 3GPP files, a
bronze-silver-gold pipeline on Iceberg and DuckDB builds them into a
versioned KPI catalog, and an HTTP API under a semver contract serves
the result. The operator is modelled on public facts about Indonesian
networks, and its vendor systems drop files every 15 minutes; the API
serves KPIs, configuration, alarms, topology, network planning and a
what-if simulator. Every data-quality problem that breaks real operator
pipelines is planted on purpose and guarded by a test. The network, its
traffic and its faults are synthetic, generated from seeds: not a real
place, operator or network.

## Quickstart

One command runs the demo from files to API: the stack starts, the app
generates two weeks of synthetic history, delivers and collects the files,
builds bronze, silver, gold and the planning tables, then serves the API on
`http://127.0.0.1:8000` (`/status` for the pipeline page). The first start
takes a while on a laptop, since it builds the whole lake before it serves;
a restart only builds what is missing.

```bash
git clone https://github.com/adityonugrohoid/ran-lakehouse.git
cd ran-lakehouse
docker compose up -d
docker compose logs -f app   # "serving compose-demo on port 8000" when ready
curl "http://127.0.0.1:8000/v1/kpis?cell=ENB0001_B3_1&kpi_id=LTE_ERAB_DROP&formula_version=1&granularity=day&start=2026-01-05&end=2026-01-12"
```

The keys and passwords in `compose.yaml` and `compose/seaweedfs-iam.json`
are local placeholders: catalog auth is off and every port binds to
127.0.0.1, as the header of `compose.yaml` says. Do not reuse them outside
local runs.

## What the reports show

Every number here is from a committed report in `results/`, written by
code in this repository from one record (`.md` and `.json`), on synthetic
data.

- Gold agrees with the network model on every complete WIB day of the
  12-week demo run (83 of 84 days compared): `results/gold_build.md`.
- Every planted data-quality case is found where it was planted: 71 late
  files, 48 same-content and 24 changed redeliveries, 24 missing files,
  24 suspect cases and 92 network elements moved to a renamed counter
  release; a KPI formula revision is reprocessed with both versions kept;
  time travel and lineage are tested: `results/planted_cases.md`.
- A one-step tilt or power change moves a neighbour's DL throughput by a
  median of 1.34%, p99 12.61%: `results/whatif_sensitivity.md`.
- Against live counters from a public data set, the daily load shape
  agrees with Pearson r 0.9433 (LTE) and 0.7717 (GSM); the model's volume per
  user does not rise with PRB use as the live network's does, a stated
  gap: `results/real_data_crosscheck.md`.
- The plan solver proves all 20 planning scenarios in 3.34 s:
  `results/planning_scenarios.md`.
- A 9,813-cell network runs two simulated days through every stage in
  4,808.3 s on the build machine, about half of it building the network
  twice (once per stage process that needs it); silver is the bottleneck,
  extrapolated
  to 9.22 hours per day at 250,000 cells on one machine:
  `results/scale_test.md`.

## Pipeline by hand

Start the lake stack without the app, then load the synthetic history into
bronze, or run days on a clock:

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
`results/kpi_catalog.md`). Rebuild one KPI formula version over a range of
UTC days from the gold cell counters with `reprocess`:

```bash
uv run ranlake gold --warehouse demo
uv run ranlake reprocess --warehouse demo --kpi LTE_RRC_SSR --version 2 --first 2026-02-10 --last 2026-02-16
```

Trace any gold value back to its silver rows, bronze rows and source files
(name, hash, arrival):

```bash
uv run ranlake lineage --warehouse demo --kpi LTE_ERAB_DROP --version 1 \
  --cell ENB0001_B3_1 --granularity day --period 2026-02-10
```

Generate the planning data of the expansion area (terrain, villages,
candidate sites, coverage, backhaul and power options) into gold, write the
scenario cards to gold and their exact optima to the evaluation-only table
with the solver report (`results/planning_scenarios.md`), then solve any
constraint set that follows `contract/plan_constraints.schema.json`:

```bash
uv run ranlake planning --warehouse demo
uv run python -m ran_lakehouse.planning.scenario_report --warehouse demo
uv run ranlake plan --warehouse demo --constraints my_constraints.json
```

Measure the pipeline at scale (one stage per process, peak memory and spill
per stage), and fetch the public data set for the cross-check (it stays
under `data/`, never committed):

```bash
uv run ranlake scale-test --profile scale --warehouse scale --days 2 --record runs/scale/full.json
uv run python -m ran_lakehouse.crosscheck.fetch
uv run python -m ran_lakehouse.crosscheck.report
```

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

## Reports

`docs/spec.md` is the rulebook; the reports cite its rules by id.

| Report | What it holds |
|---|---|
| `results/world.md` | the synthetic map, population and network of the demo and tiny profiles |
| `results/model.md` | the network model: coverage, load, counters and their relationship checks |
| `results/faults.md` | the planted network faults, their signatures and the right fixes |
| `results/whatif_sensitivity.md` | what one-step changes do to the changed cell and its neighbours |
| `results/pm_files.md`, `results/config_alarms.md` | the vendor-style PM, CM and FM files |
| `results/bronze_backfill.md`, `results/silver_build.md`, `results/gold_build.md` | the 12-week demo lake, layer by layer |
| `results/kpi_catalog.md` | every KPI formula, its standard and its version (rule E4) |
| `results/planted_cases.md` | every planted data-quality case and its guarding tests (rule E3) |
| `results/time_travel_lineage.md` | reading gold as of a past moment, and tracing a value to its files |
| `results/planning.md`, `results/planning_scenarios.md` | the expansion area, the scenario cards and the solver comparison |
| `results/real_data_crosscheck.md` | the synthetic network against public live counters (rule E2) |
| `results/scale_test.md` | a 10,000-cell day through every stage, and the extrapolation (rule E1) |
| `results/stack_spike.md` | the stack choice: dbt on DuckDB and Iceberg |

## Credits

- Plans are solved by SCIP 10 (Apache-2.0) through OR-Tools 9.15 MathOpt
  (Apache-2.0), chosen by the measurement in `results/planning_scenarios.md`.
- The real-data cross-check uses "Performance Management Counters from Live
  5G, 4G and 2G Radio Access Network" by Peter Lehoczký, Matúš Turcsány,
  Laura Krajčovičová, Filip Zatroch, Marcel Kajan and Marek Galinski,
  Zenodo, https://doi.org/10.5281/zenodo.17815388 (2026), CC BY 4.0. Only
  aggregated statistics and figures derived from it are committed.
- This project grew out of [NetPulse AI](https://github.com/adityonugrohoid/hackathon-telecom-ops),
  a multi-agent telecom operations assistant. That project consumed a
  bundled database; this one is the data platform it should have had.

## License

MIT, see [LICENSE](LICENSE).

## Author

Adityo Nugroho ([adityonugroho.com](https://adityonugroho.com)),
building with a Claude Code agentic workflow.
