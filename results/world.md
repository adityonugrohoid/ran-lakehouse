# World report

Synthetic world (rule W): the map, population, sites and cells are generated from seeds
(rule W1); no real operator, network, site, city or person, and no real coordinates
(rule W2). Written by `python -m ran_lakehouse.world.report` from `world.json`.

## Demo profile

Map 60.0 x 40.0 km; served region x < 40.0 km, expansion area east of it (rule W3).

![Sites by vendor and technology](world_map.png)

![Population density and sites](world_population.png)

![Nearest-site distance by area class](world_isd.png)

### Population

| Item | Value |
|---|---|
| total | 1390320 |
| served region | 1201779 |
| expansion area | 188542 |
| expansion village | 150 |
| served city | 3 |
| served town | 16 |
| served village | 60 |

| Served area class | km2 |
|---|---|
| urban | 61.94 |
| suburban | 393.75 |
| rural | 1144.31 |

### Sites

| Item | Sites |
|---|---|
| total | 290 |
| area class urban | 143 |
| area class suburban | 96 |
| area class rural | 51 |
| vendor huawei | 181 |
| vendor nokia | 109 |
| Huawei-style share | 0.624 |
| technology GSM | 3 |
| technology LTE | 197 |
| technology LTE+GSM | 90 |

### Cells

| Item | Cells |
|---|---|
| total | 1824 |
| technology GSM | 279 |
| technology LTE | 1545 |
| LTE share of cells | 0.847 |
| band B1 | 339 |
| band B28 | 63 |
| band B3 | 861 |
| band B40 | 192 |
| band B8 | 90 |
| band G1800 | 90 |
| band G900 | 189 |
| class EUtranCellFDD | 1353 |
| class EUtranCellTDD | 192 |
| class GsmCell | 279 |
| vendor huawei | 1143 |
| vendor nokia | 681 |

### Nearest-site distance, km

| Area class | Sites | p10 | Median | p90 |
|---|---|---|---|---|
| urban | 143 | 0.54 | 0.61 | 0.67 |
| suburban | 96 | 1.58 | 1.81 | 2.1 |
| rural | 51 | 3.08 | 3.39 | 3.84 |

## Tiny profile

Map 11.0 x 4.0 km; 5 sites, 27 cells (B1 9, B3 15, G900 3).

## Notes

- Inter-site distance: sites sit on a hexagonal lattice spaced at the START ISD of their area class, jittered by up to 15% of it, so the distance to the nearest site runs below the lattice spacing. Median nearest-site distance against lattice ISD: urban 0.61 of 0.7 km (0.87), suburban 1.81 of 2.0 km (0.91), rural 3.39 of 4.0 km (0.85).
- Area classes are judged on density smoothed over 1.5 km (ASSUMPTION), so a village reads as rural and a town as a whole; a site closer than 0.7 of its class ISD to a denser class's site is dropped (ASSUMPTION).
- Vendor regions: sites west of x = 12.5 km are Huawei-style (0.624 of sites), the rest Nokia-style; 8 of 143 urban sites fall east of the split, on the eastern edges of the cities. The split is a design choice (rule P5).
- Technology (rule W6, shares of cells): LTE 0.847 of cells; GSM on 93 of 290 sites, 3 of them GSM-only rural sites; 1.79 LTE layers per LTE site on average.
- Tiny profile: 5 sites and 27 cells from the same generator and band rules. The START asked for about 7 sites and about 20 cells; with about 1.79 LTE layers per site, 7 sites would carry about 38 cells, so the two targets cannot both hold.
- Names: DNs follow TS 32.300 V19.0.0 clause 7. Class names are the XML solution-set spellings (TS 28.659 V20.0.0 for E-UTRAN; TS 28.656 V19.0.0 for GERAN: BssFunction, BtsSiteMgr, GsmCell), where the TS 28.655 information model writes BSSFunction, BTSSiteMgr, GSMCell.

## Determinism

SHA-256 over sites, cells and the population raster (rule W1); a test rebuilds both
profiles and compares.

| Profile | SHA-256 |
|---|---|
| demo | 19ef353c2788c132a22bc53a15c0804644038475d83550a45517e85e275c5e56 |
| tiny | 708ba63770b56a6756662ef66b355981813011853e6b83ee3a19f8b42d4321d5 |

## START and ASSUMPTION values

| Parameter | Value |
|---|---|
| raster_km (W4, START) | 0.25 |
| urban_min_density_per_km2 (START) | 2900.0 |
| suburban_min_density_per_km2 (START) | 850.0 |
| isd_km (START) | {"urban": 0.7, "suburban": 2.0, "rural": 4.0} |
| site_jitter_fraction (ASSUMPTION) | 0.15 |
| min_spacing_fraction (ASSUMPTION) | 0.7 |
| class_smoothing_km (ASSUMPTION) | 1.5 |
| sectors (W6, ASSUMPTION) | 3 |
| azimuth_jitter_sd_deg (ASSUMPTION) | 10.0 |
| gsm_cosite_share (START) | {"urban": 0.25, "suburban": 0.3, "rural": 0.55} |
| lte_extra_layer_p (START; B3 on every LTE site) | {"urban": {"B1": 0.6, "B40": 0.3}, "suburban": {"B1": 0.3, "B40": 0.15}, "rural": {"B8": 0.5, "B28": 0.3}} |
| demo vendor_split_x_km (START, design choice) | 12.5 |
| demo gsm_only_share_rural (START) | 0.08 |
| demo rural density served/expansion per km2 (ASSUMPTION) | [80.0, 15.0] |
| demo settlements (START) | {"city": [3, 150000.0], "town": [16, 20000.0], "served villages": [60], "expansion villages": [150]} |
