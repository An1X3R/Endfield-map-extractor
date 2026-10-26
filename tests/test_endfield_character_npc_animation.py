from dataclasses import replace
from pathlib import Path
import zlib

import unittest

from endfield_character_npc_animation import (
    NpcAnimationBindingError, NpcAnimationPointer, bind_npc_cpu_actions,
    npc_avatar_source_transforms, npc_cpu_animation_resource_path,
    validate_npc_generic_target, validate_npc_humanoid_target,
)
from endfield_character_npc_assembly import NpcSourceObject


def cpu_source(template: str) -> NpcSourceObject:
    return NpcSourceObject(Path("source.json"), {
        "configTag": {"tagId": zlib.crc32(template.encode())},
        "avatar": {"m_FileID": 1, "m_PathID": 17},
        "allBaseClips": [{"stateName": "IdleLoop", "clip": {"m_FileID": 2, "m_PathID": -31}}],
    }, {"type": "MonoBehaviour", "container": npc_cpu_animation_resource_path(template),
        "pptrReferences": [
            {"path": "$.avatar", "fileId": 1, "pathId": 17, "expectedTargetSourceFile": "CAB-avatar"},
            {"path": "$.allBaseClips[0].clip", "fileId": 2, "pathId": -31, "expectedTargetSourceFile": "CAB-clip"},
        ]})


def avatar_source() -> NpcSourceObject:
    pose = {"t": {"X": 0., "Y": 0., "Z": 0.}, "q": {"X": 0., "Y": 0., "Z": 0., "W": 1.},
            "s": {"X": 1., "Y": 1., "Z": 1.}}
    return NpcSourceObject(Path("avatar.json"), {"m_TOS": {"3": "Root"}, "m_Avatar": {
        "m_AvatarSkeleton": {"m_ID": [0, 3], "m_Node": [{"m_ParentId": -1}, {"m_ParentId": 0}]},
        "m_DefaultPose": {"m_X": [pose, pose]},
    }}, {"type": "Avatar"})


def generic_avatar_source(target: bool, driven_pose_delta: float) -> NpcSourceObject:
    pose = {"t": {"X": 0., "Y": 0., "Z": 0.}, "q": {"X": 0., "Y": 0., "Z": 0., "W": 1.},
            "s": {"X": 1., "Y": 1., "Z": 1.}}
    paths = ("", "Mesh", "Root/Bone", "Root") if target else ("", "Root", "Root/Bone", "Mesh")
    ids = (0, 13, 12, 11) if target else (0, 11, 12, 13)
    parents = (-1, 0, 3, 0) if target else (-1, 0, 1, 0)
    default_poses: list[dict[str, object]] = []
    skeleton_poses: list[dict[str, object]] = []
    for path in paths:
        default = {"t": dict(pose["t"]), "q": dict(pose["q"]), "s": dict(pose["s"])}
        skeleton_pose = {"t": dict(pose["t"]), "q": dict(pose["q"]), "s": dict(pose["s"])}
        if path == "Mesh":
            default["t"]["X"] = 0.25
            skeleton_pose["t"]["X"] = 0.5
        if path == "Root/Bone" and driven_pose_delta:
            default["t"]["X"] = driven_pose_delta
        if target and path in ("Root", "Root/Bone"):
            default["q"] = {axis: -value for axis, value in default["q"].items()}
            skeleton_pose["q"] = {axis: -value for axis, value in skeleton_pose["q"].items()}
        default_poses.append(default)
        skeleton_poses.append(skeleton_pose)
    return NpcSourceObject(Path("avatar.json"), {
        "m_TOS": {str(path_id): path for path_id, path in zip(ids, paths, strict=True)},
        "m_Avatar": {
            "m_AvatarSkeleton": {
                "m_ID": list(ids),
                "m_Node": [{"m_ParentId": parent} for parent in parents],
            },
            "m_DefaultPose": {"m_X": default_poses},
            "m_AvatarSkeletonPose": {"m_X": skeleton_poses},
        },
    }, {"type": "Avatar", "rawDataSha256": "a" * 64 if not target else "b" * 64})


def generic_clip(paths: tuple[int, ...]) -> NpcSourceObject:
    return NpcSourceObject(Path("clip.json"), {
        "m_ClipBindingConstant": {"genericBindings": [
            {"typeID": "Transform", "path": path} for path in paths]},
    }, {"type": "AnimationClip", "rawDataSha256": "c" * 64})


class NpcAnimationBindingTests(unittest.TestCase):
    def test_target_human_mapping_requires_every_present_source_slot(self) -> None:
        def human(indices: list[int]) -> NpcSourceObject:
            return NpcSourceObject(Path("avatar.json"), {"m_Avatar": {"m_Human": {
                "m_Skeleton": {"m_Node": [{"m_ParentId": -1}, {"m_ParentId": 0}]},
                "m_SkeletonPose": {"m_X": [{}, {}]}, "m_HumanBoneIndex": indices}}}, {"type": "Avatar"})

        source = human([0, 1, *([-1] * 23)])
        validate_npc_humanoid_target(source, human([1, 0, *([-1] * 23)]))
        for indices in ([0, -1, *([-1] * 23)], [0, 2, *([-1] * 23)], [0]):
            with self.subTest(indices=indices), self.assertRaises(NpcAnimationBindingError):
                validate_npc_humanoid_target(source, human(indices))

    def test_exact_cpu_template_avatar_and_clip(self) -> None:
        template = "NPC/AnimationConfig/Humanoid/Strong/strong"
        binding = bind_npc_cpu_actions(template, cpu_source(template), NpcAnimationPointer("cab-AVATAR", 17))
        self.assertEqual(binding.template_namespace, "Humanoid")
        self.assertEqual(binding.actions[0].state_name, "IdleLoop")
        self.assertEqual(binding.actions[0].pointer, NpcAnimationPointer("CAB-clip", -31))

    def test_generic_cpu_template_uses_its_own_resource_namespace_and_source_binding(self) -> None:
        template = "NPC/AnimationConfig/Generic/domainshop"
        self.assertEqual(
            npc_cpu_animation_resource_path(template),
            "assets/beyond/dynamicassets/gameplay/npc/animation/generic/data_npc_cpuanimdata_domainshop.asset",
        )
        binding = bind_npc_cpu_actions(template, cpu_source(template), NpcAnimationPointer("CAB-avatar", 17))
        self.assertEqual(binding.template_namespace, "Generic")
        self.assertEqual(binding.actions[0].state_name, "IdleLoop")
        self.assertEqual(binding.actions[0].pointer, NpcAnimationPointer("CAB-clip", -31))

    def test_family_alone_does_not_establish_binding(self) -> None:
        template = "NPC/AnimationConfig/Humanoid/Strong/strong"
        with self.assertRaisesRegex(NpcAnimationBindingError, "cross-Avatar"):
            bind_npc_cpu_actions(template, cpu_source(template), NpcAnimationPointer("CAB-other", 17))
        source = cpu_source(template)
        with self.assertRaisesRegex(NpcAnimationBindingError, "GameplayTag"):
            bind_npc_cpu_actions(template, replace(source, value={**source.value, "configTag": {"tagId": 0}}),
                                 NpcAnimationPointer("CAB-avatar", 17))

    def test_missing_and_ambiguous_clip_pointer_fail(self) -> None:
        template = "NPC/AnimationConfig/Humanoid/Strong/strong"
        source = cpu_source(template)
        references = source.metadata["pptrReferences"]
        for invalid in (references[:1], [*references, references[1]]):
            with self.assertRaisesRegex(NpcAnimationBindingError, "ambiguous"):
                bind_npc_cpu_actions(template, replace(source, metadata={**source.metadata, "pptrReferences": invalid}),
                                     NpcAnimationPointer("CAB-avatar", 17))

    def test_generic_path_must_match_the_exact_cpu_template_shape(self) -> None:
        with self.assertRaisesRegex(NpcAnimationBindingError, "Generic/<actor>"):
            npc_cpu_animation_resource_path("NPC/AnimationConfig/Generic/rabbit01/extra")

    def test_avatar_hierarchy_preserves_source_parent_locals(self) -> None:
        rows = npc_avatar_source_transforms(avatar_source(), "NPC_Root")
        self.assertEqual([row["path"] for row in rows], ["NPC_Root", "NPC_Root/Root"])
        self.assertEqual(rows[1]["rotation"], [0., 0., 0., 1.])
        source = avatar_source()
        invalid = replace(source, value={**source.value, "m_TOS": {"3": "Missing/Root"}})
        with self.assertRaisesRegex(NpcAnimationBindingError, "parent"):
            npc_avatar_source_transforms(invalid, "NPC_Root")

    def test_generic_avatar_equivalence_matches_paths_not_array_indices(self) -> None:
        proof = validate_npc_generic_target(generic_avatar_source(False, 0.), generic_avatar_source(True, 0.),
                                            (generic_clip((11, 12)),))
        self.assertEqual(proof.clip_count, 1)
        self.assertEqual(proof.transform_binding_count, 2)
        self.assertEqual(proof.driven_path_count, 2)
        self.assertEqual(proof.validated_path_count, 3)
        self.assertEqual(proof.max_default_pose_delta, 0.)
        self.assertEqual(proof.to_document()["format"], "NpcGenericAvatarEquivalence/1")

    def test_generic_avatar_equivalence_rejects_driven_pose_delta(self) -> None:
        with self.assertRaisesRegex(NpcAnimationBindingError, "driven path or ancestor"):
            validate_npc_generic_target(generic_avatar_source(False, 0.), generic_avatar_source(True, 2e-6),
                                        (generic_clip((12,)),))

    def test_generic_target_accessory_does_not_change_driven_binding(self) -> None:
        target = generic_avatar_source(True, 0.)
        avatar = target.value['m_Avatar']
        skeleton = avatar['m_AvatarSkeleton']
        value = {**target.value, 'm_TOS': {**target.value['m_TOS'], '15': 'Root/Bone/Accessory'},
            'm_Avatar': {**avatar,
                'm_AvatarSkeleton': {'m_ID': [*skeleton['m_ID'], 15],
                    'm_Node': [*skeleton['m_Node'], {'m_ParentId': 2}]},
                'm_DefaultPose': {'m_X': [*avatar['m_DefaultPose']['m_X'], avatar['m_DefaultPose']['m_X'][0]]},
                'm_AvatarSkeletonPose': {'m_X': [*avatar['m_AvatarSkeletonPose']['m_X'], avatar['m_AvatarSkeletonPose']['m_X'][0]]}}}
        expanded = replace(target, value=value)
        proof = validate_npc_generic_target(generic_avatar_source(False, 0.), expanded, (generic_clip((11, 12)),))
        self.assertEqual(proof.target_only_path_count, 1)
        self.assertEqual(proof.validated_path_count, 3)
        self.assertEqual(proof.max_default_pose_delta, 0.)
        with self.assertRaisesRegex(NpcAnimationBindingError, 'lacks source transform paths'):
            validate_npc_generic_target(expanded, generic_avatar_source(False, 0.), (generic_clip((11, 12)),))

    def test_generic_avatar_equivalence_rejects_missing_transform_path_hash(self) -> None:
        target = generic_avatar_source(True, 0.)
        value = {**target.value,
            "m_TOS": {"0": "", "13": "Mesh", "12": "Root/Bone", "14": "Root"},
            "m_Avatar": {**target.value["m_Avatar"],
                "m_AvatarSkeleton": {"m_ID": [0, 13, 12, 14],
                    "m_Node": [{"m_ParentId": parent} for parent in (-1, 0, 3, 0)]}}}
        with self.assertRaisesRegex(NpcAnimationBindingError, "one exact path in both Avatars"):
            validate_npc_generic_target(generic_avatar_source(False, 0.), replace(target, value=value),
                                        (generic_clip((11,)),))

    def test_generic_avatar_equivalence_requires_transform_binding_per_clip(self) -> None:
        empty_clip = generic_clip(())
        with self.assertRaisesRegex(NpcAnimationBindingError, "no Transform path bindings"):
            validate_npc_generic_target(generic_avatar_source(False, 0.), generic_avatar_source(True, 0.),
                                        (empty_clip,))


if __name__ == "__main__":
    unittest.main()
