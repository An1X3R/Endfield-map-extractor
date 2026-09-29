"""Portable, read-only extraction primitives used by the first-run coordinator."""

from importlib import import_module

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

_EXPORT_MODULES = {
    "build_asset_resolution_database": "assets",
    "build_instance_database": "instances",
    "build_unresolved_asset_database": "instances",
    "build_resource_database": "resources",
    "extract_bootstrap_metadata": "resources",
    "extract_candidate_bundles": "resources",
    "read_bundle_candidate_names": "resources",
    "read_bundle_candidate_names_by_map": "resources",
    "read_bundle_closure_names_by_map": "resources",
    "write_bundle_scan_index": "resources",
    "extract_terrain_surface": "terrain",
}


def __getattr__(name: str) -> object:
    """Keep public extraction imports lazy for Blender's dependency-free math path."""
    if name not in _EXPORT_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{_EXPORT_MODULES[name]}", __name__), name)
