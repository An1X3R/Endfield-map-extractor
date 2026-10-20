"""Validate distinct packed image interpretations from an actual character source packet."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"),
                str(Path(__file__).resolve().parents[1] / "src/character_runtime")]

from character_mesh_effect import MESH_EFFECT_SHADERS
from rebuild_character_materials import load_images, read_packet, texture_colorspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("source-dir", "shader-identities", "selection", "renderer-visibility", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    packet = read_packet(args.source_dir, args.shader_identities, args.selection, args.renderer_visibility)
    images = load_images(packet)
    expected: dict[tuple[tuple[str, int], str], set[str]] = {}
    for source in packet.materials:
        for binding in source.textures:
            texture = packet.textures[binding.identity]
            space = texture_colorspace(binding, texture)
            alpha = "CHANNEL_PACKED"
            if source.shader in MESH_EFFECT_SHADERS:
                space = "Non-Color" if texture.color_space == 0 else "sRGB"
                alpha = "STRAIGHT"
            expected.setdefault((texture.identity, space), set()).add(alpha)
            image = images[(texture.identity, space, alpha)]
            if image.alpha_mode != alpha or image.colorspace_settings.name != space:
                raise AssertionError(f"Image interpretation differs from source role: {source.identity}/{binding.name}")
            if image.packed_file is None or hashlib.sha256(image.packed_file.data).hexdigest() != texture.sha256:
                raise AssertionError(f"Image role lost its exact packed source bytes: {texture.identity}/{space}/{alpha}")
    conflicts = {key: modes for key, modes in expected.items() if len(modes) > 1}
    if not conflicts:
        raise AssertionError("This integration input must contain a real shared texture with different alpha roles")
    for (identity, space), modes in conflicts.items():
        if len({images[(identity, space, alpha)].as_pointer() for alpha in modes}) != len(modes):
            raise AssertionError(f"Distinct source alpha roles share a mutable image datablock: {identity}/{space}")
    args.output.write_text(json.dumps({"status": "passed", "image_roles": len(images),
        "shared_identity_colorspace_with_distinct_alpha": len(conflicts),
        "packed_bytes_match_source": True}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
