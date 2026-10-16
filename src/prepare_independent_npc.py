"""Prepare an exact independent NPC prefab part for the configured static reader."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from endfield_character_bundle_inputs import prepare_provider_bundles
from endfield_character_cache import CharacterDataCache
from endfield_character_npc_catalog import read_independent_npc_catalog
from endfield_character_enemy_index import enemy_index_from_catalog
from endfield_character_sources import CharacterSourceContext


def prepare(args: argparse.Namespace) -> None:
    catalog = json.loads(args.catalog.read_text(encoding="utf-8-sig"))
    provider_index = Path(catalog["provider_index_source"]["path"])
    cache_root = args.cache_dir
    if cache_root is None:
        raise ValueError("Independent NPC preparation requires an explicit cache directory")
    npc = read_independent_npc_catalog(args.game_root, provider_index, CharacterDataCache(cache_root),
                                     enemy_index_from_catalog(catalog, cache_root), lambda: None)
    entries = [entry for entry in npc.entries if entry.npc_id == args.character_id and entry.source_kind == "prefab"
               and entry.resource_reference.template_id == args.template_id]
    providers = [provider for entry in entries for part in entry.resource_reference.prefab_parts
                 if part.part_name == args.part_name and part.resource_path == args.resource_path
                 for provider in part.providers if provider.logical_name == args.provider]
    if len(providers) != 1 or len(providers[0].serialized_cabs) != 1:
        raise ValueError("NPC prefab part/provider must resolve uniquely to one source CAB")
    context = CharacterSourceContext(args.game_root, Path(), cache_root, args.output.parent,
                                     provider_index, args.database, args.cab_inventory)
    files = prepare_provider_bundles(context, providers[0], args.output / "bundles", lambda: None,
                                    lambda stage, fraction, message: None)
    manifest = {"format": "EndfieldSingleCharacterInput/1", "unity_version": args.unity_version,
                "bundles": [{"path": file.path, "bytes": file.bytes, "sha256": file.sha256} for file in files],
                "root": {"source": providers[0].serialized_cabs[0].name, "resource_path": args.resource_path}}
    (args.output / "input.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (args.output / "preparation_receipt.json").write_text(json.dumps({"format": "EndfieldIndependentNpcPreparation/1",
        "source_identity": npc.source_identity, "npc_id": args.character_id,
        "part_name": args.part_name, "provider": asdict(providers[0]), "bundles": [asdict(file) for file in files]}, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repository-src", "catalog", "game-root", "database", "cab-inventory", "output", "cache-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("character-id", "template-id", "part-name", "resource-path", "provider", "unity-version", "variant"):
        parser.add_argument("--" + name, required=True)
    prepare(parser.parse_args())


if __name__ == "__main__":
    main()
