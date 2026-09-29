# Stack check: DuckDB, dbt and SQLMesh on Iceberg

Synthetic data. Measured by `python -m ran_lakehouse.stackcheck` against the local
compose stack on 2026-09-29 UTC (rule S2). Rendered from `stack_spike.json`.

## Versions

| Component | Version |
|---|---|
| duckdb | 1.5.6 |
| pyiceberg | 0.12.0 |
| pyarrow | 25.0.1 |
| dbt-core | 1.12.5 |
| dbt-duckdb | 1.11.0 |
| sqlmesh | 0.236.2 |
| duckdb extension avro | f9d5902 |
| duckdb extension httpfs | 4bc690d |
| duckdb extension iceberg | 890b78a9c |
| lakekeeper | 0.13.6 |
| image postgres | 17.11 |
| image quay.io/lakekeeper/catalog | v0.13.6 |
| image chrislusf/seaweedfs | 4.48 |

## Data

| Item | Value |
|---|---|
| cells | 100 |
| days | 14 |
| initial_rows | 134380 |
| late_rows_inserted | 20 |
| late_rows_updated | 20 |

## Checks

| Engine | Case | Result | Seconds | Detail |
|---|---|---|---|---|
| duckdb | create and write an Iceberg table | pass | 0.071 | 134380 rows written in one INSERT |
| duckdb | MERGE INTO for a late batch (rule D1) | pass | 0.081 | 20 rows updated and 20 inserted; every late row applied; 134400 rows |
| duckdb | time travel to the pre-merge snapshot (rule D7) | pass | 0.005 | AT (VERSION) and AT (TIMESTAMP) return the pre-merge table: 134380 rows, 20 rows with their pre-correction values |
| duckdb | formula change and reprocess (rule D6) | pass | 0.060 | both formula versions stored side by side (2800 rows), each row joined to its formula text; reprocessing inserted version 2 for the full history |
| pyiceberg | read the same tables (rule S1) | pass | 0.088 | read the merged table (134400 rows, delete files applied), the first snapshot (134380 rows) and both KPI formula versions |
| dbt | incremental merge model, formula change (rule D6) | pass | 4.926 | version 1 then version 2 merged into one Iceberg table keyed by formula_version (2800 rows); both match the independent computation |
| dbt | table model rebuilt with a new formula (rule D6) | FAIL | - | see errors |
| sqlmesh | plan into Iceberg, formula change and backfill (rule D6) | FAIL | - | see errors |

## Errors

dbt, table model rebuilt with a new formula (rule D6):

```
19:19:42    Runtime Error in model kpi_accessibility_day_table (models/kpi_accessibility_day_table.sql)
Catalog Error: This table (kpi_accessibility_day_table__dbt_tmp) was modified already, can't be renamed!
```

sqlmesh, plan into Iceberg, formula change and backfill (rule D6):

```
_duckdb.CatalogException: Catalog Error: SET schema: No catalog + schema named "lk" found.
```

## Notes

- DuckDB MERGE (update plus insert) wrote 2 snapshots after the initial load but moved the snapshot log by 1: the intermediate snapshot never became current, so readers see the merge atomically.

## Recommendation

- No fallback needed. DuckDB-Iceberg MERGE and time travel both work through Lakekeeper, so bronze, silver and gold can all be Iceberg tables; the rule S2 fallback (bronze and silver in plain Parquet) does not apply.
- Transform tool: dbt (dbt-core 1.12.5, dbt-duckdb 1.11.0), with gold models materialized as incremental models using the merge strategy and a formula_version key. That pattern passed rule D6: a formula change is merged in beside the old version, and both stay queryable and labelled. Table materialization fails on Iceberg, so it is not used.
- SQLMesh 0.236.2 is not usable on this stack as released: its DuckDB adapter switches catalogs with USE, which DuckDB-Iceberg rejects for an Iceberg catalog. Its versioned physical tables would suit rule D6, but it would need adapter changes.
- Late arrivals (rule D1) load through plain DuckDB MERGE INTO from Python: one MERGE moves the snapshot log once, so readers never see a half-applied batch. Rule D7 reads use AT (VERSION => ...) or AT (TIMESTAMP => ...); DuckDB 1.5.6 needs a subquery to alias a time-travel read.
- The timings are for a small synthetic batch and one run each; they show the operations work at interactive speed, not throughput. The dbt time is dominated by dbt start-up. Throughput is measured by the scale test (rule E1).

## Evidence

- Lakekeeper v0.13.6 has no filesystem warehouse: its storage profiles are s3, adls, gcs and onelake (crates/lakekeeper/src/service/storage/mod.rs and the management OpenAPI at tag v0.13.6), and docs.lakekeeper.io/getting-started says a warehouse needs an external object store (S3, ADLS, GCS). Hence SeaweedFS in compose.yaml (rule S3).
- MinIO is not used: github.com/minio/minio is archived and its README says the repository is no longer maintained; Lakekeeper's own compose examples moved to SeaweedFS (lakekeeper/lakekeeper pull request 1811, merged 2026-06-03).
- dbt-duckdb table materialization on Iceberg: CTAS into __dbt_tmp then rename in one transaction is refused by DuckDB-Iceberg; the fix is open upstream (duckdb/dbt-duckdb pull request 747).
- SQLMesh catalogs mapping accepts only type and path for an attached catalog (sqlmesh/core/config/connection.py at v0.236.2), so the Iceberg endpoint travels in an ICEBERG secret; DuckDB 1.5.6's iceberg extension (890b78a9c) cannot create views in an Iceberg catalog, so the virtual layer is mapped to a local DuckDB catalog.
