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
label and progress to `runs/<warehouse>/status.json`. The silver and gold
layers and the API land in the next pull requests.

## License

MIT, see [LICENSE](LICENSE).

## Author

Adityo Nugroho ([adityonugroho.com](https://adityonugroho.com)),
building with a Claude Code agentic workflow.
