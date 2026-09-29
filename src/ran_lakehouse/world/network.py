"""Sites, sectors and cells of the served region (rules W6, M1, C1).

Sites sit on a hexagonal lattice per area class, spaced by the class
inter-site distance and jittered, kept where the population layer has that
class. Each site gets its technologies, LTE band layers and vendor; each
sector on each band is one cell (rule W6).
"""

from dataclasses import dataclass

import numpy as np

from ran_lakehouse.seeds import Purpose, rng
from ran_lakehouse.world.population import PopulationLayer
from ran_lakehouse.world.profiles import (
    AZIMUTH_JITTER_SD_DEG,
    GSM_COSITE_SHARE,
    ISD_KM,
    LTE_EXTRA_LAYER_P,
    MAP_EDGE_MARGIN_KM,
    MIN_SPACING_FRACTION,
    SECTOR_AZIMUTHS_DEG,
    SECTORS,
    SITE_JITTER_FRACTION,
    SUBURBAN_MIN_DENSITY,
    URBAN_MIN_DENSITY,
    Profile,
)

AREA_CLASSES = ("urban", "suburban", "rural")

# Downlink range in MHz and duplex mode per band (rule W6). LTE: TS 36.101
# V20.1.0 clause 5.5, Table 5.5-1. GSM: TS 45.005 V19.0.0 clause 2 (E-GSM
# 900 and DCS 1800 base transmit).
BANDS = {
    "B28": (758.0, 803.0, "FDD"),
    "B8": (925.0, 960.0, "FDD"),
    "B3": (1805.0, 1880.0, "FDD"),
    "B1": (2110.0, 2170.0, "FDD"),
    "B40": (2300.0, 2400.0, "TDD"),
    "G900": (925.0, 960.0, "FDD"),
    "G1800": (1805.0, 1880.0, "FDD"),
}
LTE_BAND_ORDER = ("B28", "B8", "B3", "B1", "B40")
TDD_BANDS = frozenset(b for b, (_, _, mode) in BANDS.items() if mode == "TDD")
# GSM band per area class (owner decision: 900 rural, 1800 urban; suburban
# on 900, ASSUMPTION).
GSM_BAND = {"urban": "G1800", "suburban": "G900", "rural": "G900"}

# Operations naming (rule C1). DNs follow TS 32.300 V19.0.0 clause 7: RDNs
# "Class=value", comma separated, root first. Class names are the XML
# solution-set spellings, as carried in Bulk CM files: E-UTRAN from TS 28.659
# V20.0.0 (ENBFunction, EUtranCellFDD, EUtranCellTDD, the same as the TS
# 28.658 IS), GERAN from TS 28.656 V19.0.0 (BssFunction, BtsSiteMgr, GsmCell;
# the TS 28.655 IS writes BSSFunction, BTSSiteMgr, GSMCell). The root and
# region subnetworks are this world's own names.
ROOT_SUBNETWORK = "RanLake"
VENDOR_REGION = {"huawei": "West", "nokia": "East"}
BSC_NAME = {"huawei": "BSC01", "nokia": "BSC02"}


@dataclass(frozen=True)
class Site:
    """One site of the served region.

    Attributes:
        site_id: Stable number, 1-based, ordered by x then y.
        name: Site name, "SITE0001".
        x_km: Site x.
        y_km: Site y.
        area_class: "urban", "suburban" or "rural".
        vendor: "huawei" (Huawei-style dialect) or "nokia" (Nokia-style).
        has_lte: Whether the site carries LTE.
        has_gsm: Whether the site carries GSM.
        lte_bands: LTE bands on the site, in LTE_BAND_ORDER.
        gsm_band: GSM band, or empty when the site has no GSM.
        azimuths_deg: Azimuth of each sector.
    """

    site_id: int
    name: str
    x_km: float
    y_km: float
    area_class: str
    vendor: str
    has_lte: bool
    has_gsm: bool
    lte_bands: tuple[str, ...]
    gsm_band: str
    azimuths_deg: tuple[float, ...]


@dataclass(frozen=True)
class Cell:
    """One cell: one sector on one band.

    Attributes:
        cell_name: Stable name, for example "ENB0142_B3_1".
        site_id: Owning site.
        technology: "LTE" or "GSM".
        band: Band, for example "B3" or "G900".
        sector: Sector number, 1-based.
        azimuth_deg: Sector azimuth.
        object_class: NRM class (rule C1).
        managed_element: Managed element holding the cell.
        dn: Distinguished name (TS 32.300 style).
    """

    cell_name: str
    site_id: int
    technology: str
    band: str
    sector: int
    azimuth_deg: float
    object_class: str
    managed_element: str
    dn: str


def area_class_of(density: np.ndarray) -> np.ndarray:
    """Classify densities into area classes.

    Args:
        density: Persons per km2.

    Returns:
        Class names, same shape.
    """
    return np.where(
        density >= URBAN_MIN_DENSITY,
        "urban",
        np.where(density >= SUBURBAN_MIN_DENSITY, "suburban", "rural"),
    )


def lattice(
    isd_km: float, width_km: float, height_km: float
) -> list[tuple[int, int, float, float]]:
    """Hexagonal lattice points covering a rectangle.

    Args:
        isd_km: Distance between neighbouring points.
        width_km: Rectangle width.
        height_km: Rectangle height.

    Returns:
        (row, column, x, y) for each point inside the rectangle.
    """
    row_step = isd_km * np.sqrt(3.0) / 2.0
    points = []
    for row in range(int(height_km / row_step) + 1):
        y = row_step * (row + 0.5)
        offset = isd_km / 2.0 if row % 2 else 0.0
        for col in range(int(width_km / isd_km) + 1):
            x = isd_km * (col + 0.5) + offset
            if x < width_km and y < height_km:
                points.append((row, col, x, y))
    return points


def place_sites(profile: Profile, population: PopulationLayer) -> list[tuple[float, float, str]]:
    """Place site positions per area class, densest class first.

    Args:
        profile: The world profile.
        population: The population layer.

    Returns:
        (x, y, area class) for every site, unordered.
    """
    sites: list[tuple[float, float, str]] = []
    for class_index, area_class in enumerate(AREA_CLASSES):
        isd = ISD_KM[area_class]
        for row, col, x, y in lattice(isd, profile.served_width_km, profile.height_km):
            entity = (class_index + 1) * 1_000_000 + row * 1_000 + col
            jitter = SITE_JITTER_FRACTION[area_class] * isd
            dx, dy = rng(Purpose.SITE_JITTER, entity).uniform(-1.0, 1.0, 2) * jitter
            px, py = float(x + dx), float(y + dy)
            # Sites stay inside the served region and away from the map edge.
            inside_x = MAP_EDGE_MARGIN_KM <= px < profile.served_width_km
            inside_y = MAP_EDGE_MARGIN_KM <= py <= profile.height_km - MAP_EDGE_MARGIN_KM
            if not (inside_x and inside_y):
                continue
            density = population.class_density_at(np.array([px]), np.array([py]))
            if str(area_class_of(density)[0]) != area_class:
                continue
            # Where classes meet, a coarser lattice point can land next to a
            # denser class's site; keep the coarser site only if it is not
            # too close to a site already placed.
            spacing = MIN_SPACING_FRACTION * isd
            if all(
                np.hypot(px - sx, py - sy) >= spacing
                for sx, sy, other in sites
                if other != area_class
            ):
                sites.append((px, py, area_class))
    return sites


def build_sites(profile: Profile, population: PopulationLayer) -> tuple[Site, ...]:
    """Build every site with its technologies, bands, vendor and sectors.

    Args:
        profile: The world profile.
        population: The population layer.

    Returns:
        Sites ordered by x then y, numbered from 1.
    """
    positions = sorted(place_sites(profile, population), key=lambda p: (p[0], p[1]))
    sites: list[Site] = []
    for site_id, (x, y, area_class) in enumerate(positions, start=1):
        tech = rng(Purpose.SITE_TECHNOLOGY, site_id).uniform(size=2)
        gsm_only = area_class == "rural" and tech[0] < profile.gsm_only_share_rural
        has_gsm = gsm_only or bool(tech[1] < GSM_COSITE_SHARE[area_class])
        bands: tuple[str, ...] = ()
        if not gsm_only:
            draws = rng(Purpose.SITE_LAYERS, site_id).uniform(size=len(LTE_BAND_ORDER))
            extra = LTE_EXTRA_LAYER_P[area_class]
            bands = tuple(
                b
                for b, u in zip(LTE_BAND_ORDER, draws, strict=True)
                if b == "B3" or (b in extra and u < extra[b])
            )
        jitter = rng(Purpose.SECTOR_AZIMUTH, site_id).normal(0.0, AZIMUTH_JITTER_SD_DEG, SECTORS)
        azimuths = tuple(
            round(float((a + j) % 360.0), 1)
            for a, j in zip(SECTOR_AZIMUTHS_DEG, jitter, strict=True)
        )
        sites.append(
            Site(
                site_id=site_id,
                name=f"SITE{site_id:04d}",
                x_km=round(x, 3),
                y_km=round(y, 3),
                area_class=area_class,
                vendor="huawei" if x < profile.vendor_split_x_km else "nokia",
                has_lte=not gsm_only,
                has_gsm=has_gsm,
                lte_bands=bands,
                gsm_band=GSM_BAND[area_class] if has_gsm else "",
                azimuths_deg=azimuths,
            )
        )
    return tuple(sites)


def build_cells(sites: tuple[Site, ...]) -> tuple[Cell, ...]:
    """Expand sites into cells, one per sector and band.

    Args:
        sites: The sites.

    Returns:
        Cells ordered by site, LTE before GSM, band, then sector.
    """
    cells = []
    for site in sites:
        region = f"SubNetwork={ROOT_SUBNETWORK},SubNetwork={VENDOR_REGION[site.vendor]}"
        enb = f"ENB{site.site_id:04d}"
        for band in site.lte_bands:
            object_class = "EUtranCellTDD" if band in TDD_BANDS else "EUtranCellFDD"
            for sector, azimuth in enumerate(site.azimuths_deg, start=1):
                name = f"{enb}_{band}_{sector}"
                dn = f"{region},ManagedElement={enb},ENBFunction=1,{object_class}={name}"
                cells.append(
                    Cell(name, site.site_id, "LTE", band, sector, azimuth, object_class, enb, dn)
                )
        if site.has_gsm:
            bsc = BSC_NAME[site.vendor]
            bts = f"BTS{site.site_id:04d}"
            for sector, azimuth in enumerate(site.azimuths_deg, start=1):
                name = f"{bts}_{site.gsm_band}_{sector}"
                dn = f"{region},ManagedElement={bsc},BssFunction=1,BtsSiteMgr={bts},GsmCell={name}"
                cells.append(
                    Cell(
                        name,
                        site.site_id,
                        "GSM",
                        site.gsm_band,
                        sector,
                        azimuth,
                        "GsmCell",
                        bsc,
                        dn,
                    )
                )
    return tuple(cells)
