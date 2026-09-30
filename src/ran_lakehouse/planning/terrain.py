"""Terrain of the expansion area (rule G2): a synthetic height map.

Spectral synthesis, written fresh: white noise in the Fourier domain is
shaped by a power-law spectrum |k|^-SPECTRAL_BETA, transformed back and
rescaled to the relief range. The served region has no terrain (its
propagation model is Hata, rule M); a point west of the expansion area
takes the height of the area's western edge (ASSUMPTION: the served plain
continues at the edge heights), so line-of-sight paths to hubs in the
served region can be profiled.
"""

from dataclasses import dataclass

import numpy as np

from ran_lakehouse.seeds import Purpose, rng
from ran_lakehouse.world.profiles import Profile

STEP_KM = 0.1  # height map spacing (ASSUMPTION)
RELIEF_M = (200.0, 600.0)  # lowest and highest ground, m above sea level (START)
# Power-law exponent of the height spectrum: 2 gives rough, 4 very smooth
# ground (START, judged by eye on the rendered hillshade).
SPECTRAL_BETA = 3.2
# Wavelengths shorter than this are removed, so hills are not jagged at the
# grid scale (START).
MIN_WAVELENGTH_KM = 0.5


@dataclass(frozen=True)
class Terrain:
    """Ground height over the expansion area.

    Attributes:
        x0_km: x of the first column (the served region's eastern edge).
        y0_km: y of the first row.
        step_km: Grid spacing.
        heights_m: Ground height, shape (rows, cols); row 0 is the south.
    """

    x0_km: float
    y0_km: float
    step_km: float
    heights_m: np.ndarray

    def height_at(self, x_km: np.ndarray, y_km: np.ndarray) -> np.ndarray:
        """Ground height at points, bilinear between grid nodes.

        Points outside the area take the nearest edge's height.

        Args:
            x_km: x of the points.
            y_km: y of the points (broadcast with x_km).

        Returns:
            Heights, m.
        """
        rows, cols = self.heights_m.shape
        fx = np.clip((np.asarray(x_km) - self.x0_km) / self.step_km, 0.0, cols - 1.0)
        fy = np.clip((np.asarray(y_km) - self.y0_km) / self.step_km, 0.0, rows - 1.0)
        c0 = np.minimum(np.floor(fx).astype(int), cols - 2)
        r0 = np.minimum(np.floor(fy).astype(int), rows - 2)
        tx, ty = fx - c0, fy - r0
        h = self.heights_m
        top = h[r0, c0] * (1 - tx) + h[r0, c0 + 1] * tx
        bottom = h[r0 + 1, c0] * (1 - tx) + h[r0 + 1, c0 + 1] * tx
        result: np.ndarray = top * (1 - ty) + bottom * ty
        return result

    def profile(
        self, x0_km: float, y0_km: float, x1_km: float, y1_km: float, samples: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Ground heights along a straight path, end points included.

        Args:
            x0_km: Start x.
            y0_km: Start y.
            x1_km: End x.
            y1_km: End y.
            samples: Number of points.

        Returns:
            (distance from the start in km, ground height in m) per point.
        """
        t = np.linspace(0.0, 1.0, samples)
        x = x0_km + t * (x1_km - x0_km)
        y = y0_km + t * (y1_km - y0_km)
        return t * float(np.hypot(x1_km - x0_km, y1_km - y0_km)), self.height_at(x, y)


def build_terrain(profile: Profile) -> Terrain:
    """The expansion area's height map, deterministic per seed.

    Args:
        profile: World profile (the expansion area is x from served_width_km
            to width_km, the full height).

    Returns:
        The terrain.
    """
    width = profile.width_km - profile.served_width_km
    cols = round(width / STEP_KM) + 1
    rows = round(profile.height_km / STEP_KM) + 1
    noise = rng(Purpose.TERRAIN, 0).standard_normal((rows, cols))
    ky = np.fft.fftfreq(rows, d=STEP_KM)[:, None]
    kx = np.fft.rfftfreq(cols, d=STEP_KM)[None, :]
    k = np.hypot(kx, ky)
    shape = np.zeros_like(k)
    keep = (k > 0) & (k <= 1.0 / MIN_WAVELENGTH_KM)
    shape[keep] = k[keep] ** (-SPECTRAL_BETA / 2.0)
    field = np.fft.irfft2(np.fft.rfft2(noise) * shape, s=(rows, cols))
    low, high = RELIEF_M
    span = field.max() - field.min()
    heights = low + (field - field.min()) / span * (high - low)
    return Terrain(profile.served_width_km, 0.0, STEP_KM, heights)
