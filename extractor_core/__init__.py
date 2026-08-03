"""Portable, read-only extraction primitives used by the first-run coordinator."""

from .assets import build_asset_resolution_database
from .instances import build_instance_database, build_unresolved_asset_database
from .resources import (
    build_resource_database,
    extract_bootstrap_metadata,
    extract_candidate_bundles,
    read_bundle_candidate_names,
    read_bundle_candidate_names_by_map,
    read_bundle_closure_names_by_map,
    write_bundle_scan_index,
)
from .terrain import extract_terrain_surface

__all__ = [
    "build_asset_resolution_database",
    "build_instance_database",
    "build_unresolved_asset_database",
    "build_resource_database",
    "extract_bootstrap_metadata",
    "extract_candidate_bundles",
    "read_bundle_candidate_names",
    "read_bundle_candidate_names_by_map",
    "read_bundle_closure_names_by_map",
    "write_bundle_scan_index",
    "extract_terrain_surface",
]
