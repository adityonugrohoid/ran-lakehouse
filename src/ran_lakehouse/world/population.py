"""Population layer (rule W4): settlements and a raster over the whole map.

Clustered settlements (cities, towns, villages) plus sparse rural density.
The expansion area (rule W3) holds only villages and rural density. The
shapes are ASSUMPTION, judged by eye and by the real-data cross-check
(rule E2).
"""

from dataclasses import dataclass

import numpy as np

from ran_lakehouse.seeds import Purpose, rng
from ran_lakehouse.world.profiles import RASTER_KM, Profile, SettlementClass

# Minimum spacing between settlement centres by kind, km (ASSUMPTION).
MIN_SPACING_KM = {"city": 8.0, "town": 4.0, "village": 1.2}
# Keep settlement centres this far inside their zone edges, km (ASSUMPTION).
EDGE_MARGIN_KM = {"city": 3.0, "town": 1.5, "village": 0.5}
# Correlation length of the rural density noise, km (ASSUMPTION).
RURAL_NOISE_KM = 1.5
# Lognormal sigma of the rural density noise (ASSUMPTION).
RURAL_NOISE_SIGMA = 0.6
MAX_PLACEMENT_TRIES = 10_000
# Area classes (urban, suburban, rural) are judged on density smoothed over
# this Gaussian length, so a village reads as rural and a town as a whole
# (ASSUMPTION).
CLASS_SMOOTHING_KM = 1.5


@dataclass(frozen=True)
class Settlement:
    """One settlement.

    Attributes:
        settlement_id: Stable id, unique in the world.
        kind: "city", "town" or "village".
        area: "served" or "expansion".
        x_km: Centre x.
        y_km: Centre y.
        population: Persons.
        spread_km: Gaussian spread of the settlement's population.
    """

    settlement_id: int
    kind: str
    area: str
    x_km: float
    y_km: float
    population: int
    spread_km: float


@dataclass(frozen=True)
class PopulationLayer:
    """The population raster and the settlements that shaped it.

    Attributes:
        persons: Persons per raster cell, shape (ny, nx); row 0 is the south
            edge (rule W2 origin at the south-west corner).
        class_density: Density smoothed over CLASS_SMOOTHING_KM, persons per
            km2, same shape; the area classes are judged on it.
        settlements: Every settlement.
        raster_km: Raster cell size.
    """

    persons: np.ndarray
    class_density: np.ndarray
    settlements: tuple[Settlement, ...]
    raster_km: float

    def class_density_at(self, x_km: np.ndarray, y_km: np.ndarray) -> np.ndarray:
        """Look up the smoothed density (persons per km2) at points.

        Args:
            x_km: Point x coordinates.
            y_km: Point y coordinates.

        Returns:
            Smoothed density of the raster cell holding each point.
        """
        ny, nx = self.class_density.shape
        col = np.clip((x_km / self.raster_km).astype(int), 0, nx - 1)
        row = np.clip((y_km / self.raster_km).astype(int), 0, ny - 1)
        density: np.ndarray = self.class_density[row, col]
        return density


def place_centres(
    cls: SettlementClass,
    zone: tuple[float, float, float, float],
    existing: list[tuple[float, float]],
    first_id: int,
) -> list[tuple[float, float]]:
    """Place settlement centres uniformly in a zone with a minimum spacing.

    Args:
        cls: Settlement class to place.
        zone: (x_min, x_max, y_min, y_max) in km.
        existing: Centres already placed; new ones keep the class spacing from
            them too.
        first_id: Settlement id of the first centre, the rule W1 entity id.

    Returns:
        The new centres, in id order.

    Raises:
        RuntimeError: If a centre cannot be placed within the try budget.
    """
    margin = EDGE_MARGIN_KM[cls.kind]
    spacing = MIN_SPACING_KM[cls.kind]
    x_min, x_max, y_min, y_max = zone
    placed: list[tuple[float, float]] = []
    for i in range(cls.count):
        stream = rng(Purpose.SETTLEMENT_PLACEMENT, first_id + i)
        for _ in range(MAX_PLACEMENT_TRIES):
            x = float(stream.uniform(x_min + margin, x_max - margin))
            y = float(stream.uniform(y_min + margin, y_max - margin))
            if all(np.hypot(x - ex, y - ey) >= spacing for ex, ey in existing + placed):
                placed.append((x, y))
                break
        else:
            raise RuntimeError(f"could not place {cls.kind} {first_id + i} in zone {zone}")
    return placed


def build_settlements(profile: Profile) -> tuple[Settlement, ...]:
    """Place and size every settlement of a profile.

    Args:
        profile: The world profile.

    Returns:
        Settlements in id order: served classes in profile order, then the
        expansion villages.
    """
    served_zone = (0.0, profile.served_width_km, 0.0, profile.height_km)
    city_zone = (0.0, profile.city_zone_width_km, 0.0, profile.height_km)
    expansion_zone = (profile.served_width_km, profile.width_km, 0.0, profile.height_km)
    plan = [
        (cls, "served", city_zone if cls.kind == "city" else served_zone)
        for cls in profile.served_settlements
    ]
    plan.append((profile.expansion_villages, "expansion", expansion_zone))
    settlements: list[Settlement] = []
    centres: list[tuple[float, float]] = []
    for cls, area, zone in plan:
        first_id = len(settlements) + 1
        new = place_centres(cls, zone, centres, first_id)
        centres += new
        for i, (x, y) in enumerate(new):
            sid = first_id + i
            size = rng(Purpose.SETTLEMENT_SIZE, sid).lognormal(
                np.log(cls.median_population), cls.sigma
            )
            population = round(float(size))
            spread = cls.spread_km_per_sqrt_10k * float(np.sqrt(population / 10_000))
            settlements.append(Settlement(sid, cls.kind, area, x, y, population, spread))
    return tuple(settlements)


def smooth_noise(shape: tuple[int, int], length_cells: float) -> np.ndarray:
    """Draw a smooth, unit-variance Gaussian field.

    Args:
        shape: Raster shape (ny, nx).
        length_cells: Gaussian smoothing length in raster cells.

    Returns:
        The field, mean 0 and standard deviation 1.
    """
    white = rng(Purpose.RURAL_DENSITY, 0).standard_normal(shape)
    ky = np.fft.fftfreq(shape[0])[:, None]
    kx = np.fft.fftfreq(shape[1])[None, :]
    kernel = np.exp(-2.0 * (np.pi * length_cells) ** 2 * (kx**2 + ky**2))
    field = np.real(np.fft.ifft2(np.fft.fft2(white) * kernel))
    result: np.ndarray = (field - field.mean()) / field.std()
    return result


def build_population(profile: Profile) -> PopulationLayer:
    """Build the population layer of a profile.

    Args:
        profile: The world profile.

    Returns:
        The raster and its settlements.
    """
    nx = round(profile.width_km / RASTER_KM)
    ny = round(profile.height_km / RASTER_KM)
    xc = (np.arange(nx) + 0.5) * RASTER_KM
    yc = (np.arange(ny) + 0.5) * RASTER_KM
    cell_km2 = RASTER_KM**2
    served = (xc < profile.served_width_km)[None, :]
    base = np.where(served, profile.rural_density_served, profile.rural_density_expansion)
    noise = smooth_noise((ny, nx), RURAL_NOISE_KM / RASTER_KM)
    # Lognormal multiplier with mean 1, so the mean rural density holds.
    multiplier = np.exp(RURAL_NOISE_SIGMA * noise - RURAL_NOISE_SIGMA**2 / 2)
    persons = base * multiplier * cell_km2
    settlements = build_settlements(profile)
    for s in settlements:
        gx = np.exp(-((xc - s.x_km) ** 2) / (2 * s.spread_km**2))
        gy = np.exp(-((yc - s.y_km) ** 2) / (2 * s.spread_km**2))
        kernel = np.outer(gy, gx)
        persons = persons + s.population * kernel / kernel.sum()
    class_density = gaussian_blur(persons, CLASS_SMOOTHING_KM / RASTER_KM) / cell_km2
    return PopulationLayer(
        persons=persons,
        class_density=class_density,
        settlements=settlements,
        raster_km=RASTER_KM,
    )


def gaussian_blur(values: np.ndarray, sigma_cells: float) -> np.ndarray:
    """Blur a raster with a Gaussian, zero outside the map.

    Args:
        values: The raster.
        sigma_cells: Gaussian standard deviation in raster cells.

    Returns:
        The blurred raster, same shape; totals are kept inside the map
        except for what spills past its edges.
    """
    pad = int(np.ceil(4 * sigma_cells))
    ny, nx = values.shape
    padded = np.zeros((ny + 2 * pad, nx + 2 * pad))
    padded[pad : pad + ny, pad : pad + nx] = values
    ky = np.fft.fftfreq(padded.shape[0])[:, None]
    kx = np.fft.fftfreq(padded.shape[1])[None, :]
    kernel = np.exp(-2.0 * (np.pi * sigma_cells) ** 2 * (kx**2 + ky**2))
    blurred = np.real(np.fft.ifft2(np.fft.fft2(padded) * kernel))
    result: np.ndarray = blurred[pad : pad + ny, pad : pad + nx]
    return result
