"""Assemble verified scene and character binaries without copying local runtime state."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil

from endfield_character_runtime import read_character_tools
from endfield_character_tools import CharacterToolsError, _object, _relative
from endfield_scene_tool_bundle import export_scene_tool_bundle


@dataclass(frozen=True)
class BundledTools:
    scene: Path
    character_receipt: Path


def export_character_tool_bundle(receipt: Path, destination: Path) -> Path:
    """Copy only the hash-verified runtime inventory, never state or machine configuration."""
    read_character_tools(receipt)
    document = _object(json.loads(receipt.read_text(encoding="utf-8")), "character receipt")
    files = _object(document.get("files"), "character runtime files")
    if destination.exists():
        raise FileExistsError(f"Tool bundle destination already exists: {destination}")
    source = receipt.resolve().parent
    for relative in files:
        original = _relative(source, relative, "runtime file")
        target = _relative(destination, relative, "bundle file")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
    output = destination / "character_tools.json"
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    read_character_tools(output)
    return output


def read_tool_bundle(root: Path) -> BundledTools:
    """Validate the bundle index and then the original tool receipts at installation time."""
    manifest = root / "tool_bundle.json"
    document = _object(json.loads(manifest.read_text(encoding="utf-8")), "tool bundle")
    if document.get("format") != "EndfieldToolBundle/1":
        raise CharacterToolsError(f"Unsupported tool bundle format: {manifest}")
    records = _object(document.get("receipts"), "bundle receipts")
    verified: dict[str, Path] = {}
    for name in ("scene", "character"):
        entry = _object(records.get(name), name)
        path = _relative(root, entry.get("path"), name)
        if not path.is_file():
            raise FileNotFoundError(f"Bundled tool receipt missing: {path}")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != entry.get("sha256"):
            raise CharacterToolsError(f"Bundled tool receipt hash mismatch: {path}")
        verified[name] = path
    read_character_tools(verified["character"])
    from endfield_scene_helpers import load_cached_scene_helper_report
    scene = root / "scene"
    prepared = load_cached_scene_helper_report(scene, Path(__file__).resolve().parent)
    if prepared is None or prepared.receipt_path != verified["scene"].resolve():
        raise CharacterToolsError(f"Scene receipt does not belong to the supplied bundle: {scene}")
    return BundledTools(scene, verified["character"])


def export_tool_bundle(scene_cache: Path, character_receipt: Path, destination: Path) -> Path:
    """Produce a relocatable binary-only installation for the matching application source."""
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"Choose a new bundle destination: {destination}")
    receipts = {
        "scene": export_scene_tool_bundle(scene_cache, destination / "scene"),
        "character": export_character_tool_bundle(character_receipt, destination / "character"),
    }
    records: dict[str, dict[str, str]] = {}
    for name, path in receipts.items():
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        records[name] = {"path": path.relative_to(destination).as_posix(), "sha256": digest}
    manifest = destination / "tool_bundle.json"
    manifest.write_text(json.dumps({
        "format": "EndfieldToolBundle/1", "platform": "windows-x64",
        "receipts": records,
        "requires": ["Matching application source", "Python environment", ".NET 9 runtime"],
        "distribution": "Local validation bundle; public redistribution requires the dependency notices and corresponding-source package.",
    }, indent=2) + "\n", encoding="utf-8")
    read_tool_bundle(destination)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene-cache", type=Path, required=True)
    parser.add_argument("--character-receipt", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    receipt = export_tool_bundle(args.scene_cache.resolve(), args.character_receipt.resolve(), args.output_root.resolve())
    print(json.dumps({"status": "assembled", "receipt": str(receipt)}))


if __name__ == "__main__":
    main()
