"""Deterministic random streams (rule W1).

Every generator draws from `np.random.default_rng([BASE_SEED, purpose_id,
entity_id])`: no clock, no global random state, so the same seed gives the
same world byte for byte.
"""

from enum import IntEnum

import numpy as np

BASE_SEED = 20260930


class Purpose(IntEnum):
    """Purpose ids, one per kind of draw (rule W1).

    Values below 10 are left to the stack check, which seeds its own
    counters with purpose 1.
    """

    SETTLEMENT_PLACEMENT = 10
    SETTLEMENT_SIZE = 11
    RURAL_DENSITY = 12
    SITE_JITTER = 20
    SITE_TECHNOLOGY = 21
    SITE_LAYERS = 22
    SECTOR_AZIMUTH = 23
    MODEL_NOISE = 30


def rng(purpose: Purpose, entity_id: int) -> np.random.Generator:
    """Return the random stream for one purpose and entity.

    Args:
        purpose: What the draw is for.
        entity_id: The entity the draw belongs to (settlement, site, ...);
            0 for draws that belong to the whole world.

    Returns:
        A generator seeded with [BASE_SEED, purpose, entity_id].
    """
    return np.random.default_rng([BASE_SEED, int(purpose), entity_id])
