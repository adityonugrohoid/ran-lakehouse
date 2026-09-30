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

The repo is being built. What runs today: start the local lake stack,
then load the synthetic history into bronze, or run days on a clock.

```bash
uv sync
docker compose up -d --wait
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

The API lands in the next pull requests.

## License

MIT, see [LICENSE](LICENSE).

## Author

Adityo Nugroho ([adityonugroho.com](https://adityonugroho.com)),
building with a Claude Code agentic workflow.
