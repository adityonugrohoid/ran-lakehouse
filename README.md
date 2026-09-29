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

The repo is being built. The generator, the pipeline and the API land in
the next pull requests, and this section will then carry the commands
that run them.

## License

MIT, see [LICENSE](LICENSE).

## Author

Adityo Nugroho ([adityonugroho.com](https://adityonugroho.com)),
building with a Claude Code agentic workflow.
