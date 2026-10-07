# Scale test

A synthetic network of about 10,000 cells through the whole pipeline, measured stage by stage on the build machine (rule E1). Written by `python -m ran_lakehouse.scale report` from `scale_test.json`; the runs come from `ranlake scale-test`. One run each, so every figure is one sample: no spread, and no memory target is claimed.

| Run | Profile | Cells | LTE / GSM | Grid points | Days simulated | Total wall (s) |
|---|---|---|---|---|---|---|
| slice | demo | 1,716 | 1,452 / 264 | 102,400 | 2 | 282.5 |
| full | scale | 9,813 | 8,166 / 1,647 | 640,000 | 2 | 4,808.3 |

## Why two simulated days

The network's days are WIB days (UTC+7), and silver and gold build UTC days, each only after its cutoff, the UTC day's end plus 30 minutes. One simulated WIB day covers the last 7 hours of one UTC day and the first 17 of the next, so after one day no UTC day is complete: silver and gold would process only a partial one. The full run simulates two days, so that one complete UTC day goes from files to gold (1 complete UTC day in gold); every rate below is stated per complete UTC day of 96 periods, scaled by the periods each stage actually processed.

## Per stage, full run

Each stage runs in a process of its own; peak resident set from os.wait4, peak DuckDB spill folder size sampled every second. The collect stage builds the network again before it simulates, renders, delivers and collects, so its wall time holds a second network build; its own work is the last row. The network build is a once-per-network cost and is counted once below.

| Stage process | Wall (s) | Peak RSS (MB) | Peak spill (MB) |
|---|---|---|---|
| network build | 1,228.9 | 3,538 | |
| collect | 1,848.8 | 3,660 | not recorded in this run |
| silver | 1,683.7 | 2,225 | not recorded in this run |
| gold | 43.9 | 1,711 | not recorded in this run |
| of which collect's own work (simulate 3.2 s, render 195.4 s, deliver and collect 373.2 s) | 571.8 | | |

Silver is the bottleneck at scale: 1,684 s for 43,025,914 rows, against 44 s for gold and 373 s for collecting the same data. A reading of the code, not verified: silver stages each partition (one UTC day of one EMS) as DuckDB temporary tables, the bronze rows with every column and then the mapped rows with every column, and joins the bronze rows to themselves to find changed redeliveries; at the 1 GB memory limit those tables live in the spill folder. The slice's silver spilled 2,755 MB for 7,497,868 rows. Making silver stream or narrow its staging is an open item, not built here.

## Per stage, slice

| Stage process | Wall (s) | Peak RSS (MB) | Peak spill (MB) |
|---|---|---|---|
| network build | 27.0 | 698 | |
| collect | 94.2 | 1,973 | 0 |
| silver | 137.2 | 1,670 | 2,755 |
| gold | 23.0 | 926 | 0 |
| of which collect's own work (simulate 0.5 s, render 20.3 s, deliver and collect 44.8 s) | 65.6 | | |

## Per complete UTC day

| Figure | Slice (demo) | Full (scale) |
|---|---|---|
| simulate, seconds | 0.2 | 1.6 |
| render, seconds | 10.2 | 97.7 |
| collect, seconds | 22.4 | 186.6 |
| silver, seconds | 106.2 | 1,303.5 |
| gold, seconds | 17.8 | 34.0 |
| files delivered | 198 | 198 |
| files per second (render and collect) | 6.07 | 0.69 |
| bronze rows | 5,651,200 | 32,446,925 |
| silver rows | 5,804,801 | 33,310,385 |
| gold rows | 1,753,579 | 10,091,071 |
| bronze rows per second | 252,286 | 173,885 |
| silver rows per second | 54,649 | 25,554 |
| gold rows per second | 98,480 | 296,909 |
| bronze storage per cell-day, bytes | 15,281 | 17,338 |
| silver storage per cell-day, bytes | 11,697 | 17,178 |
| gold storage per cell-day, bytes | 9,926 | 10,533 |

Files: per UTC day, 2 EMS times 96 periods of PM files (one type B file per EMS and period) plus one CM snapshot, one CM change log and one FM export per EMS, so 198 a day is 192 PM files and 6 others; in the full run's two days, 4 CM, 4 CMLOG, 4 FM, 383 PM. Silver holds more rows than bronze because it adds the 3GPP measurements derived from vendor-style counters (PRB use and cell unavailable time, flagged derived): 1,012,584 rows in the full run.

## Latency per period, file arrival to gold

Computed, not observed end to end: from the simulated arrival time of each file, the cutoff rule and the measured silver and gold times; the run itself was a batch, so no wall clock saw a file arrive and its KPIs appear. Silver builds a UTC day after its cutoff and gold builds it next, so a period's KPIs are available in gold at the cutoff plus the silver and gold time of the day (1,337.5 s in the full run). For each of the 192 PM files of the complete UTC days (PM only; CM and FM files do not feed gold KPIs), from its (simulated) arrival to gold:

| Run | min | median | p90 | max |
|---|---|---|---|---|
| slice | 25.6 min | 745.5 min | 1,314.5 min | 1,455.0 min |
| full | 45.8 min | 765.7 min | 1,334.7 min | 1,475.2 min |

The wait for the cutoff dominates (a file of the day's first period waits almost a day); the processing itself is the per-period cost below. A live run that needs fresher gold would build silver per hour instead of per day; not measured here.

| Stage | Slice, s per period | Full, s per period |
|---|---|---|
| simulate | 0.003 | 0.017 |
| render | 0.106 | 1.018 |
| collect | 0.233 | 1.944 |
| silver | 1.106 | 13.578 |
| gold | 0.185 | 0.354 |

## Extrapolation to 250,000 cells

Method. Each term is placed by what it grows with:

- with the cells (rows, files, bytes, and the time of simulate, render, collect, silver and gold that handles them, silver at its time per row in this run): the full run's figure per complete UTC day times 25.48, the target over the full run's cells, on one machine with one process per stage, as measured; the network build is not part of these per-day figures;
- with the grid points and the cells per band together (the network build: coverage computes every grid point against every cell of its band): a power law in cells through the slice and the full run, two points. The grid grows with the served area (exponent 1.05 in cells here) and the cells with area and density.

The network build is a one-off per network in principle (it is deterministic from the profile), but the current code builds it again in every process that needs it (each backfill, run, serve and scale-test stage): per run as built.

Assumptions: the scale profile's density and band mix, the same machine, no parallelism, and silver and gold staying linear in rows.

| Term | Grows with | At the target |
|---|---|---|
| simulate, hours per UTC day | cells | 0.01 |
| render, hours per UTC day | cells | 0.69 |
| collect, hours per UTC day | cells | 1.32 |
| silver, hours per UTC day | cells | 9.22 |
| gold, hours per UTC day | cells | 0.24 |
| files delivered per UTC day | EMS and periods, not cells (one type B file per EMS and period; each file grows with its cells) | 198 |
| bronze rows per UTC day | cells | 826,631,127 |
| silver rows per UTC day | cells | 848,628,987 |
| gold rows per UTC day | cells | 257,084,250 |
| bronze storage, GB per UTC day | cells | 4.33 |
| silver storage, GB per UTC day | cells | 4.29 |
| gold storage, GB per UTC day | cells | 2.63 |
| grid points | served area | 19,230,162 |
| network build, hours (exponent 2.19) | grid points and cells per band | 409.4 (rough: two points) |
| network build, peak GB (exponent 0.93) | grid points and cells per band | 70.4 (rough: two points) |

What would have to change at that size (not measured):

- the network build: keep only the cells within reach of each grid point (a spatial index), build per region in parallel, and build it once per network instead of once per process;
- collect: one collector process per EMS or per region in parallel; a type B file per EMS and period grows with its cells, so the file count stays low and file size grows;
- silver and gold: partitions are already one UTC day per EMS; at that size they run in parallel, or on a distributed engine (Spark or Trino over the same Iceberg tables) instead of one DuckDB process;
- storage grows linearly with the cells, which object storage and the catalog take as is.
