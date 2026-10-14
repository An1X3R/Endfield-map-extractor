"""Check authenticated prefab delivery against real files and saved-frame evidence."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from endfield_character_npc_prefab_action import verify_prefab_action_delivery
from endfield_character_selection import CharacterSelection


class PrefabActionDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.build = self.output / "build"
        self.build.mkdir()
        self.blend = self.build / "character.blend"
        self.blend.write_bytes(b"verified file fixture")
        self.inputs = {}
        for field in ("source_blend", "static_source_audit", "static_build_receipt", "preparation_receipt",
                      "animation_receipt", "npc_animation_receipt"):
            path = self.output / (field + ".json")
            path.write_bytes(field.encode())
            self.inputs[field] = path
        self.selection = CharacterSelection("a" * 64, "npc", "template", "part", "npc", "assets/model.prefab", "main/model.ab")
        self.receipt = {"format": "EndfieldPrefabNpcActionReceipt/1", "status": "saved_reopened_all_frames_verified",
            "npc_id": "npc", "template_id": "template", "part_name": "part", "state_name": "IdleLoop",
            "source_material_geometry_shapes_preserved": True, "runtime_animator_reconstructed": False,
            "binding": {"frames": 3, "bind_world_calibration": {"calibrated": True}},
            "reopen_validation": {"status": "saved_active_action_sample_matrix_verified", "checked_frames": [1, 2, 3],
                                  "maximum_matrix_error": 1e-6},
            "input_hashes": {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.inputs.values()},
            "file": str(self.blend), "sha256": hashlib.sha256(self.blend.read_bytes()).hexdigest(),
            "bytes": self.blend.stat().st_size}

    def verify(self, receipt: dict[str, object]) -> list[dict[str, object]]:
        return verify_prefab_action_delivery(receipt, self.build, self.output, self.selection, "IdleLoop", 3, self.inputs)

    def test_real_file_hash_and_source_inputs_are_verified(self) -> None:
        self.assertEqual(self.verify(self.receipt)[0]["relative_path"], "build/character.blend")
        self.inputs["source_blend"].write_bytes(b"changed static source")
        with self.assertRaisesRegex(ValueError, "source evidence"):
            self.verify(self.receipt)

    def test_wrong_identity_missing_frames_and_legacy_binding_are_rejected(self) -> None:
        for field, value in (("npc_id", "other"), ("source_material_geometry_shapes_preserved", False),
                             ("runtime_animator_reconstructed", True)):
            with self.assertRaises(ValueError):
                self.verify({**self.receipt, field: value})
        for field, value in (("checked_frames", [1, 3]), ("maximum_matrix_error", float("nan")),
                             ("maximum_matrix_error", 1)):
            changed = deepcopy(self.receipt)
            changed["reopen_validation"][field] = value
            with self.assertRaisesRegex(ValueError, "all-frame"):
                self.verify(changed)
        changed = deepcopy(self.receipt)
        changed["binding"]["bind_world_calibration"]["calibrated"] = False
        with self.assertRaisesRegex(ValueError, "calibrated"):
            self.verify(changed)

    def test_delivery_cannot_escape_build_output_or_change_bytes(self) -> None:
        outside = self.output / "outside.blend"
        outside.write_bytes(self.blend.read_bytes())
        for changes in ({"file": str(outside)}, {"sha256": "f" * 64}, {"bytes": True}):
            with self.assertRaisesRegex(ValueError, "artifact"):
                self.verify({**self.receipt, **changes})


if __name__ == "__main__":
    unittest.main()
