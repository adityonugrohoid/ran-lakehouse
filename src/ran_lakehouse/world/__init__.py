"""The synthetic world (rule W): map, population, sites and cells."""

from dataclasses import dataclass

from ran_lakehouse.world.network import Cell, Site, build_cells, build_sites
from ran_lakehouse.world.population import PopulationLayer, build_population
from ran_lakehouse.world.profiles import PROFILES, Profile


@dataclass(frozen=True)
class World:
    """One generated world.

    Attributes:
        profile: The profile it was built from.
        population: Population raster and settlements.
        sites: Sites of the served region.
        cells: Cells, one per sector and band.
    """

    profile: Profile
    population: PopulationLayer
    sites: tuple[Site, ...]
    cells: tuple[Cell, ...]


def build_world(profile_name: str) -> World:
    """Build the world of a profile (rule W1: same profile, same world).

    Args:
        profile_name: A key of PROFILES.

    Returns:
        The world.

    Raises:
        KeyError: If the profile does not exist.
    """
    if profile_name not in PROFILES:
        raise KeyError(f"unknown profile {profile_name!r}; known: {sorted(PROFILES)}")
    profile = PROFILES[profile_name]
    population = build_population(profile)
    sites = build_sites(profile, population)
    return World(profile, population, sites, build_cells(sites))
