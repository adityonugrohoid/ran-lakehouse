"""World profiles (rule W7): one generator, different sizes.

Every number here is START (rule W7) or ASSUMPTION, replaced by reviewed
values recorded in the world report.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SettlementClass:
    """How many settlements of one kind, and how big.

    Attributes:
        kind: "city", "town" or "village".
        count: Number of settlements.
        median_population: Median of the lognormal size distribution.
        sigma: Lognormal shape (standard deviation of log population).
        spread_km_per_sqrt_10k: Gaussian spread of the settlement in km per
            square root of (population / 10,000); ASSUMPTION.
    """

    kind: str
    count: int
    median_population: float
    sigma: float
    spread_km_per_sqrt_10k: float


@dataclass(frozen=True)
class Profile:
    """Map, population and network sizes for one profile.

    Attributes:
        name: Profile name.
        width_km: Map width (x), km.
        height_km: Map height (y), km.
        served_width_km: The served region is x < served_width_km; the
            expansion area is the rest (rule W3).
        city_zone_width_km: Cities are placed at x < city_zone_width_km, the
            west of the served region.
        served_settlements: Settlement classes in the served region.
        expansion_villages: Villages in the expansion area (rule G1).
        rural_density_served: Mean rural density in the served region,
            persons per km2.
        rural_density_expansion: Mean rural density in the expansion area,
            persons per km2.
        vendor_split_x_km: Sites at x < this are Huawei-style, the rest
            Nokia-style (rule P5, a design choice).
        gsm_only_share_rural: Share of rural sites that carry GSM only.
    """

    name: str
    width_km: float
    height_km: float
    served_width_km: float
    city_zone_width_km: float
    served_settlements: tuple[SettlementClass, ...]
    expansion_villages: SettlementClass
    rural_density_served: float
    rural_density_expansion: float
    vendor_split_x_km: float
    gsm_only_share_rural: float


# Population raster cell, rule W4 (START 250 m).
RASTER_KM = 0.25

# Area classes from population density, persons per km2 (START, ASSUMPTION).
URBAN_MIN_DENSITY = 2900.0
SUBURBAN_MIN_DENSITY = 850.0

# Target inter-site distance per area class, km (START, owner decision:
# dense urban 0.5-1, suburban about 2, rural 4-6).
ISD_KM = {"urban": 0.7, "suburban": 2.0, "rural": 4.0}
# Site position jitter as a fraction of the class ISD, uniform in x and y;
# urban START 0.30 so city grids do not read as perfect lattices, else
# ASSUMPTION 0.15.
SITE_JITTER_FRACTION = {"urban": 0.30, "suburban": 0.15, "rural": 0.15}
# Every site stays at least this far inside the map edge, km (START).
MAP_EDGE_MARGIN_KM = 0.5
# A site is dropped if it is closer than this fraction of its own class ISD
# to a site already placed (ASSUMPTION).
MIN_SPACING_FRACTION = 0.7

SECTORS = 3  # rule W6, ASSUMPTION
SECTOR_AZIMUTHS_DEG = (0.0, 120.0, 240.0)
AZIMUTH_JITTER_SD_DEG = 10.0  # ASSUMPTION

# Share of sites per area class that also carry GSM (START, rule W6: about
# a third of sites overall).
GSM_COSITE_SHARE = {"urban": 0.25, "suburban": 0.3, "rural": 0.6}

# Probability that a site of each class carries each extra LTE layer; B3
# 1800 is on every LTE site (START, owner decision on the band mix).
LTE_EXTRA_LAYER_P = {
    "urban": {"B1": 0.6, "B40": 0.3},
    "suburban": {"B1": 0.3, "B40": 0.15},
    "rural": {"B8": 0.5, "B28": 0.3},
}

DEMO = Profile(
    name="demo",
    width_km=60.0,
    height_km=40.0,
    served_width_km=40.0,
    city_zone_width_km=16.0,
    served_settlements=(
        SettlementClass("city", 3, 150_000.0, 0.4, 0.55),
        SettlementClass("town", 16, 20_000.0, 0.5, 0.5),
        SettlementClass("village", 60, 1_500.0, 0.6, 0.6),
    ),
    expansion_villages=SettlementClass("village", 150, 900.0, 0.6, 0.6),
    rural_density_served=80.0,
    rural_density_expansion=15.0,
    vendor_split_x_km=12.5,
    gsm_only_share_rural=0.08,
)

TINY = Profile(
    name="tiny",
    width_km=11.0,
    height_km=4.0,
    served_width_km=8.0,
    city_zone_width_km=8.0,
    served_settlements=(SettlementClass("town", 1, 40_000.0, 0.0, 0.8),),
    expansion_villages=SettlementClass("village", 3, 800.0, 0.6, 0.6),
    rural_density_served=80.0,
    rural_density_expansion=15.0,
    vendor_split_x_km=4.0,
    gsm_only_share_rural=0.0,
)

PROFILES = {p.name: p for p in (TINY, DEMO)}
