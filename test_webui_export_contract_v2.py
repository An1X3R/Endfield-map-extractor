from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import jsonschema

import webui_export_contract_v2 as contract


def create_game_install(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "Endfield.exe").write_bytes(b"")
    global_managers = root / "Endfield_Data" / "globalgamemanagers"
    global_managers.parent.mkdir(parents=True)
    global_managers.write_bytes(b"")
    (root / "Endfield_Data" / "StreamingAssets" / "VFS").mkdir(parents=True)
    return root


def payload(map_id: str = "map01") -> dict:
    return {
        "format": contract.CONTRACT_FORMAT,
        "request_id": "test-request",
        "map_id": map_id,
        "selection": {"type": "preview_rect", "umin": 0.1, "umax": 0.3, "vmin": 0.2, "vmax": 0.4},
        "layers": {"terrain": True, "instances": True, "water": True, "effects": True},
        "water_mode": "stable_eevee",
        "effects": {"selection_mode": "full_system_by_anchor"},
        "output": {"root": os.environ.get("ENDFIELD_EXPORT_ROOT", tempfile.gettempdir()), "label": "contract_test"},
    }


class ContractTests(unittest.TestCase):
    def test_blend_requires_current_user_blender(self) -> None:
        value = payload()
        value["export_mode"] = "blend"
        with self.assertRaisesRegex(ValueError, "source.blender_exe"):
            contract.canonicalize_job(value)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = create_game_install(root / "renamed-install")
            blender = root / "custom-blender" / "blender.exe"
            blender.parent.mkdir()
            blender.write_bytes(b"test")
            output = root / "output"
            output.mkdir()
            value["source"] = {"game_root": str(game), "blender_exe": str(blender)}
            value["output"]["root"] = str(output)
            value["export_groups"] = ["building", "prop"]
            result = contract.canonicalize_job(value)
            self.assertEqual(result["export_mode"], "blend")
            self.assertEqual(result["source"]["blender_exe"], str(blender.resolve()))
            self.assertEqual(result["export_groups"], ["building", "prop"])
            schema = json.loads(Path(__file__).with_name("webui_export_job_v2.schema.json").read_text())
            jsonschema.validate(value, schema)

    def test_unknown_export_mode_fails(self) -> None:
        value = payload()
        value["export_mode"] = "pretend_blend"
        with self.assertRaisesRegex(ValueError, "Unsupported export_mode"):
            contract.canonicalize_job(value)

    def test_map01_preview_uses_decreasing_z(self) -> None:
        canonical = contract.canonicalize_job(payload("map01"))
        self.assertEqual(canonical["preview"]["v_direction"], "z_decreasing")
        self.assertEqual(canonical["bounds"], {"xmin": -896.0, "xmax": -384.0, "zmin": 128.0, "zmax": 640.0})

    def test_map02_world_bounds_snap_outward(self) -> None:
        value = payload("map02")
        value["selection"] = {"type": "world_bounds", "xmin": -900, "xmax": -100, "zmin": 100, "zmax": -200}
        canonical = contract.canonicalize_job(value)
        self.assertEqual(canonical["bounds"], {"xmin": -1024.0, "xmax": 0.0, "zmin": -256.0, "zmax": 128.0})

    def test_effect_selection_defaults_and_estimate(self) -> None:
        value = payload()
        del value["effects"]
        canonical = contract.canonicalize_job(value)
        self.assertEqual(canonical["effects"]["selection_mode"], "full_system_by_anchor")
        self.assertGreater(canonical["job_id"], "")
        self.assertEqual(contract.estimate_workload(canonical)["enabled_layers"], 4)
        self.assertEqual(canonical["export_groups"], ["all"])

    def test_export_groups_and_source_paths(self) -> None:
        value = payload()
        value["export_groups"] = ["lighting", "particle", "lighting"]
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            root = create_game_install(temporary_root / "game" / "renamed-install")
            output = temporary_root / "output"
            output.mkdir()
            blender = temporary_root / "blender.exe"
            blender.write_text("demo", encoding="ascii")
            value["source"] = {"game_root": str(root), "blender_exe": str(blender)}
            value["output"]["root"] = str(output)
            canonical = contract.canonicalize_job(value)
        self.assertEqual(canonical["export_groups"], ["lighting", "particle"])
        self.assertTrue(canonical["source"]["game_read_only"])

    def test_data_package_source_does_not_require_blender(self) -> None:
        value = payload()
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            root = create_game_install(temporary_root / "game" / "portable-build")
            output = temporary_root / "output"
            output.mkdir()
            value["source"] = {"game_root": str(root)}
            value["output"]["root"] = str(output)
            canonical = contract.canonicalize_job(value)
        self.assertNotIn("blender_exe", canonical["source"])
        self.assertTrue(canonical["source"]["game_read_only"])

    def test_source_paths_require_endfield_install_structure(self) -> None:
        missing_entries = (
            ("missing-executable", Path("Endfield.exe"), "Endfield.exe", False),
            (
                "missing-global-managers",
                Path("Endfield_Data") / "globalgamemanagers",
                "Endfield_Data/globalgamemanagers",
                False,
            ),
            (
                "missing-vfs",
                Path("Endfield_Data") / "StreamingAssets" / "VFS",
                "Endfield_Data/StreamingAssets/VFS",
                True,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for case_name, relative_path, expected_label, is_directory in missing_entries:
                with self.subTest(missing=expected_label):
                    root = create_game_install(Path(temporary) / case_name)
                    missing_path = root / relative_path
                    missing_path.rmdir() if is_directory else missing_path.unlink()
                    with self.assertRaisesRegex(ValueError, expected_label.replace("/", r"\/")):
                        contract.validate_source_paths({"game_root": str(root)})

    def test_request_schema_accepts_package_only_source_and_all_layer_flags(self) -> None:
        value = payload()
        value["source"] = {"game_root": r"X:\Games\Endfield Game"}
        value["layers"].update({"roads": True, "vegetation": True, "lights": True})
        schema = json.loads(
            Path(__file__).with_name("webui_export_job_v2.schema.json").read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(schema).validate(value)

    def test_unknown_export_group_is_rejected(self) -> None:
        value = payload()
        value["export_groups"] = ["shader_magic"]
        with self.assertRaises(ValueError):
            contract.canonicalize_job(value)

    def test_output_root_is_restricted(self) -> None:
        value = payload()
        with tempfile.TemporaryDirectory() as temporary:
            game_root = create_game_install(Path(temporary) / "renamed-game-root")
            blender = Path(temporary) / "blender.exe"
            blender.write_text("demo", encoding="ascii")
            value["source"] = {"game_root": str(game_root), "blender_exe": str(blender)}
            value["output"]["root"] = str(game_root)
            with self.assertRaises(ValueError):
                contract.canonicalize_job(value)

    def test_output_root_can_use_an_external_writable_folder(self) -> None:
        value = payload()
        with tempfile.TemporaryDirectory() as temporary:
            value["output"]["root"] = temporary
            canonical = contract.canonicalize_job(value)
        self.assertEqual(canonical["output"]["root"], str(Path(temporary).resolve()))

    def test_job_id_is_canonicalized_and_restricted(self) -> None:
        value = payload()
        value["job_id"] = "ui-job_01"
        self.assertEqual(contract.canonicalize_job(value)["job_id"], "ui-job_01")
        value["job_id"] = "../outside"
        with self.assertRaises(ValueError):
            contract.canonicalize_job(value)

    def test_transition_and_event(self) -> None:
        contract.validate_transition("queued", "preparing")
        with self.assertRaises(ValueError):
            contract.validate_transition("completed", "building")
        event = contract.make_event("job", 1, "building", "building", 0.5, "halfway", {"stage": "map"})
        self.assertEqual(event["format"], contract.EVENT_FORMAT)
        self.assertEqual(event["payload"]["stage"], "map")


if __name__ == "__main__":
    unittest.main()
