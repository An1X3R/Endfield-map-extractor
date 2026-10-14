from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from endfield_character_animation_families import (
    CharacterAnimationFamilyError,
    resolve_animation_family_prefixes_from_catalog,
)
from endfield_character_catalog import (
    CharacterCatalog,
    CharacterIdentity,
    CharacterModelBinding,
    CharacterModelSummary,
    CharacterPrefabInfoSource,
    CharacterPrefabPart,
    CharacterPrefabReferenceField,
    CharacterPrefabTemplate,
    CharacterPrefabVariant,
    CharacterProviderIndexSource,
    CharacterSerializedCab,
    CharacterResourceProvider,
)


def _never_cancelled() -> None:
    return None


def _catalog(reference: str) -> CharacterCatalog:
    resource_path = "assets/beyond/dynamicassets/gameplay/actors/postmodels/characters/chr_0018_dapan_postmodel.prefab"
    provider = CharacterResourceProvider("characters/dapan.ab", (CharacterSerializedCab("dapan.cab", ()),))
    part = CharacterPrefabPart(
        "chr_0018_dapan_postmodel",
        (
            CharacterPrefabVariant("characters", resource_path, "found", (provider,)),
            CharacterPrefabVariant("npc", resource_path.replace("/characters/", "/npc/"), "found", (provider,)),
        ),
    )
    template = CharacterPrefabTemplate(
        "npc_0001_dapan",
        "chr_0018_dapan",
        CharacterPrefabInfoSource("Data/Json/NPC/PrefabInfo/npc_0001_dapan.json", "a" * 64, 1),
        (part.part_name,),
        (CharacterPrefabReferenceField("cpuAnimationTempletName", reference),),
        (part,),
        "not_verified",
    )
    binding = CharacterModelBinding("chr_0018_dapan", "bound", (template,), "not_verified")
    return CharacterCatalog(
        "EndfieldCharacterCatalog/2",
        "b" * 64,
        (CharacterIdentity("chr_0018_dapan", "大潘", "Dapan", "18"),),
        (),
        CharacterProviderIndexSource("providers.jsonl", "c" * 64, 1),
        (binding,),
        CharacterModelSummary(1, 0, 1, 1, 2, 2, "not_performed"),
    )


def _database(path: Path, resource_paths: tuple[str, ...]) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE paths (path_index INTEGER PRIMARY KEY, path TEXT NOT NULL, "
            "path_folded TEXT NOT NULL, opaque_record BLOB NOT NULL, string_offset INTEGER NOT NULL)"
        )
        connection.execute("CREATE INDEX paths_folded_idx ON paths(path_folded)")
        connection.executemany(
            "INSERT INTO paths(path, path_folded, opaque_record, string_offset) VALUES (?, ?, X'', 0)",
            ((value, value.casefold()) for value in resource_paths),
        )
        connection.commit()


class CharacterAnimationFamilyTests(unittest.TestCase):
    def test_template_reference_resolves_actor_and_existing_common_families(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "resource_index.sqlite"
            actor = "assets/beyond/arts/entity/actor/panda/dapan/animations/battle/attack.fbx"
            common = "assets/beyond/arts/entity/actor/panda/common/animations/battle/a_actor_panda_battle_stun_end.fbx"
            unrelated = "assets/beyond/arts/entity/actor/girl/pelica/animations/battle/attack.fbx"
            _database(database, (actor, common, unrelated))

            prefixes = resolve_animation_family_prefixes_from_catalog(
                _catalog("NPC/AnimationConfig/Humanoid/Panda/dapan"),
                "chr_0018_dapan",
                "ASSETS\\BEYOND\\DYNAMICASSETS\\GAMEPLAY\\ACTORS\\POSTMODELS\\CHARACTERS\\CHR_0018_DAPAN_POSTMODEL.PREFAB",
                database,
                _never_cancelled,
            )

        self.assertEqual(prefixes, (
            "assets/beyond/arts/entity/actor/panda/dapan/animations/",
            "assets/beyond/arts/entity/actor/panda/common/animations/",
        ))

    def test_missing_shared_family_is_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "resource_index.sqlite"
            actor = "assets/beyond/arts/entity/actor/panda/dapan/animations/battle/attack.anim"
            _database(database, (actor,))
            prefixes = resolve_animation_family_prefixes_from_catalog(
                _catalog("NPC/AnimationConfig/Humanoid/Panda/dapan"),
                "chr_0018_dapan",
                "assets/beyond/dynamicassets/gameplay/actors/postmodels/characters/chr_0018_dapan_postmodel.prefab",
                database,
                _never_cancelled,
            )
        self.assertEqual(prefixes, ("assets/beyond/arts/entity/actor/panda/dapan/animations/",))

    def test_unrecognized_template_namespace_fails_without_id_heuristic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "resource_index.sqlite"
            _database(database, ())
            with self.assertRaisesRegex(CharacterAnimationFamilyError, "Unsupported CPU animation template"):
                resolve_animation_family_prefixes_from_catalog(
                    _catalog("NPC/Other/Panda/dapan"),
                    "chr_0018_dapan",
                    "assets/beyond/dynamicassets/gameplay/actors/postmodels/characters/chr_0018_dapan_postmodel.prefab",
                    database,
                    _never_cancelled,
                )

    def test_prefab_must_match_catalog_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "resource_index.sqlite"
            _database(database, ())
            with self.assertRaisesRegex(CharacterAnimationFamilyError, "absent from the character catalog"):
                resolve_animation_family_prefixes_from_catalog(
                    _catalog("NPC/AnimationConfig/Humanoid/Panda/dapan"),
                    "chr_0018_dapan",
                    "assets/other.prefab",
                    database,
                    _never_cancelled,
                )


if __name__ == "__main__":
    unittest.main()
