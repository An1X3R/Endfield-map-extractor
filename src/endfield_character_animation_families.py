"""Resolve animation search families from the selected prefab's source template."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from typing import TYPE_CHECKING

from endfield_character_cache import CharacterDataCache
from endfield_character_catalog import CharacterCatalog, read_character_catalog_cached

if TYPE_CHECKING:
    from endfield_character_sources import CharacterSourceContext


class CharacterAnimationFamilyError(ValueError):
    """Animation-family evidence is absent, ambiguous, or outside the source schema."""


_ANIMATION_REFERENCE = re.compile(
    r"^npc/animationconfig/humanoid/([a-z0-9_-]+)/([a-z0-9_-]+)$"
)


def _normalized_path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _reference_for_selection(catalog: CharacterCatalog, character_id: str,
                             resource_path: str) -> str:
    identities = tuple(row for row in catalog.characters if row.character_id == character_id)
    bindings = tuple(row for row in catalog.model_bindings if row.character_id == character_id)
    if len(identities) != 1 or len(bindings) != 1:
        raise CharacterAnimationFamilyError(
            f"Animation family requires one catalog character and model binding: character={character_id}, "
            f"identities={len(identities)}, bindings={len(bindings)}"
        )
    binding = bindings[0]
    if binding.status != "bound":
        raise CharacterAnimationFamilyError(
            f"Animation family has no prefab template: character={character_id}"
        )

    selected_references: set[str] = set()
    normalized_resource = _normalized_path(resource_path)
    matching_variants = 0
    for template in binding.templates:
        for part in template.parts:
            for variant in part.variants:
                if _normalized_path(variant.resource_path) != normalized_resource:
                    continue
                matching_variants += 1
                if variant.provider_status != "found":
                    continue
                fields = tuple(field for field in template.reference_fields
                               if field.source_field == "cpuAnimationTempletName")
                if len(fields) != 1 or fields[0].value is None:
                    raise CharacterAnimationFamilyError(
                        f"Selected prefab has no unique CPU animation template reference: "
                        f"character={character_id}, template={template.template_id}, path={resource_path}"
                    )
                selected_references.add(fields[0].value)
    if matching_variants == 0:
        raise CharacterAnimationFamilyError(
            f"Selected prefab path is absent from the character catalog: character={character_id}, path={resource_path}"
        )
    if not selected_references:
        raise CharacterAnimationFamilyError(
            f"Selected prefab path has no provider-backed catalog variant: character={character_id}, path={resource_path}"
        )
    if len(selected_references) != 1:
        raise CharacterAnimationFamilyError(
            f"Selected prefab maps to conflicting CPU animation templates: character={character_id}, "
            f"references={sorted(selected_references)}"
        )
    return next(iter(selected_references))


def _prefixes_from_reference(reference: str) -> tuple[str, str]:
    normalized = _normalized_path(reference).strip("/")
    match = _ANIMATION_REFERENCE.fullmatch(normalized)
    if match is None:
        raise CharacterAnimationFamilyError(
            f"Unsupported CPU animation template path: reference={reference!r}; "
            "expected NPC/AnimationConfig/Humanoid/<family>/<actor>"
        )
    family, actor = match.groups()
    actor_prefix = f"assets/beyond/arts/entity/actor/{family}/{actor}/animations/"
    common_prefix = f"assets/beyond/arts/entity/actor/{family}/common/animations/"
    return actor_prefix, common_prefix


def _has_animation_assets(resource_database: Path, prefix: str,
                          check_cancelled: Callable[[], None]) -> bool:
    check_cancelled()
    if not resource_database.is_file():
        raise FileNotFoundError(f"Character resource database is missing: path={resource_database}")
    upper_bound = prefix[:-1] + "0"
    uri = resource_database.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(paths)")}
            if not {"path", "path_folded"}.issubset(columns):
                raise CharacterAnimationFamilyError(
                    f"Character resource database has an unsupported paths schema: path={resource_database}, "
                    f"columns={sorted(columns)}"
                )
            row = connection.execute(
                "SELECT 1 FROM paths WHERE path_folded >= ? AND path_folded < ? "
                "AND substr(path_folded, 1, ?) = ? AND instr(path_folded, '##') = 0 "
                "AND (substr(path_folded, -4) = '.fbx' OR substr(path_folded, -5) = '.anim') LIMIT 1",
                (prefix, upper_bound, len(prefix), prefix),
            ).fetchone()
    except sqlite3.Error as error:
        raise CharacterAnimationFamilyError(
            f"Cannot query character resource database: path={resource_database}, prefix={prefix}, error={error}"
        ) from error
    check_cancelled()
    return row is not None


def resolve_animation_family_prefixes_from_catalog(
    catalog: CharacterCatalog,
    character_id: str,
    resource_path: str,
    resource_database: Path,
    check_cancelled: Callable[[], None],
) -> tuple[str, ...]:
    """Return source-backed actor and shared-family animation search prefixes."""
    if not character_id.strip() or not resource_path.strip():
        raise CharacterAnimationFamilyError("Animation family selection requires character and prefab identities")
    reference = _reference_for_selection(catalog, character_id, resource_path)
    candidates = _prefixes_from_reference(reference)
    return tuple(prefix for prefix in candidates
                 if _has_animation_assets(resource_database, prefix, check_cancelled))


def resolve_animation_family_prefixes(
    context: CharacterSourceContext,
    character_id: str,
    resource_path: str,
    check_cancelled: Callable[[], None],
) -> tuple[str, ...]:
    """Resolve prefixes through the caller-owned persistent catalog cache."""
    catalog = read_character_catalog_cached(
        context.game_root,
        context.provider_index,
        CharacterDataCache(context.cache_root),
        check_cancelled,
    )
    prefixes = resolve_animation_family_prefixes_from_catalog(
        catalog, character_id, resource_path, context.resource_database, check_cancelled,
    )
    if not prefixes:
        raise CharacterAnimationFamilyError(
            f"CPU animation template has no provider-indexed clips: character={character_id}, "
            f"path={resource_path}"
        )
    return prefixes
