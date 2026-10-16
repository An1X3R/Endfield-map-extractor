"""Export original illustration PNGs through the installed source preparation.

List selectors:
  python src/export_illustrations.py list --character-config .tools/character/runtime.json
Export every discovered illustration to a new directory:
  python src/export_illustrations.py export --character-config .tools/character/runtime.json \
    --helper-cache .tools/scene --output <new-directory> --all
Select particular characters with repeated --character-id arguments, or select
exact variants using repeated --illustration-id arguments from the catalog.
This command does not launch Blender, download tools or modify game files.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from endfield_character_service import CharacterService
from endfield_illustrations import (
    IllustrationCatalog, IllustrationExportError, export_illustrations,
    public_illustration_catalog, read_illustration_catalog,
)
from endfield_scene_helpers import require_prepared_scene_helpers


def _check_cancelled() -> None:
    """CLI interruption is delivered through KeyboardInterrupt."""


def _progress(stage: str, fraction: float, message: str) -> None:
    print(json.dumps({"stage": stage, "fraction": fraction, "message": message}), file=sys.stderr, flush=True)


def _selected_ids(catalog: IllustrationCatalog, arguments: argparse.Namespace) -> tuple[str, ...]:
    if arguments.all:
        return tuple(entry.illustration_id for entry in catalog.entries)
    if arguments.illustration_id:
        return tuple(arguments.illustration_id)
    characters = tuple(arguments.character_id)
    if len(set(characters)) != len(characters):
        raise IllustrationExportError("Duplicate --character-id arguments")
    known = {entry.character_id for entry in catalog.entries}
    if set(characters) - known:
        raise IllustrationExportError(f"Characters have no discovered illustration: {sorted(set(characters) - known)}")
    return tuple(entry.illustration_id for entry in catalog.entries if entry.character_id in characters)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", required=True)
    listing = subcommands.add_parser("list", help="List original illustrations and exact character names")
    listing.add_argument("--character-config", type=Path, required=True)
    exporting = subcommands.add_parser("export", help="Export original-size RGBA PNGs, without Blender")
    exporting.add_argument("--character-config", type=Path, required=True)
    exporting.add_argument("--helper-cache", type=Path, required=True)
    exporting.add_argument("--output", type=Path, required=True)
    selectors = exporting.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--all", action="store_true")
    selectors.add_argument("--character-id", action="append")
    selectors.add_argument("--illustration-id", action="append")
    arguments = parser.parse_args()
    context = CharacterService(arguments.character_config).prepared_source_context()
    catalog = read_illustration_catalog(context, _check_cancelled)
    if arguments.command == "list":
        print(json.dumps(public_illustration_catalog(catalog), ensure_ascii=False, indent=2))
        return
    selected = _selected_ids(catalog, arguments)
    helpers = require_prepared_scene_helpers(arguments.character_config.resolve(), arguments.helper_cache.resolve())
    result = export_illustrations(context, catalog, selected, helpers.probe, arguments.output.resolve(),
                                 _check_cancelled, _progress)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
