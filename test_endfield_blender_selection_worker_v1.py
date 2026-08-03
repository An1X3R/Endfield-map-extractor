from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jsonschema

import blender_audit_endfield_selection_worker_v1 as reopen_audit
import endfield_blender_selection_worker_v1 as worker


SCHEMA_PATH = Path(__file__).with_name("endfield_blender_selection_worker_v1.schema.json")
REGISTRY_PATH = Path(__file__).with_name("endfield_blender_worker_profiles_v1.json")
TEST_DATASET_FINGERPRINT = "A" * 64


class FakeAuditObject:
    def __init__(self, name: str, **properties: object) -> None:
        self.name = name
        self.type = "MESH"
        self.data = object()
        self.modifiers: list[object] = []
        self._properties = dict(properties)

    def get(self, name: str, default: object = None) -> object:
        return self._properties.get(name, default)

    def __contains__(self, name: str) -> bool:
        return name in self._properties


def all_layers(enabled: bool = True) -> dict[str, bool]:
    return {
        "terrain": enabled,
        "instances": enabled,
        "roads": enabled,
        "vegetation": enabled,
        "effects": enabled,
        "water": enabled,
        "lights": enabled,
    }


def normalized_job(
    *,
    map_id: str,
    bounds: tuple[float, float, float, float],
    sector_keys: list[str],
    output_dir: Path,
    game_root: Path,
    batch_mode: str = "per_sector",
) -> dict[str, object]:
    log_dir = output_dir.parent / "log" / output_dir.name
    return {
        "format": worker.JOB_FORMAT,
        "sourceFormat": worker.JOB_FORMAT,
        "jobId": "test-job",
        "label": "test-job",
        "mapId": map_id,
        "bounds": worker.format_bounds(bounds),
        "boundsSemantics": "half-open [min,max)",
        "sectorKeys": sector_keys,
        "derivedSectorKeys": [
            worker.sector_key(value) for value in worker.sectors_for_bounds(bounds)
        ],
        "batchMode": batch_mode,
        "layers": all_layers(),
        "exportGroups": ["all"],
        "effects": {"selectionMode": "full_system_by_anchor"},
        "waterMode": "stable_eevee",
        "outputDir": str(output_dir),
        "logDir": str(log_dir),
        "eventsPath": str(log_dir / "events.jsonl"),
        "cancelMarker": str(log_dir / "CANCEL_REQUESTED"),
        "gameRoot": str(game_root),
        "exportRoot": str(output_dir.parent),
        "stage119": {
            "root": str(output_dir.parent / "stage119"),
            "fingerprint": TEST_DATASET_FINGERPRINT,
        },
        "blenderExe": None,
        "exportPackage": None,
    }


def write_export_package(
    package: Path,
    *,
    canonical_overrides: dict[str, object] | None = None,
    selection_overrides: dict[str, object] | None = None,
    manifest_overrides: dict[str, object] | None = None,
    manifest_selection_overrides: dict[str, object] | None = None,
    audit_overrides: dict[str, object] | None = None,
) -> None:
    package.mkdir()
    canonical = {
        "format": worker.CANONICAL_FORMAT,
        "job_id": "package-test",
        "map_id": "map01",
        "bounds": {"xmin": 0.0, "xmax": 128.0, "zmin": 0.0, "zmax": 128.0},
        "layers": {"terrain": True, "instances": True, "water": True, "effects": True},
        "export_groups": ["all"],
        "water_mode": "stable_eevee",
        "effects": {"selection_mode": "full_system_by_anchor"},
        "output": {"root": str(package.parent), "label": "package-test"},
    }
    canonical.update(canonical_overrides or {})
    selection = {
        "format": "EndfieldRegionSelectionExport/1",
        "mapId": "map01",
        "bounds": dict(canonical["bounds"]),
        "boundsSemantics": "half-open [min,max)",
        "sectorKeys": ["sector:0:0"],
        "effectsPolicy": "full_system_by_anchor",
        "waterMode": "stable_eevee",
        "blenderAxis": worker.AXIS_CONTRACT,
    }
    selection.update(selection_overrides or {})
    manifest_selection = dict(selection)
    manifest_selection.update(manifest_selection_overrides or {})
    manifest = {
        "format": "EndfieldWebUIRegionExport/1",
        "datasetFingerprint": TEST_DATASET_FINGERPRINT,
        "mapId": "map01",
        "selection": manifest_selection,
    }
    manifest.update(manifest_overrides or {})

    canonical_path = package / "canonical_request.json"
    selection_path = package / "selection.json"
    manifest_path = package / "export_manifest.json"
    worker.write_json(canonical_path, canonical)
    worker.write_json(selection_path, selection)
    worker.write_json(manifest_path, manifest)
    audit = {
        "status": "passed",
        "canonicalRequestSha256": worker.sha256_file(canonical_path),
        "manifestSha256": worker.sha256_file(manifest_path),
        "selectionSha256": worker.sha256_file(selection_path),
    }
    audit.update(audit_overrides or {})
    worker.write_json(package / "audit.json", audit)


class SectorDerivationTests(unittest.TestCase):
    def test_aligned_bounds_are_half_open(self) -> None:
        self.assertEqual(worker.sectors_for_bounds((0.0, 128.0, 0.0, 128.0)), [(0, 0)])
        self.assertEqual(worker.sectors_for_bounds((-128.0, 0.0, -128.0, 0.0)), [(-1, -1)])

    def test_partial_bounds_include_each_intersecting_sector_once(self) -> None:
        self.assertEqual(
            worker.sectors_for_bounds((-0.25, 128.0, -0.25, 128.0)),
            [(-1, -1), (0, -1), (-1, 0), (0, 0)],
        )


class LogicalPlanTests(unittest.TestCase):
    def test_per_sector_plan(self) -> None:
        bounds = (0.0, 256.0, 0.0, 128.0)
        units = worker.plan_logical_units(bounds, [(0, 0), (1, 0)], "per_sector")
        self.assertEqual([unit["sectorKeys"] for unit in units], [["sector:0:0"], ["sector:1:0"]])
        self.assertEqual(units[0]["bounds"], worker.format_bounds((0.0, 128.0, 0.0, 128.0)))
        self.assertEqual(units[1]["bounds"], worker.format_bounds((128.0, 256.0, 0.0, 128.0)))

    def test_cluster_4x4_uses_global_sector_grid(self) -> None:
        bounds = (384.0, 640.0, 0.0, 128.0)
        units = worker.plan_logical_units(bounds, [(3, 0), (4, 0)], "cluster_4x4_sectors")
        self.assertEqual([unit["tag"] for unit in units], ["cluster4_0_0", "cluster4_1_0"])
        self.assertEqual([unit["sectorKeys"] for unit in units], [["sector:3:0"], ["sector:4:0"]])

        negative = worker.plan_logical_units(
            (-512.0, 0.0, 0.0, 128.0),
            [(-4, 0), (-1, 0)],
            "cluster_4x4_sectors",
        )
        self.assertEqual(len(negative), 1)
        self.assertEqual(negative[0]["tag"], "cluster4_-1_0")

    def test_merged_selection_preserves_exact_half_open_bounds(self) -> None:
        bounds = (32.0, 224.0, 16.0, 112.0)
        units = worker.plan_logical_units(bounds, [(0, 0), (1, 0)], "merged_selection")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["bounds"], worker.format_bounds(bounds))
        self.assertEqual(units[0]["sectorKeys"], ["sector:0:0", "sector:1:0"])


class ProfilePartitionTests(unittest.TestCase):
    def test_cross_quadrant_partition_is_complete_and_non_overlapping(self) -> None:
        profiles = [
            {"profileId": "ne", "region": worker.format_bounds((0.0, 2048.0, 0.0, 2048.0))},
            {"profileId": "nw", "region": worker.format_bounds((-2048.0, 0.0, 0.0, 2048.0))},
            {"profileId": "se", "region": worker.format_bounds((0.0, 2048.0, -2048.0, 0.0))},
            {"profileId": "sw", "region": worker.format_bounds((-2048.0, 0.0, -2048.0, 0.0))},
        ]
        partitions = worker.partition_bounds((-128.0, 128.0, -128.0, 128.0), profiles)
        self.assertEqual(
            [(profile["profileId"], bounds) for profile, bounds in partitions],
            [
                ("ne", (0.0, 128.0, 0.0, 128.0)),
                ("nw", (-128.0, 0.0, 0.0, 128.0)),
                ("se", (0.0, 128.0, -128.0, 0.0)),
                ("sw", (-128.0, 0.0, -128.0, 0.0)),
            ],
        )

    def test_partition_rejects_uncovered_selection(self) -> None:
        profiles = [
            {"profileId": "east", "region": worker.format_bounds((0.0, 128.0, 0.0, 128.0))}
        ]
        with self.assertRaisesRegex(worker.WorkerError, "not fully covered"):
            worker.partition_bounds((-128.0, 128.0, 0.0, 128.0), profiles)


class ReopenFootprintExtraTests(unittest.TestCase):
    BOUNDS = (-640.0, -384.0, -512.0, -128.0)

    def valid_extra(self, *, name: str = "XTRA_floor022_677769277", entity_id: int = 677769277) -> FakeAuditObject:
        return FakeAuditObject(
            name,
            endfield_footprint_overlap_extra=True,
            entity_base="P_mod_map02_floor+1_022_02",
            entity_id=entity_id,
            origin_unity_xz=[-383.9700012207031, -426.8900146484375],
            footprint_unity_xz=[
                -390.8933985233307,
                -383.9700012207031,
                -434.1588909258644,
                -426.89001462184876,
            ],
        )

    def test_tagged_or_named_extra_is_instance_payload(self) -> None:
        self.assertTrue(reopen_audit.is_instance_payload(self.valid_extra()))
        self.assertTrue(reopen_audit.is_instance_payload(FakeAuditObject("XTRA_missing_tag")))
        self.assertFalse(reopen_audit.is_instance_payload(FakeAuditObject("ordinary_mesh")))

    def test_valid_boundary_extra_passes_all_explicit_assertions(self) -> None:
        rows: list[dict[str, object]] = []
        audit = reopen_audit.append_footprint_extra_assertions(
            rows,
            {"footprint_overlap_extra_count": 1},
            [self.valid_extra()],
            self.BOUNDS,
        )
        self.assertEqual(audit["extraCount"], 1)
        self.assertTrue(all(row["passed"] for row in rows), rows)
        self.assertEqual(
            [row["name"] for row in rows],
            [
                "footprint_extra_declared_count_matches",
                "footprint_extra_entity_keys_unique",
                "footprint_extra_origins_outside_half_open_unity_xz",
                "footprint_extra_footprints_finite_and_ordered",
                "footprint_extra_footprints_intersect_half_open_unity_xz",
            ],
        )

    def test_duplicate_inside_and_nonintersecting_extras_fail(self) -> None:
        duplicate = self.valid_extra(name="XTRA_duplicate")
        inside = FakeAuditObject(
            "XTRA_inside",
            endfield_footprint_overlap_extra=True,
            entity_base="P_inside",
            entity_id=2,
            origin_unity_xz=[-500.0, -300.0],
            footprint_unity_xz=[-510.0, -500.0, -310.0, -300.0],
        )
        nonintersecting = FakeAuditObject(
            "XTRA_nonintersecting",
            endfield_footprint_overlap_extra=True,
            entity_base="P_outside",
            entity_id=3,
            origin_unity_xz=[-300.0, -300.0],
            footprint_unity_xz=[-310.0, -300.0, -310.0, -300.0],
        )
        rows: list[dict[str, object]] = []
        audit = reopen_audit.append_footprint_extra_assertions(
            rows,
            {"footprint_overlap_extra_count": 3},
            [self.valid_extra(), duplicate, inside, nonintersecting],
            self.BOUNDS,
        )
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(audit["extraCount"], 4)
        self.assertFalse(by_name["footprint_extra_declared_count_matches"]["passed"])
        self.assertFalse(by_name["footprint_extra_entity_keys_unique"]["passed"])
        self.assertFalse(by_name["footprint_extra_origins_outside_half_open_unity_xz"]["passed"])
        self.assertTrue(by_name["footprint_extra_footprints_finite_and_ordered"]["passed"])
        self.assertFalse(by_name["footprint_extra_footprints_intersect_half_open_unity_xz"]["passed"])

    def test_nonfinite_and_reversed_footprints_fail_finite_assertion(self) -> None:
        nonfinite = FakeAuditObject(
            "XTRA_nonfinite",
            endfield_footprint_overlap_extra=True,
            entity_base="P_nonfinite",
            entity_id=4,
            origin_unity_xz=[-300.0, -300.0],
            footprint_unity_xz=[-390.0, float("nan"), -310.0, -300.0],
        )
        reversed_bounds = FakeAuditObject(
            "XTRA_reversed",
            endfield_footprint_overlap_extra=True,
            entity_base="P_reversed",
            entity_id=5,
            origin_unity_xz=[-300.0, -300.0],
            footprint_unity_xz=[-380.0, -390.0, -310.0, -300.0],
        )
        rows: list[dict[str, object]] = []
        reopen_audit.append_footprint_extra_assertions(
            rows,
            {"footprint_overlap_extra_count": 2},
            [nonfinite, reversed_bounds],
            self.BOUNDS,
        )
        by_name = {row["name"]: row for row in rows}
        self.assertFalse(by_name["footprint_extra_footprints_finite_and_ordered"]["passed"])
        self.assertFalse(by_name["footprint_extra_footprints_intersect_half_open_unity_xz"]["passed"])


class PlanRetentionTests(unittest.TestCase):
    def test_plan_retains_explicit_unmapped_sector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_root = root / "exports"
            export_root.mkdir()
            game_root = root / "game"
            (game_root / "Endfield_Data" / "StreamingAssets" / "VFS").mkdir(parents=True)
            output_dir = export_root / "new-output"
            job = normalized_job(
                map_id="map01",
                bounds=(0.0, 128.0, 0.0, 128.0),
                sector_keys=["sector:0:0", "sector:99:99"],
                output_dir=output_dir,
                game_root=game_root,
            )
            registry = {
                "stage119Fingerprint": TEST_DATASET_FINGERPRINT,
                "profiles": [
                    {
                        "profileId": "map01-test",
                        "mapId": "map01",
                        "status": "test",
                        "region": worker.format_bounds(worker.MAP_BOUNDS["map01"]),
                    }
                ]
            }
            with mock.patch.dict(os.environ, {"ENDFIELD_EXPORT_ROOT": str(export_root)}), mock.patch.object(
                worker,
                "validate_stage119",
                return_value={"fingerprint": TEST_DATASET_FINGERPRINT},
            ), mock.patch.object(worker, "verify_worker_scripts", return_value=[]):
                plan = worker.execute(job, registry, root / "unused-profile.json", plan_only=True)

        self.assertEqual(plan["mappedSectorKeys"], ["sector:0:0"])
        self.assertEqual(plan["retainedUnmappedSectorKeys"], ["sector:99:99"])
        self.assertEqual(plan["logicalUnits"][0]["sectorKeys"], ["sector:0:0"])

    def test_plan_only_rejects_category_filtered_export_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_root = root / "exports"
            export_root.mkdir()
            game_root = root / "game"
            (game_root / "Endfield_Data" / "StreamingAssets" / "VFS").mkdir(parents=True)
            job = normalized_job(
                map_id="map01",
                bounds=(0.0, 128.0, 0.0, 128.0),
                sector_keys=["sector:0:0"],
                output_dir=export_root / "new-output",
                game_root=game_root,
            )
            job["exportGroups"] = ["terrain"]
            with mock.patch.dict(os.environ, {"ENDFIELD_EXPORT_ROOT": str(export_root)}), mock.patch.object(
                worker,
                "validate_stage119",
                return_value={"fingerprint": TEST_DATASET_FINGERPRINT},
            ), mock.patch.object(worker, "verify_worker_scripts", return_value=[]):
                with self.assertRaisesRegex(worker.WorkerError, r"exportGroups must be \['all'\]"):
                    worker.execute(
                        job,
                        {"stage119Fingerprint": TEST_DATASET_FINGERPRINT},
                        root / "unused-profile.json",
                        plan_only=True,
                    )


class EventEmitterTests(unittest.TestCase):
    def test_non_terminal_progress_is_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events_path = Path(temporary) / "events.jsonl"
            emitter = worker.EventEmitter(events_path, "event-test")
            with mock.patch("builtins.print"):
                emitter.emit("preparing", "preflight", 0.40, "first")
                emitter.emit("building", "partition", 0.20, "must not regress")
                emitter.emit("auditing", "reopen", 0.75, "advance")
                emitter.emit("completed", "completed", 1.00, "done")
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([event["sequence"] for event in events], [1, 2, 3, 4])
        self.assertEqual([event["progress"] for event in events], [0.40, 0.40, 0.75, 1.00])
        self.assertEqual(emitter.progress, 1.00)


class ExportPackageValidationTests(unittest.TestCase):
    def test_valid_package_loads_verified_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package"
            write_export_package(package)
            raw = worker.load_job_from_package(package)
        self.assertEqual(raw["map_id"], "map01")
        self.assertEqual(raw["sectorKeys"], ["sector:0:0"])
        self.assertEqual(raw["datasetFingerprint"], TEST_DATASET_FINGERPRINT)

    def test_package_tampering_is_rejected(self) -> None:
        cases = [
            (
                "audit canonical request sha",
                {"audit_overrides": {"canonicalRequestSha256": "0" * 64}},
                "canonical request hash mismatch",
            ),
            (
                "audit manifest sha",
                {"audit_overrides": {"manifestSha256": "0" * 64}},
                "manifest hash mismatch",
            ),
            (
                "map",
                {"selection_overrides": {"mapId": "map02"}},
                "mapId mismatch",
            ),
            (
                "bounds",
                {
                    "selection_overrides": {
                        "bounds": {"xmin": 0.0, "xmax": 256.0, "zmin": 0.0, "zmax": 128.0}
                    }
                },
                "bounds mismatch",
            ),
            (
                "sector keys",
                {"manifest_selection_overrides": {"sectorKeys": ["sector:1:0"]}},
                "sectorKeys mismatch",
            ),
            (
                "effects policy",
                {"selection_overrides": {"effectsPolicy": "anchor_overlap"}},
                "effects policy mismatch",
            ),
        ]
        for label, options, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                package = Path(temporary) / "package"
                write_export_package(package, **options)
                with self.assertRaisesRegex(worker.WorkerError, expected):
                    worker.load_job_from_package(package)


class BlenderOverrideValidationTests(unittest.TestCase):
    def test_blender_override_must_match_profile_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blender = Path(temporary) / "blender.exe"
            blender.write_bytes(b"not-a-reviewed-blender")
            profile = {
                "profileId": "test-profile",
                "status": "test",
                "blenderExe": str(blender),
                "allowedBlenderSha256": ["0" * 64],
                "lockedInputs": [],
            }
            with self.assertRaisesRegex(worker.WorkerError, "not allowlisted"):
                worker.verify_profile(profile, str(blender))

    def test_blender_override_must_point_to_blender_exe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "other.exe"
            executable.write_bytes(b"reviewed-content")
            profile = {
                "profileId": "test-profile",
                "status": "test",
                "blenderExe": str(executable),
                "allowedBlenderSha256": [worker.sha256_file(executable)],
                "lockedInputs": [],
            }
            with self.assertRaisesRegex(worker.WorkerError, "must point to blender.exe"):
                worker.verify_profile(profile, str(executable))


class LayerStatusTests(unittest.TestCase):
    def test_map01_deferred_layer_statuses(self) -> None:
        status = worker.layer_status(
            {
                "mapId": "map01",
                "layers": all_layers(),
                "effects": {"selectionMode": "full_system_by_anchor"},
                "waterMode": "stable_eevee",
            }
        )
        self.assertEqual(status["effects"]["status"], "deferred_missing_blender_payload")
        self.assertEqual(status["effects"]["selectionPolicy"], "full_system_by_anchor")
        self.assertEqual(status["water"]["status"], "candidate_overlay_not_built")
        self.assertEqual(status["lights"]["status"], "not_started")
        self.assertEqual(status["roads"]["status"], "included_in_instances_unfiltered")

    def test_map02_and_category_only_deferred_statuses(self) -> None:
        layers = all_layers()
        layers["instances"] = False
        status = worker.layer_status(
            {
                "mapId": "map02",
                "layers": layers,
                "effects": {"selectionMode": "full_system_by_anchor"},
                "waterMode": "flowmap_fresnel",
            }
        )
        self.assertEqual(status["water"]["status"], "candidate_overlay_pending")
        self.assertEqual(status["water"]["coverageStatus"], "partial_reviewed_pcg_meshes")
        self.assertEqual(status["lights"]["status"], "partial_zero_records")
        self.assertEqual(status["roads"]["status"], "deferred_category_filter")
        self.assertEqual(status["vegetation"]["status"], "deferred_category_filter")


class WaterWorkflowTests(unittest.TestCase):
    def test_profile_command_exports_canonical_selection_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = {
                "mapId": "map02",
                "builder": str(root / "builder.py"),
                "workRoot": str(root / "work"),
                "assetsDb": str(root / "assets.sqlite"),
                "terrainSurface": str(root / "terrain"),
            }
            job = {
                "bounds": {"xmin": -640.0, "xmax": -384.0, "zmin": -512.0, "zmax": -128.0},
                "layers": {"terrain": True},
            }
            _command, environment = worker.profile_command(
                profile,
                root / "blender.exe",
                root / "output.blend",
                (-512.0, -384.0, -512.0, -128.0),
                job,
            )
            self.assertEqual(
                json.loads(environment["ENDFIELD_CANONICAL_SELECTION_BOUNDS"]),
                [-640.0, -384.0, -512.0, -128.0],
            )

    def test_water_step_count_respects_map_toggle_and_mode(self) -> None:
        base = {"mapId": "map02", "layers": {"water": False}, "waterMode": "stable_eevee"}
        self.assertEqual(worker.water_steps_for_job(base), 0)
        base["layers"]["water"] = True
        self.assertEqual(worker.water_steps_for_job(base), 1)
        base["waterMode"] = "flowmap_fresnel"
        self.assertEqual(worker.water_steps_for_job(base), 2)
        base["mapId"] = "map01"
        self.assertEqual(worker.water_steps_for_job(base), 0)

    def test_water_commands_use_profile_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "builder": str(root / "water_builder.py"),
                "modeSwitcher": str(root / "mode_switcher.py"),
                "sourceWaterBlend": str(root / "source.blend"),
                "objProbe": str(root / "probe.json"),
                "flowmapAtlas": str(root / "atlas.png"),
            }
            profile = {"profileId": "map02-test", "waterPayload": payload}
            blender = root / "blender.exe"
            import_command = worker.water_import_command(
                profile,
                blender,
                root / "input.blend",
                root / "output.blend",
                root / "audit.json",
                (-640.0, -384.0, -512.0, -128.0),
            )
            self.assertIn(str((root / "water_builder.py").resolve()), import_command)
            self.assertIn(str((root / "atlas.png").resolve()), import_command)
            self.assertEqual(import_command[import_command.index("--xmin") + 1], "-640")
            mode_command = worker.water_mode_command(
                profile,
                blender,
                root / "input.blend",
                root / "output.blend",
                root / "mode-audit.json",
                "flowmap_fresnel",
            )
            self.assertIn(str((root / "mode_switcher.py").resolve()), mode_command)
            self.assertEqual(mode_command[-1], "flowmap_fresnel")

    def test_water_locks_are_skipped_when_water_is_not_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blender = root / "blender.exe"
            blender.write_bytes(b"reviewed-blender")
            missing = root / "missing-water-input"
            profile = {
                "profileId": "map02-test",
                "mapId": "map02",
                "status": "test",
                "blenderExe": str(blender),
                "allowedBlenderSha256": [worker.sha256_file(blender)],
                "waterPayload": {
                    name: str(missing.with_name(f"{missing.name}-{name}"))
                    for name in ("builder", "modeSwitcher", "sourceWaterBlend", "objProbe", "flowmapAtlas")
                },
                "lockedInputs": [
                    {"role": "water_builder", "path": str(missing), "sha256": "0" * 64}
                ],
            }
            evidence = worker.verify_profile(profile, None, include_water=False)
            self.assertFalse(evidence["waterPayloadVerified"])
            self.assertEqual(evidence["lockedInputs"], [])

    def test_water_payload_must_be_fully_hash_locked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blender = root / "blender.exe"
            blender.write_bytes(b"reviewed-blender")
            payload = {}
            locks = []
            names = ("builder", "modeSwitcher", "sourceWaterBlend", "objProbe", "flowmapAtlas")
            for name in names:
                path = root / name
                path.write_bytes(name.encode("ascii"))
                payload[name] = str(path)
                if name != "flowmapAtlas":
                    locks.append({
                        "role": f"water_{name}",
                        "path": str(path),
                        "bytes": path.stat().st_size,
                        "sha256": worker.sha256_file(path),
                    })
            profile = {
                "profileId": "map02-test",
                "mapId": "map02",
                "status": "test",
                "blenderExe": str(blender),
                "allowedBlenderSha256": [worker.sha256_file(blender)],
                "waterPayload": payload,
                "lockedInputs": locks,
            }
            with self.assertRaisesRegex(worker.WorkerError, "water payload paths are not hash-locked"):
                worker.verify_profile(profile, None, include_water=True)


class OutputBoundaryTests(unittest.TestCase):
    def test_rejects_output_outside_export_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(worker.WorkerError, "must be inside"):
                worker.ensure_output_boundary(root / "outside", root / "game", root / "exports")

    def test_rejects_output_inside_game_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            export_root = Path(temporary)
            game_root = export_root / "game"
            with self.assertRaisesRegex(worker.WorkerError, "cannot be inside"):
                worker.ensure_output_boundary(game_root / "generated", game_root, export_root)

    def test_rejects_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            export_root = Path(temporary)
            game_root = export_root / "game"
            output = export_root / "existing"
            output.mkdir()
            with self.assertRaisesRegex(worker.WorkerError, "refusing to overwrite"):
                worker.ensure_output_boundary(output, game_root, export_root)


@unittest.skipUnless(
    REGISTRY_PATH.is_file(),
    "local reviewed Blender profile registry is intentionally not published",
)
class RegistryRevisionTests(unittest.TestCase):
    def test_map01_stage121_revision_is_the_only_active_owner(self) -> None:
        registry = worker.load_profiles(REGISTRY_PATH)
        active = worker.profiles_for_map(registry, "map01")
        self.assertEqual(
            [profile["profileId"] for profile in active],
            ["map01-full-stage642-stage121-footprintfix-v1"],
        )
        partitions = worker.partition_bounds(
            (-128.0, 128.0, -128.0, 128.0), active
        )
        self.assertEqual(len(partitions), 1)
        self.assertEqual(
            partitions[0][0]["profileId"],
            "map01-full-stage642-stage121-footprintfix-v1",
        )

    def test_historical_map01_profile_is_retained_verbatim(self) -> None:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        historical = registry.get("historicalProfiles") or []
        self.assertEqual(
            [profile["profileId"] for profile in historical],
            ["map01-full-stage642-v1"],
        )
        canonical = json.dumps(
            historical[0], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest().upper(),
            "2705BDFDFD9A1558232BEBBFAA7BAD8BBC9DC174863970FB911585E800DF1BC1",
        )

    def test_stage121_map01_builder_and_auditor_are_hash_locked(self) -> None:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        profile = next(
            value
            for value in registry["profiles"]
            if value["profileId"] == "map01-full-stage642-stage121-footprintfix-v1"
        )
        self.assertEqual(profile["supersedes"], "map01-full-stage642-v1")
        self.assertEqual(
            profile["builder"],
            r"F:\endfield extractor\blender_build_map01_chunk_stage121_footprintfix_v1.py",
        )
        builder_lock = next(
            item for item in profile["lockedInputs"] if item["role"] == "builder"
        )
        self.assertEqual(builder_lock["bytes"], 12920)
        self.assertEqual(
            builder_lock["sha256"],
            "3B1321C5B0E8A5BF30625EF771ADDDE625349E3F9751DAB779C708C943B1FE72",
        )
        auditor = next(
            item for item in registry["workerScripts"] if item["role"] == "auditor"
        )
        self.assertEqual(auditor["bytes"], 27899)
        self.assertEqual(
            auditor["sha256"],
            "5C8A17C57128349097C6CD0134FA656FA308602BEDED217F2595BEAFEE505449",
        )


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(cls.schema)
        cls.validator = jsonschema.Draft202012Validator(cls.schema)

    def test_worker_job_and_canonical_worker_extension_validate(self) -> None:
        direct = {
            "format": "EndfieldBlenderSelectionJob/1",
            "jobId": "direct-test",
            "mapId": "map01",
            "bounds": {"xmin": 0, "xmax": 128, "zmin": 0, "zmax": 128},
            "sectorKeys": ["sector:0:0", "sector:99:99"],
            "batchMode": "per_sector",
            "layers": {"terrain": True, "instances": True, "effects": True},
            "effects": {"selectionMode": "full_system_by_anchor"},
            "exportGroups": ["all"],
            "waterMode": "stable_eevee",
            "outputDir": "F:\\Endfield_Map_Exports\\worker-schema-test",
            "logDir": "F:\\Endfield_Map_Exports\\log\\worker-schema-test",
        }
        self.validator.validate(direct)

        canonical = {
            "format": "EndfieldCanonicalExportJob/2",
            "request_id": None,
            "job_id": "canonical-test",
            "map_id": "map02",
            "bounds": {"xmin": -128, "xmax": 128, "zmin": -128, "zmax": 128},
            "bounds_semantics": "half_open_xz",
            "selection_grid": 128.0,
            "preview": {"origin": "top_left", "v_direction": "z_increasing"},
            "baseline_policy": "map02_versioned_baseline",
            "water_binding_status": "map_local_rules",
            "source": None,
            "layers": {"terrain": True, "instances": True, "water": True, "effects": True},
            "export_groups": ["all"],
            "water_mode": "stable_eevee",
            "effects": {"selection_mode": "full_system_by_anchor"},
            "output": {"root": "F:\\Endfield_Map_Exports", "label": "canonical-test"},
            "stage119": {"fingerprint": TEST_DATASET_FINGERPRINT},
            "blender": {
                "sectorKeys": ["sector:-1:-1", "sector:0:0"],
                "batchMode": "cluster_4x4_sectors",
                "logDir": "F:\\Endfield_Map_Exports\\log\\canonical-test",
                "eventsPath": "F:\\Endfield_Map_Exports\\events.jsonl",
                "cancelMarker": "F:\\Endfield_Map_Exports\\CANCEL_REQUESTED",
                "blenderExe": "F:\\blender\\blender.exe",
            },
        }
        self.validator.validate(canonical)

    def test_effects_selection_policy_is_closed_when_requested(self) -> None:
        invalid = {
            "format": "EndfieldCanonicalExportJob/2",
            "job_id": "invalid-effects",
            "map_id": "map02",
            "bounds": {"xmin": 0, "xmax": 128, "zmin": 0, "zmax": 128},
            "layers": {"terrain": True, "instances": True, "water": False, "effects": True},
            "export_groups": ["effects"],
            "water_mode": "stable_eevee",
            "effects": {"selection_mode": "anchor_overlap"},
            "output": {"root": "F:\\Endfield_Map_Exports", "label": "invalid-effects"},
        }
        self.assertTrue(list(self.validator.iter_errors(invalid)))

    def test_runtime_log_path_constraints_are_documented(self) -> None:
        definitions = self.schema["$defs"]
        for name in ("logDirPath", "eventsPath", "cancelMarkerPath"):
            description = definitions[name]["description"]
            self.assertIn("ENDFIELD_EXPORT_ROOT\\log", description)
            self.assertIn("outside the game installation", description)


if __name__ == "__main__":
    unittest.main(verbosity=2)
