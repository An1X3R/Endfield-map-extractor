from dataclasses import replace
from pathlib import Path
import zlib

import unittest

from endfield_character_npc_animation import (
    NpcAnimationBindingError, NpcAnimationPointer, bind_npc_cpu_actions,
    npc_avatar_source_transforms, npc_cpu_animation_resource_path,
    validate_npc_humanoid_target,
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


if __name__ == "__main__":
    unittest.main()
