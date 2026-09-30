# ran-lakehouse: spec

This document is the rulebook for ran-lakehouse. Each rule has an id, and
code and reports cite rules by that id (for example "rule D5"). Numbers
marked START are starting values; each is replaced by a measured or
reviewed value, recorded in a report. Every constant in code is cited
(spec, edition, clause) or labelled ASSUMPTION.

## 1. What the lakehouse is

A mini telecom lakehouse and data generator for a synthetic Indonesian
multi-vendor operator running 4G (LTE) and 2G (GSM). Simulated vendor
systems drop performance files every 15 minutes; a pipeline takes them
through bronze, silver and gold; an HTTP API serves KPIs, configuration,
alarms, topology, planning tables and a what-if simulator. It is a data
platform that downstream projects consume through its versioned API, and
it stands on its own as a data engineering showcase.

It proves, with planted cases and tests, the problems that break real
operator data: late, duplicate and missing files; suspect data; counters
renamed after a software upgrade; KPI formula changes that force
reprocessing; audits of what a KPI said at a past moment; lineage from any
KPI to its source file.

Out of scope: 5G, 3G, 2G sunset planning, real EMS or OSS connections,
security hardening, high availability, operator scale (covered by the
measured scale test, rule E1).

## 2. World (rule W)

- W1 Deterministic. Every generator draws from
  `np.random.default_rng([BASE_SEED, purpose_id, entity_id])`. No clock, no
  global random state. Same seed, same world, byte for byte.
- W2 Coordinates are a local grid in km, origin at the south-west corner.
  The network is modelled on public facts about Indonesian networks
  (bands, technology mix, the WIB time zone; rules W5, W6). No real
  operator, network, site, city or person is named, and no real
  coordinates are used.
- W3 One world, two areas on the same map: the served region (existing
  network, optimization) and an uncovered 3T expansion area next to it
  (planning, rule G).
- W4 Population layer: a raster over the whole map (START 250 m cells),
  clustered settlements plus sparse rural density. It drives cell traffic
  (rule M3) and village population (rule G2). ASSUMPTION distributions,
  judged by eye and by the real-data cross-check (rule E2).
- W5 Time zones: both areas are in WIB (UTC+07:00), since areas next to
  each other on one map share a zone (ASSUMPTION). Offset handling is
  exercised by the vendor dialects instead: the Nokia-style EMS writes its
  files with UTC timestamps (+0000) and the Huawei-style EMS writes local
  time with +0700; file names and XML carry the offset (rule P2) and silver
  normalizes both.
- W6 Network, per public facts:
  - technologies, as shares of cells: LTE about 80-85%, GSM about 15-20%
    (START, ASSUMPTION). Every served site carries LTE except a handful of
    GSM-only rural sites; about a third of sites also carry GSM, 3 cells
    each. An LTE cell is one sector on one band;
  - LTE bands: FDD B1 2100, B3 1800, B8 900, B28 700; TDD B40 2300 on part
    of the network; GSM on 900 and 1800;
  - sectors: 3 per site (ASSUMPTION), azimuths near 0/120/240 with jitter;
  - vendors: two vendor dialects by region (rule P5); the split is a
    design choice, since no public regional split exists.
- W7 Profiles, one generator:
  - tiny: about 5 sites (about 27 cells), 2 days (tests, CI, seconds);
  - demo: about 300 sites, about 1,500 LTE and 300 GSM cells, 12 weeks at
    15-minute granularity; planning area about 150 villages and 60
    candidate sites (evaluation, replay site);
  - scale: about 10,000 cells for 1 day (rule E1).
  All counts START.

## 3. Network model and what-if (rule M)

- M1 Cell state: site coordinates, azimuth, antenna height, electrical
  tilt, transmit power, band and bandwidth, neighbour list, handover
  offset (cell individual offset), technology, vendor.
- M2 Coverage on the grid: path loss from Okumura-Hata / COST-231 Hata
  (cite the source and its validity range; B28 700 and B1 2100 need a
  stated treatment at the edge of the range, ASSUMPTION), plus a standard
  sector antenna pattern (horizontal and vertical, cite the 3GPP TR used).
  No terrain in the served region (stated). Best server and signal
  quality per grid cell from received power and interference.
- M3 Load: users from the population layer (rule W4) assigned to their
  best server, times a diurnal and weekly profile (ASSUMPTION shape,
  checked against the real data, rule E2), plus noise.
- M4 LTE counters derived from load and signal quality: PRB use, IP
  throughput falling with load and poor quality, RRC and E-RAB success
  falling with congestion and interference, drops rising at weak edges and
  with missing neighbours, timing advance distribution from footprint
  distance, CQI distribution from signal quality, unavailable time from
  outages. Transfer functions are documented and labelled ASSUMPTION.
- M5 GSM: TCH blocking from Erlang B (traffic against available TCHs),
  SDCCH blocking likewise, drops from weak edges and interference,
  handover counters from neighbour relations.
- M6 What-if: apply a bounded change (tilt within +/-2 degrees, power
  within +/-3 dB per change, add or remove a neighbour, handover offset
  (cell individual offset) within +/-6 dB and moved at most 3 dB per
  change, ASSUMPTION) and replay the next week with the same random
  noise (common random numbers), for the changed cell and every cell whose
  load or interference it touches. Output: next week's counters and KPIs
  for those cells, before and after.
- M7 Stated simplifications: no terrain in the served region, no
  scheduler, fading or mobility traces; grid-level coverage, load and
  interference.

## 4. Planted faults (rule F)

- F1 Every fault is a change inside the model, never a painted-on counter
  pattern:

| Id | Fault | Inside the model | Right answer |
|---|---|---|---|
| F1a | Tilt changed by mistake (overshooting) | tilt set too high on a given day | restore tilt |
| F1b | Missing neighbour | relation deleted | add neighbour |
| F1c | Power reduced | power lowered after maintenance | restore power |
| F1d | Capacity | traffic surge in the population layer | load-balancing offset or capacity note |
| F1e | External interference | uplink interference source added | no parameter fix, field visit |
| F1f | Outage | cell down for hours | no parameter fix, alarm-driven |

  Faults stay local: a fault changes the KPIs (more than START 1 point)
  of only its cell and nearby cells, which carry at most START 2% of a
  technology's access attempts in a day; a report lists every scheduled
  fault's reach and a test enforces the limit. F1e: the interference
  source sits START 0.1 to 0.3 km along the cell's azimuth and is as
  strong as it takes to raise the faulty cell's uplink noise by START
  15 dB; the other cells hear it through their own antenna gain and path
  loss.
- F2 Each fault leaves its trace in CM (config change log for F1a-F1c),
  FM (alarms for F1f, and for F1e where a vendor raises one) and PM.
- F3 Faults are scheduled per seed across the 12 weeks, with overlap in
  some weeks and quiet weeks in between. START about 3 to 6 per week in the
  demo profile.
- F4 Answers (fault id, cell, start, cause, right answer, the recovery the
  right fix achieves in the what-if) go to an evaluation-only table the
  serving API never returns (rule A3).

## 5. Performance files and collection (rule P)

- P1 3GPP PM XML per TS 32.435 (Rel-19 V19.0.0) and the 5G-era copy in
  TS 28.532 clause 12.3.2: measCollecFile, fileHeader (fileFormatVersion,
  vendorName, dnPrefix, fileSender, measCollec beginTime), measData
  (managedElement; measInfo with job, granPeriod, repPeriod,
  measTypes, measValue measObjLdn, measResults), fileFooter. Validate
  against the XSD in tests. The XSD is not vendored (3GPP asks permission
  to redistribute code from its specifications): CI checks the structure
  of TS 32.435 clause 4.2.2, and an optional test validates against the
  schema fetched from the 3GPP archive. Distribution measurements (the
  CQI and timing-advance histograms) report at 60-minute granularity:
  their measInfo carries granPeriod and repPeriod PT3600S, ending with
  the hour, in the file of the hour's last 15-minute period; every other
  measInfo stays at PT900S (TS 32.435 gives each measInfo its own
  granPeriod). ASSUMPTION: the histograms are most of the values, and
  collecting them hourly keeps a 12-week backfill within a laptop budget.
- P2 File names per TS 32.432 clause 5.1.2 with UTC offsets, type B: one
  file per EMS per granularity period carrying every network element, for
  example `B20260105.1500+0700-1515+0700_EMS-HW-01.xml.gz`, gzip. Reason:
  12 weeks at one file per network element per 15 minutes would be about
  2.3 million files, too many for a laptop-scale lake. The parser still
  accepts type A (one network element per file).
- P3 Suspect flag: `<suspect>true</suspect>` on measValue where a
  collection was interrupted (TS 32.401) or a planted case needs it;
  default omitted.
- P4 A second format: Nokia-style OMeS XML (PMSetup / PMMOResult / MO /
  numeric counter ids such as M8013C31), for the regions set to that
  dialect. Written from public descriptions; stated as "modelled on",
  not a vendor copy. One file per EMS per granularity period, UTC
  timestamps (rule W5), named `OMeS_<EMS>_<UTC start>Z.xml.gz`
  (ASSUMPTION: no public naming convention found). As in P1, the
  distribution measurements (LTE_Quality_DL, LTE_Timing_Advance) report
  at 60-minute intervals, in a second PMSetup with interval 60 in the
  file of the hour's last period (ASSUMPTION, same reason).
- P5 Two vendor dialects, covering both kinds of multi-vendor difference;
  each has a dictionary mapping to 3GPP names (LTE TS 32.425, GSM
  TS 52.402):
  - Huawei-style: 3GPP PM XML (P1) with vendor counter names `L.*`
    (L.RRC.ConnReq.Att, L.E-RAB.AttEst, L.Thrp.bits.DL,
    L.ChMeas.PRB.DL.Used.Avg, L.Traffic.User.Avg) = same format,
    different names; the counter rename case (rule D5) runs on this
    dialect;
  - Nokia-style: own format (P4) with numeric ids = different format and
    different names.
  No vendor uses 3GPP counter names natively; mapping is the operator's
  job and is many-to-one, per software release (rule D5). The README says
  more dialects are one dictionary each. Region split between the two:
  a design choice, stated.
- P6 GSM measurement names follow TS 52.402 Annex B (camelCase, spec
  spellings kept, for example attImmediateAssingProcs).
- P7 Collection: simulated EMS per vendor region writes to a landing area;
  a collector picks up files. Landing keeps the last few days (START 3),
  as a real EMS does; bronze keeps everything.
- P8 Clock: real 15-minute cadence, or an accelerated clock that is
  always labelled as accelerated in the UI and API responses.

## 6. Configuration and alarms (rule C)

- C1 CM: daily snapshots and a change log of cell parameters (rule M1),
  with object classes per the NRMs: LTE TS 28.658 (ENBFunction,
  EUtranCellFDD, EUtranCellTDD, EUtranRelation), GSM TS 28.655
  (BSSFunction, BTSSiteMgr, GSMCell, GSMRelation). DNs per TS 32.300.
  DNs and files use the XML solution-set spellings: TS 28.659 for E-UTRAN
  (the same names) and TS 28.656 for GERAN (BssFunction, BtsSiteMgr,
  GsmCell, GsmRelation).
- C2 FM: an alarm log with X.733 fields (perceivedSeverity, eventType,
  probableCause, specificProblem), raise and clear times, per TS 32.111-2
  style for LTE and GSM.
- C3 Exports: each EMS writes its CM snapshot, CM change log and FM log
  per day as gzip JSON lines in its own DN style and time zone (rule W5),
  with the cell name as userLabel for joining (ASSUMPTION: a project
  format; 3GPP Bulk CM XML is not modelled).

## 7. Data quality proof cases (rule D)

Each is planted by the generator and has a test that fails loudly when
the property breaks:
- D1 Late file: arrives after its successors; merged, KPIs recomputed,
  flagged.
- D2 Duplicate file: same content twice, and same name with different
  content; idempotent load, conflict flagged.
- D3 Missing file: a gap per network element and period; flagged, never
  silently zero.
- D4 Suspect data: carried from XML to silver and gold as a quality flag;
  KPIs state their coverage.
- D5 Counter rename after a software upgrade: a vendor dialect changes a
  counter name on a date for part of the network; silver maps both by
  release; the KPI series stays continuous.
- D6 KPI formula change: a gold formula is revised; history is reprocessed
  from bronze; both versions are queryable and labelled.
- D7 Time travel: "what did this KPI say at time T" answered from table
  history (Iceberg snapshots), tested.
- D8 Lineage: one query from any gold KPI value to its source files,
  network element and counters.

## 8. Lake layers (rule L)

- L1 Bronze: raw parsed values, immutable, with source file name, hash,
  arrival time and parser version.
- L2 Silver: typed, mapped to 3GPP names by vendor and release, deduped,
  late arrivals merged, gaps and suspect flagged, quality checks run per
  load.
- L3 Gold KPIs, each with id, formula, standard source and where operators
  differ, in a KPI catalog published as a report:
  - LTE per TS 32.450 V19.0.0: E-RAB accessibility (RRC x S1SIG x ERAB
    success), E-RAB retainability (releases per session time), IP
    throughput (IPVol / IPTime), cell availability (from
    CellUnavailableTime), mobility (map 32.450's HO.Exe*/HO.Prep* names to
    the 32.425 HO.* counters, stated);
  - operator-defined LTE KPIs, labelled as such: RRC setup success rate,
    E-RAB drop rate, PRB utilization;
  - GSM per TS 32.410 V19.0.0 where defined (service access success,
    abnormal release rate, HOSR); CSSR, TCH and SDCCH blocking, SDCCH drop
    per documented vendor-style definitions, labelled as such;
  - ratio of sums, never average of ratios; granularities 15 min, hour,
    day, week.
- L4 Gold CM, FM and topology tables; planning tables (rule G).
- L5 Weekly worst-cell ranking with a persistence rule (breach on N of M
  days, START N=3, M=7), exposed as a gold table.

## 9. Planning geography (rule G)

- G1 Expansion area on the same map (rule W3), uncovered today, WIB (rule W5).
- G2 Terrain: synthetic height map for the expansion area only (spectral
  method, written fresh; START relief and roughness judged by eye and
  rendered).
- G3 Villages: id, x_km, y_km, population, schools, covered_today.
- G4 Candidate sites: id, x_km, y_km, elevation, build cost, grid
  distance, fiber distance. Costs in IDR, ASSUMPTION, stated.
- G5 Coverage: Hata for 900 and 1800 MHz plus Bullington diffraction
  (ITU-R P.526, cite edition and clause) on the terrain profile. Coverage
  thresholds for LTE (RSRP) and GSM (RxLev) each carry a cited source or
  are labelled ASSUMPTION.
- G6 Backhaul and power options per candidate site: fiber (distance,
  cost), microwave (to a hub, line of sight with 60% first Fresnel zone
  clearance, cost), satellite (always available, about 8 Mbps cap,
  monthly cost), power (grid if within a distance, else solar, cost).
- G7 Gold tables: villages, candidate_sites, coverage (site x village,
  signal_dBm, covered), backhaul_power_options.
- G8 Planning scenarios: scenario cards (id, area, written request, the
  constraint set it implies) and each scenario's exact optimum from an
  integer program, stored in the evaluation-only table (rule A3). START
  about 20 scenarios in the demo profile.
- G9 Plan solver: one integer-programming implementation in the lakehouse
  (OR-Tools or HiGHS, chosen by measurement on the demo scenarios) serves
  both the stored optimum (G8) and the plan solve endpoint. The
  constraint schema is part of the contract (rule A1). A plan's gap to
  the optimum therefore measures how well the request was turned into
  constraints.

## 10. API and contract (rule A)

- A1 HTTP API with a contract version (semver) on every response and in
  the gold schema; breaking changes bump the major version.
- A2 Endpoints (names fixed by the API contract): topology and cells; KPIs
  by cell, KPI and time range; worst-cell ranking; CM snapshot and change
  log; alarms; quality events; lineage for a KPI value; planning tables;
  plan solve (rule G9); what-if (rule M6); clock status.
- A3 The evaluation-only answers (rules F4, G8) are never served by the
  API. A separate scoring entry point reads them.
- A4 Recorded sample export: a small fixed dataset and recorded API
  responses at a contract version, for downstream projects' tests and CI.
- A5 A thin TMF628-shaped view of gold KPIs is optional, only if it stays
  thin.

## 11. Stack (rule S)

- S1 DuckDB (1.5.x) as engine; Parquet; Apache Iceberg tables through a
  Lakekeeper REST catalog backed by Postgres; PyIceberg reads the same
  tables to prove portability. No Spark or Trino.
- S2 Transform tool: dbt (dbt-core 1.x pinned, dbt-duckdb) by default.
  The choice is decided by a measured one-day spike testing dbt and
  SQLMesh on Iceberg writes through DuckDB, on MERGE for late arrivals,
  on time travel, and on the D6 reprocessing case, written as a report.
  If DuckDB-Iceberg MERGE or time travel fails, bronze and silver merge in
  plain Parquet and Iceberg holds gold only; D7 then uses the Iceberg gold
  history. Decided by the stack check report (results/stack_spike.md): dbt
  with incremental merge models keyed by formula_version, never table
  materialization; late and duplicate files load through DuckDB MERGE;
  SQLMesh rejected as released; no fallback, bronze, silver and gold are
  all Iceberg.
- S3 docker compose with four long-running services: the lakehouse app
  (generator, collector, pipeline, API), Lakekeeper, Postgres, SeaweedFS
  (S3-compatible object storage for the Iceberg warehouse; Lakekeeper
  supports no filesystem warehouse), plus Lakekeeper's one-shot migrate
  container.
- S4 A `ranlake` CLI: generate, run (cadence or accelerated), backfill,
  reprocess, scale-test, export-sample, serve.
- S5 Conventions: uv, ruff, strict mypy, pinned CI actions and a scoped
  workflow token, tests over the core, reports written as .md plus .json
  from one record and checked byte for byte, README with an H1, an
  intro, Quickstart first, License, and Author last, MIT, ASCII prose,
  synthetic data only and labelled so.

## 12. Evidence (rule E)

- E1 Scale test: about 10,000 cells for 1 day, measured on the build
  machine (files per second, rows per second, storage per cell-day, end
  to end latency per period), extrapolated to 250,000 cells with the
  method stated. A report.
- E2 Real-data cross-check against "Performance Management Counters from
  Live 5G, 4G and 2G Radio Access Network" (Zenodo
  10.5281/zenodo.17815388, CC BY 4.0): a download script, never committed
  data; compare shapes (diurnal load, throughput against PRB use, success
  rate spread) for LTE and GSM; commit only the comparison figures and
  statistics, with attribution. A report.
- E3 Planted-case report: each D rule, what was planted, what the pipeline
  did, the test that guards it.
- E4 KPI catalog report (rule L3).

## 13. Honesty

- Synthetic data, stated on every surface. The model is simplified and
  says where (rule M7). "Modelled on" for vendor dialects. No real
  operator, network, site, city, client or person named (rule W2). Numbers in the README come from reports.

## 14. Done means

All D rules planted and tested; the demo profile runs end to end from
files to API with `docker compose up`; the API serves every A2 endpoint
at a contract version; the sample export exists; reports E1 to E4
committed; CI green; README headline numbers match the reports.
