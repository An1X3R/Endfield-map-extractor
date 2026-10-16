"""Cache exact EnemyData model components through the configured source reader."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
import os
import shutil
from pathlib import Path
import tempfile
from typing import Literal, cast

from endfield_character_bundle_inputs import prepare_providers_bundles
from endfield_character_cache import CharacterDataCache, _write_json_atomic
from endfield_character_catalog import (
    CharacterResourceProvider, _JSON_GROUP, _TABLE_GROUP, _provider_details,
    _normalize_container_path, _read_virtual_payload, _virtual_file_index,
)
from endfield_character_sources import CharacterSourceContext, validate_character_storage
from endfield_scene_export import run_scene_process
from extractor_core.resources import load_group
from extractor_core.scene_tables import read_scene_table


class EnemyModelIndexError(ValueError):
    """An enemy model cannot be tied to exact source bytes and reader output."""


@dataclass(frozen=True)
class EnemyModelBinding:
    template_id: str
    model_id: str
    resource_path: str
    source_cab: str
    path_id: int
    object_sha256: str


EnemyModelUnavailableReason = Literal["summary_missing", "provider_missing", "model_component_missing", "model_component_unsupported", "model_id_missing"]


@dataclass(frozen=True)
class EnemyModelUnavailable:
    template_id: str
    reason: EnemyModelUnavailableReason
    resource_path: str | None


@dataclass(frozen=True)
class EnemyModelIndex:
    format: str
    source_identity: str
    summary_sha256: str
    bindings: tuple[EnemyModelBinding, ...]
    unavailable: tuple[EnemyModelUnavailable, ...]


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise EnemyModelIndexError(f"Expected enemy source object: field={field}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise EnemyModelIndexError(f"Expected exact nonempty enemy source text: field={field}, value={value!r}")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise EnemyModelIndexError(f"Expected enemy source integer: field={field}, value={value!r}")
    return value


def _digest(value: object, field: str) -> str:
    digest = _text(value, field)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise EnemyModelIndexError(f"Invalid enemy source SHA256: field={field}, value={digest!r}")
    return digest


def _rows(value: object, field: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise EnemyModelIndexError(f"Expected enemy source array: field={field}")
    return tuple(_object(row, field) for row in value)


def enemy_model_binding(
    template_id: str,
    resource_path: str,
    source_cab: str,
    document: dict[str, object],
) -> EnemyModelBinding | EnemyModelUnavailable:
    """Read one scoped ModelComponentData; unrelated undecoded components stay unclaimed."""
    metadata = _object(document.get("$animestudio"), "EnemyData.$animestudio")
    if (metadata.get("type") != "MonoBehaviour"
            or _normalize_container_path(_text(metadata.get("container"), "EnemyData.container")) != resource_path
            or _text(metadata.get("sourceFile"), "EnemyData.sourceFile").casefold() != source_cab.casefold()):
        raise EnemyModelIndexError(f"EnemyData source identity disagrees with provider: template={template_id}, path={resource_path}, cab={source_cab}")
    byte_size = _integer(metadata.get("byteSize"), "EnemyData.byteSize")
    if byte_size <= 0 or byte_size != _integer(metadata.get("rawDataLength"), "EnemyData.rawDataLength"):
        raise EnemyModelIndexError(f"EnemyData object bytes are incomplete: template={template_id}")
    object_sha256 = _digest(metadata.get("rawDataSha256"), "EnemyData.rawDataSha256")
    references = _object(document.get("references"), "EnemyData.references")
    registry = _rows(references.get("RefIds"), "EnemyData.references.RefIds")
    root_rid = _integer(_object(document.get("data"), "EnemyData.data").get("rid"), "EnemyData.data.rid")
    roots = tuple(row for row in registry if row.get("rid") == root_rid)
    if len(roots) != 1 or _object(roots[0].get("type"), "EnemyData.root.type").get("class") != "EnemyTemplateData":
        raise EnemyModelIndexError(f"EnemyData root managed reference is not unique: template={template_id}, rid={root_rid}")
    components = tuple(row for row in registry
                       if _object(row.get("type"), "EnemyData.component.type").get("class") == "ModelComponentData"
                       and _object(row.get("type"), "EnemyData.component.type").get("ns") == "Beyond.Gameplay.View")
    if not components:
        return EnemyModelUnavailable(template_id, "model_component_missing", resource_path)
    if len(components) != 1:
        raise EnemyModelIndexError(f"EnemyData has multiple model components: template={template_id}, count={len(components)}")
    component = components[0]
    data = _object(component.get("data"), "EnemyData.ModelComponentData.data")
    if data.get("$decoded") is not True or any(marker in data for marker in ("$partial", "$inferred", "$undecoded")):
        return EnemyModelUnavailable(template_id, "model_component_unsupported", resource_path)
    offset = _integer(component.get("dataOffset"), "ModelComponentData.dataOffset")
    length = _integer(component.get("dataLength"), "ModelComponentData.dataLength")
    if (offset < 0 or length <= 0 or offset + length > byte_size
            or data.get("offset") != offset or data.get("length") != length
            or data.get("layout") != "Beyond.Gameplay.View.ModelComponentData"):
        raise EnemyModelIndexError(f"EnemyData model component has inconsistent byte/layout evidence: template={template_id}")
    model_id = data.get("modelId")
    if model_id == "":
        return EnemyModelUnavailable(template_id, "model_id_missing", resource_path)
    model_id = _text(model_id, "ModelComponentData.modelId")
    if any(character in model_id for character in ("/", "\\")) or model_id in (".", ".."):
        raise EnemyModelIndexError(f"Unsafe exact enemy model ID: template={template_id}, modelId={model_id!r}")
    return EnemyModelBinding(template_id, model_id, resource_path, source_cab,
                             _integer(metadata.get("pathId"), "EnemyData.pathId"), object_sha256)


def read_enemy_model_index(path: Path) -> EnemyModelIndex:
    """Validate a cached index before its identities enter catalog/export contracts."""
    raw = _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    if raw.get("format") != "EndfieldEnemyModelIndex/1":
        raise EnemyModelIndexError(f"Unsupported enemy model index: path={path}, format={raw.get('format')!r}")
    evidence = _object(raw.get("source_evidence"), "source_evidence")
    evidence_digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if evidence_digest != _digest(raw.get("source_identity"), "source_identity"):
        raise EnemyModelIndexError(f"Enemy model index source evidence digest changed: path={path}")
    bindings = tuple(EnemyModelBinding(
        _text(row.get("template_id"), "binding.template_id"), _text(row.get("model_id"), "binding.model_id"),
        _text(row.get("resource_path"), "binding.resource_path"), _text(row.get("source_cab"), "binding.source_cab"),
        _integer(row.get("path_id"), "binding.path_id"), _digest(row.get("object_sha256"), "binding.object_sha256"),
    ) for row in _rows(raw.get("bindings"), "bindings"))
    unavailable: list[EnemyModelUnavailable] = []
    for row in _rows(raw.get("unavailable"), "unavailable"):
        reason = row.get("reason")
        if reason not in ("summary_missing", "provider_missing", "model_component_missing", "model_component_unsupported", "model_id_missing"):
            raise EnemyModelIndexError(f"Unsupported enemy model availability reason: value={reason!r}")
        resource = row.get("resource_path")
        if resource is not None:
            resource = _text(resource, "unavailable.resource_path")
        unavailable.append(EnemyModelUnavailable(_text(row.get("template_id"), "unavailable.template_id"), cast(EnemyModelUnavailableReason, reason), resource))
    identities = tuple(binding.template_id for binding in bindings) + tuple(row.template_id for row in unavailable)
    if len(set(identities)) != len(identities):
        raise EnemyModelIndexError(f"Enemy model index contains duplicate template identities: path={path}")
    for binding in bindings:
        if (binding.path_id == 0 or binding.resource_path != _normalize_container_path(binding.resource_path)
                or not binding.resource_path.startswith("assets/") or ".." in binding.resource_path.split("/")
                or any(character in binding.model_id for character in ("/", "\\"))):
            raise EnemyModelIndexError(f"Enemy model index has invalid exact binding fields: template={binding.template_id}")
    if evidence.get("bindings") != [asdict(binding) for binding in bindings] or evidence.get("unavailable") != [asdict(row) for row in unavailable]:
        raise EnemyModelIndexError(f"Enemy model index bindings disagree with authenticated source evidence: path={path}")
    if raw.get("summary_sha256") != evidence.get("summary_sha256"):
        raise EnemyModelIndexError(f"Enemy model summary digest disagrees with authenticated source evidence: path={path}")
    return EnemyModelIndex("EndfieldEnemyModelIndex/1", _digest(raw.get("source_identity"), "source_identity"),
                           _digest(raw.get("summary_sha256"), "summary_sha256"), bindings, tuple(unavailable))


def prepare_enemy_model_index(
    context: CharacterSourceContext,
    json_reader: Path,
    unity_version: str,
    check_cancelled: Callable[[], None],
    progress: Callable[[str, float, str], None],
) -> Path:
    """Batch exact EnemyData providers once and fingerprint source/tool generations."""
    validate_character_storage(context.game_root, context.cache_root)
    if not json_reader.is_file() or unity_version != "2021.3.34f5":
        raise EnemyModelIndexError(f"Enemy model indexing requires the configured pinned reader: reader={json_reader}, unity_version={unity_version}")
    vfs_root = context.game_root / "Endfield_Data/StreamingAssets/VFS"
    source_directory = Path(__file__).parent
    helper_paths = tuple(source_directory / name for name in (
        "endfield_character_enemy_index.py", "endfield_character_bundle_inputs.py", "endfield_character_catalog.py",
        "endfield_character_cache.py", "endfield_character_sources.py", "endfield_scene_export.py",
        "extractor_core/resources.py", "extractor_core/scene_tables.py", "extractor_core/vfs.py"))
    source_paths = (context.game_root / "Endfield.exe", context.provider_index, context.resource_database, context.cab_inventory,
                    *sorted(vfs_root.glob("*/*.blc")), *sorted(vfs_root.glob("*/*.chk")),
                    *sorted(path for path in json_reader.parent.rglob("*") if path.is_file() and path.suffix.casefold() in (".exe", ".dll", ".json")), *helper_paths)
    cache = CharacterDataCache(context.cache_root)
    reader_hashes = tuple((str(path), cache.fingerprint_file(path, "enemy-reader/1", check_cancelled).sha256)
                          for path in source_paths if path.is_relative_to(json_reader.parent) or path in helper_paths)

    def build() -> dict[str, object]:
        table_group = load_group(vfs_root, _TABLE_GROUP)
        table_files = _virtual_file_index(table_group, _TABLE_GROUP)
        table_payload = _read_virtual_payload(vfs_root, _TABLE_GROUP, table_group, table_files, "Data/TableCfg/EnemyTemplateTable.bytes")
        templates = read_scene_table(table_payload, "EnemyTemplateTable")
        for key, value in templates.items():
            if _object(value, "EnemyTemplateTable").get("templateId") != key:
                raise EnemyModelIndexError(f"Enemy template key differs from source ID: key={key}")
        json_group = load_group(vfs_root, _JSON_GROUP)
        json_files = _virtual_file_index(json_group, _JSON_GROUP)
        summary_payload = _read_virtual_payload(vfs_root, _JSON_GROUP, json_group, json_files, "Data/Json/AIConfig/EnemyTemplateDataSummary.json")
        summary = _object(json.loads(summary_payload), "EnemyTemplateDataSummary")
        mapping = _object(summary.get("preloadEnemyTemplateId2Path"), "preloadEnemyTemplateId2Path")
        paths = {key: _normalize_container_path(_text(value, "EnemyTemplateDataSummary.path")) for key, value in mapping.items()}
        if any(key not in templates for key in paths):
            raise EnemyModelIndexError("Enemy summary contains undeclared EnemyTemplateTable IDs")
        providers, provider_source = _provider_details(context.provider_index, frozenset(paths.values()), check_cancelled)
        selected: dict[str, CharacterResourceProvider] = {}
        unavailable = [EnemyModelUnavailable(key, "summary_missing", None) for key in sorted(set(templates) - set(paths))]
        for template_id, resource in sorted(paths.items()):
            found = providers.get(resource, ())
            if not found:
                unavailable.append(EnemyModelUnavailable(template_id, "provider_missing", resource))
                continue
            if len(found) != 1 or len(found[0].serialized_cabs) != 1:
                raise EnemyModelIndexError(f"EnemyData must resolve to one provider and CAB: template={template_id}, path={resource}, providers={len(found)}")
            selected[template_id] = found[0]
        directory = context.cache_root / "enemy_model_sources"
        directory.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="generation-", dir=directory))
        temporary = work / "tmp"
        temporary.mkdir()
        environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
        source_objects: dict[tuple[str, str], list[dict[str, object]]] = {}
        if selected:
            progress("preparing", .1, "Reading exact enemy model sources")
            bundles = prepare_providers_bundles(context, tuple(selected.values()), work / "bundles", check_cancelled, progress)
            run_scene_process([str(json_reader), str(work / "bundles"), str(work / "source_objects"),
                               "--game", "ArknightsEndfield", "--unity_version", unity_version,
                               "--types", "AssetBundle:Parse", "ResourceManager:Parse", "MonoBehaviour",
                               "--export_type", "JSON", "--mono_behaviour_type_tree_priority", "SerializedFirst",
                               "--group_assets", "ByType"], work / "reader.log", environment, check_cancelled)
            for path in sorted((work / "source_objects").rglob("*.json")):
                check_cancelled()
                document = _object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
                metadata = document.get("$animestudio")
                if not isinstance(metadata, dict) or metadata.get("type") != "MonoBehaviour":
                    continue
                resource = metadata.get("container")
                if not isinstance(resource, str) or _normalize_container_path(resource) not in paths.values():
                    continue
                key = (_normalize_container_path(resource), _text(metadata.get("sourceFile"), "sourceFile").casefold())
                source_objects.setdefault(key, []).append(document)
        else:
            bundles = ()
        bindings: list[EnemyModelBinding] = []
        for template_id, provider in sorted(selected.items()):
            resource = paths[template_id]
            cab = provider.serialized_cabs[0].name
            objects = source_objects.get((resource, cab.casefold()), [])
            if len(objects) != 1:
                raise EnemyModelIndexError(f"Configured reader did not produce one exact EnemyData object: template={template_id}, path={resource}, cab={cab}, matches={len(objects)}, log={work / 'reader.log'}")
            result = enemy_model_binding(template_id, resource, cab, objects[0])
            if isinstance(result, EnemyModelBinding):
                bindings.append(result)
            else:
                unavailable.append(result)
        identity = {"summary_sha256": hashlib.sha256(summary_payload).hexdigest(),
                    "templates_sha256": hashlib.sha256(table_payload).hexdigest(), "provider_sha256": provider_source.sha256,
                    "reader_sources": reader_hashes, "unity_version": unity_version,
                    "bundles": [{"logical_name": bundle.logical_name, "bytes": bundle.bytes, "sha256": bundle.sha256} for bundle in bundles],
                    "bindings": [asdict(binding) for binding in bindings], "unavailable": [asdict(row) for row in unavailable]}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result = {**asdict(EnemyModelIndex("EndfieldEnemyModelIndex/1", digest, identity["summary_sha256"], tuple(bindings), tuple(unavailable))),
                  "source_evidence": identity, "scope": "Only the exact ModelComponentData.modelId is consumed; unrelated undecoded EnemyTemplateData fields remain unclaimed."}
        resolved_work = work.resolve(strict=True)
        if resolved_work.parent != directory.resolve(strict=True) or not resolved_work.name.startswith("generation-") or work.is_symlink():
            raise EnemyModelIndexError(f"Enemy index temporary directory escaped its owned cache: path={work}")
        reader_log = resolved_work / "reader.log"
        if reader_log.is_file():
            shutil.copyfile(reader_log, directory / ("reader-" + digest + ".log"))
        shutil.rmtree(resolved_work)
        return result

    raw, _hit = cache.get_or_build_json("enemy_model_index", "enemy-model-index/1", source_paths,
                                      (unity_version, hashlib.sha256(json.dumps(reader_hashes).encode()).hexdigest()), build, check_cancelled)
    value = _object(raw, "enemy model index cache")
    identity = _digest(value.get("source_identity"), "source_identity")
    path = context.cache_root / "enemy_models" / (identity + ".json")
    if path.is_file():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise EnemyModelIndexError(f"Enemy model index differs from its fingerprinted cache: path={path}")
    else:
        _write_json_atomic(path, value, "enemy model source index")
    read_enemy_model_index(path)
    return path


def enemy_index_from_catalog(catalog: dict[str, object], cache_root: Path) -> Path | None:
    """Resolve an explicit catalog index receipt for a CLI without reader configuration."""
    source_values = catalog.get("sources")
    if isinstance(source_values, tuple):
        source_values = list(source_values)
    sources = _rows(source_values, "catalog.sources")
    indexes = tuple(row for row in sources if row.get("source_name") == "EnemyModelIndex")
    if not indexes:
        return None
    if len(indexes) != 1:
        raise EnemyModelIndexError("Catalog has multiple enemy model indexes")
    source = indexes[0]
    virtual_path = _text(source.get("virtual_path"), "EnemyModelIndex.virtual_path")
    parts = virtual_path.split("/")
    if len(parts) != 2 or parts[0] != "EnemyModelIndex" or not parts[1].endswith(".json"):
        raise EnemyModelIndexError(f"Invalid catalog enemy index receipt path: value={virtual_path!r}")
    _digest(parts[1].removesuffix(".json"), "EnemyModelIndex.filename")
    path = cache_root / "enemy_models" / parts[1]
    if not path.is_file():
        raise EnemyModelIndexError(f"This catalog requires its exact enemy model index in the configured cache; prepare enemy sources first: path={path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != _digest(source.get("sha256"), "EnemyModelIndex.sha256"):
        raise EnemyModelIndexError(f"Catalog enemy model index bytes changed: path={path}")
    read_enemy_model_index(path)
    return path


def prepare_enemy_model_index_from_paths(
    game_root: Path,
    provider_index: Path,
    resource_database: Path,
    cab_inventory: Path,
    cache_root: Path,
    json_reader: Path,
    unity_version: str,
    check_cancelled: Callable[[], None],
) -> Path:
    """Adapt source-only CLI requests; Blender/output fields are unused by this reader."""
    context = CharacterSourceContext(game_root, Path(), cache_root, cache_root,
                                     provider_index, resource_database, cab_inventory)
    return prepare_enemy_model_index(context, json_reader, unity_version, check_cancelled,
                                     lambda _stage, _fraction, _message: None)
