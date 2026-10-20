import { isJsonObject, requestJson } from '../../lib/backendClient'
import { assertMaterialAcknowledgement, WEATHER_EFFECTS } from './materials'
import { assertAppearanceAcknowledgement } from './appearance'
import type {
  CharacterAnimation,
  CharacterAnimationCatalog,
  CharacterAnimationCoverage,
  CharacterActionAnimationSelection,
  CharacterCancelAcknowledgement,
  CharacterCapabilities,
  CharacterAssetCategory,
  CharacterCatalog,
  CharacterCpuActionReference,
  CharacterCpuActionReferences,
  CharacterDeliveryFile,
  CharacterDetail,
  CharacterExportOptions,
  CharacterExportRequest,
  CharacterAppearanceOptions,
  CharacterAppearanceReport,
  CharacterMaterialOptions,
  CharacterWeatherEffect,
  CharacterJob,
  CharacterJobError,
  CharacterJobEvent,
  CharacterJobEventsResponse,
  CharacterJobKind,
  CharacterJobPhase,
  CharacterJobResult,
  CharacterJobState,
  CharacterNameSource,
  CharacterPreparationRequest,
  CharacterPublicRequest,
  CharacterReplay,
  CharacterRequestValidation,
  CharacterSummary,
  CharacterUnsupportedAction,
  CharacterValidationIssue,
  CharacterVariant,
} from './types'

const JOB_STATES: readonly CharacterJobState[] = ['queued', 'running', 'completed', 'partial', 'failed', 'cancelled']
const JOB_PHASES: readonly CharacterJobPhase[] = [
  'queued', 'preparing', 'extracting', 'building', 'auditing', 'completed', 'partial', 'failed', 'cancelled',
]
const DELIVERY_ROLES: readonly CharacterDeliveryFile['role'][] = [
  'character', 'animation', 'expressions', 'texture', 'preview', 'report',
]
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SHA256 = /^[0-9a-f]{64}$/
const SAFE_LABEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/
const JOBS_PATH = '/api/v2/character-jobs'
const PREPARATION_PATH = '/api/v2/character-preparation'
const CHARACTERS_PATH = '/api/v2/characters'

const invalid = (field: string, expected: string): never => {
  throw new Error(`${field}: expected ${expected}`)
}

const object = (value: unknown, field: string): Record<string, unknown> =>
  isJsonObject(value) ? value : invalid(field, 'an object')

const text = (value: unknown, field: string): string =>
  typeof value === 'string' && value.trim().length > 0 ? value : invalid(field, 'non-empty text')

const nullableText = (value: unknown, field: string): string | null =>
  value === null ? null : text(value, field)

const bool = (value: unknown, field: string): boolean =>
  typeof value === 'boolean' ? value : invalid(field, 'a boolean')

const integer = (value: unknown, field: string, minimum: number): number =>
  Number.isInteger(value) && (value as number) >= minimum ? (value as number) : invalid(field, `an integer >= ${minimum}`)

const finiteNumber = (value: unknown, field: string, minimum: number, maximum: number): number =>
  typeof value === 'number' && Number.isFinite(value) && value >= minimum && value <= maximum
    ? value
    : invalid(field, `a finite number from ${minimum} to ${maximum}`)

const array = <T>(value: unknown, field: string, parseItem: (item: unknown, itemField: string) => T): T[] => {
  if (!Array.isArray(value)) return invalid(field, 'an array')
  return value.map((item, index) => parseItem(item, `${field}[${index}]`))
}

const stringArray = (value: unknown, field: string): string[] =>
  array(value, field, (item, itemField) => typeof item === 'string' ? item : invalid(itemField, 'text'))

const parseUuid = (value: unknown, field: string): string => {
  const result = text(value, field)
  return UUID.test(result) ? result : invalid(field, 'a canonical lowercase UUID')
}

const parseSha256 = (value: unknown, field: string): string => {
  const result = text(value, field)
  return SHA256.test(result) ? result : invalid(field, 'a lowercase SHA-256 identity')
}

const parseCapabilities = (value: unknown, field: string): CharacterCapabilities => {
  const row = object(value, field)
  return {
    bundle_prepare: bool(row.bundle_prepare, `${field}.bundle_prepare`),
    blend_export: bool(row.blend_export, `${field}.blend_export`),
    animation: bool(row.animation, `${field}.animation`),
    raw_expressions: bool(row.raw_expressions, `${field}.raw_expressions`),
    basic_mmd: bool(row.basic_mmd, `${field}.basic_mmd`),
  }
}

const parseVariant = (value: unknown, field: string): CharacterVariant => {
  const row = object(value, field)
  if (row.geometry_status !== 'not_verified') return invalid(`${field}.geometry_status`, 'not_verified')
  const result: CharacterVariant = {
    variant_id: text(row.variant_id, `${field}.variant_id`),
    label: text(row.label, `${field}.label`),
    variant: text(row.variant, `${field}.variant`),
    geometry_status: 'not_verified',
  }
  if ('part_names' in row) result.part_names = stringArray(row.part_names, `${field}.part_names`)
  if ('animation_binding_status' in row) {
    result.animation_binding_status = text(row.animation_binding_status, `${field}.animation_binding_status`)
  }
  return result
}

const parseNameSource = (value: unknown, field: string): CharacterNameSource | null => {
  if (value === null) return null
  const row = object(value, field)
  if (row.table !== 'NpcTemplateGroupTable' && row.table !== 'NpcTable' && row.table !== 'EnemyTemplateDisplayInfoTable' && row.table !== 'EnemyDisplayInfoTable') {
    return invalid(`${field}.table`, 'NpcTemplateGroupTable, NpcTable, EnemyTemplateDisplayInfoTable, or EnemyDisplayInfoTable')
  }
  return {
    table: row.table,
    row_key: text(row.row_key, `${field}.row_key`),
    field: text(row.field, `${field}.field`),
    text_id: text(row.text_id, `${field}.text_id`),
  }
}

const parseAssetCategory = (value: unknown, field: string): CharacterAssetCategory => {
  if (value === 'playable' || value === 'npc' || value === 'animal' || value === 'creature' || value === 'object') return value
  return invalid(field, 'playable, npc, animal, creature, or object')
}

const parseCharacterSummary = (value: unknown, field: string): CharacterSummary => {
  const row = object(value, field)
  if (row.entry_kind !== 'playable' && row.entry_kind !== 'independent_npc') {
    return invalid(`${field}.entry_kind`, 'playable or independent_npc')
  }
  const base = {
    selection_id: text(row.selection_id, `${field}.selection_id`),
    asset_category: parseAssetCategory(row.asset_category, `${field}.asset_category`),
    english_name: nullableText(row.english_name, `${field}.english_name`),
    variants: array(row.variants, `${field}.variants`, parseVariant),
    capabilities: parseCapabilities(row.capabilities, `${field}.capabilities`),
    optional_tools_reason: nullableText(row.optional_tools_reason, `${field}.optional_tools_reason`),
    unavailable_reason: nullableText(row.unavailable_reason, `${field}.unavailable_reason`),
  }
  if (row.entry_kind === 'playable') {
    if (base.english_name === null) return invalid(`${field}.english_name`, 'text for a playable character')
    return {
      ...base,
      name: text(row.name, `${field}.name`),
      character_id: text(row.character_id, `${field}.character_id`),
      entry_kind: 'playable',
    }
  }
  if (base.english_name !== null) return invalid(`${field}.english_name`, 'null for an independent NPC')
  const npcBase = { ...base, name: nullableText(row.name, `${field}.name`), english_name: null }
  if (row.name_status !== 'resolved' && row.name_status !== 'missing' && row.name_status !== 'ambiguous') {
    return invalid(`${field}.name_status`, 'resolved, missing, or ambiguous')
  }
  if (row.source_kind !== 'prefab' && row.source_kind !== 'config_assembled' && row.source_kind !== 'unresolved') {
    return invalid(`${field}.source_kind`, 'prefab, config_assembled, or unresolved')
  }
  const cpuAnimationTemplate = row.cpu_animation_template === null
    ? null
    : text(row.cpu_animation_template, `${field}.cpu_animation_template`)
  const assemblyScope = row.assembly_scope === null ? null : text(row.assembly_scope, `${field}.assembly_scope`)
  return {
    ...npcBase,
    npc_id: text(row.npc_id, `${field}.npc_id`),
    entry_kind: 'independent_npc',
    name_status: row.name_status,
    source_kind: row.source_kind,
    name_source: parseNameSource(row.name_source, `${field}.name_source`),
    expression_unavailable_reason: text(row.expression_unavailable_reason, `${field}.expression_unavailable_reason`),
    cpu_animation_template: cpuAnimationTemplate,
    animation_scope: text(row.animation_scope, `${field}.animation_scope`),
    animation_catalog_available: bool(row.animation_catalog_available, `${field}.animation_catalog_available`),
    provider_found: bool(row.provider_found, `${field}.provider_found`),
    assembly_scope: assemblyScope,
  }
}

export const parseCharacterCatalog = (value: unknown): CharacterCatalog => {
  const row = object(value, 'CharacterCatalog')
  if (row.format !== 'CharacterCatalog/1') return invalid('CharacterCatalog.format', 'CharacterCatalog/1')
  const availability = object(row.availability, 'CharacterCatalog.availability')
  const result: CharacterCatalog = {
    format: 'CharacterCatalog/1',
    catalog_id: text(row.catalog_id, 'CharacterCatalog.catalog_id'),
    source_identity: nullableText(row.source_identity, 'CharacterCatalog.source_identity'),
    availability: {
      ready: bool(availability.ready, 'CharacterCatalog.availability.ready'),
      reason: nullableText(availability.reason, 'CharacterCatalog.availability.reason'),
    },
    characters: array(row.characters, 'CharacterCatalog.characters', parseCharacterSummary),
    profiles: array(row.profiles, 'CharacterCatalog.profiles', (item, field) => {
      const profile = object(item, field)
      return {
        profile_id: text(profile.profile_id, `${field}.profile_id`),
        name: text(profile.name, `${field}.name`),
        engines: stringArray(profile.engines, `${field}.engines`),
      }
    }),
  }
  if ('limitations' in row) result.limitations = stringArray(row.limitations, 'CharacterCatalog.limitations')
  if (result.availability.ready && (result.catalog_id === 'unavailable' || result.source_identity === null)) {
    return invalid('CharacterCatalog', 'ready availability with a current catalog and source identity')
  }
  return result
}

export const parseCharacterDetail = (value: unknown): CharacterDetail => {
  const row = object(value, 'CharacterDetail')
  if (row.format !== 'CharacterDetail/1') return invalid('CharacterDetail.format', 'CharacterDetail/1')
  return {
    format: 'CharacterDetail/1',
    catalog_id: text(row.catalog_id, 'CharacterDetail.catalog_id'),
    ...parseCharacterSummary(row, 'CharacterDetail'),
  }
}

const parseReplay = (value: unknown, field: string): CharacterReplay => {
  const row = object(value, field)
  return {
    replay_id: text(row.replay_id, `${field}.replay_id`),
    label: text(row.label, `${field}.label`),
    boundary: text(row.boundary, `${field}.boundary`),
  }
}

const parseAnimation = (value: unknown, field: string): CharacterAnimation => {
  const row = object(value, field)
  const result: CharacterAnimation = {
    action_id: text(row.action_id, `${field}.action_id`),
    name: text(row.name, `${field}.name`),
    binding_status: text(row.binding_status, `${field}.binding_status`),
  }
  if ('state_name' in row) result.state_name = text(row.state_name, `${field}.state_name`)
  if ('ordinary_replays' in row) result.ordinary_replays = array(row.ordinary_replays, `${field}.ordinary_replays`, parseReplay)
  if ('native_expression_replays' in row) {
    result.native_expression_replays = array(row.native_expression_replays, `${field}.native_expression_replays`, parseReplay)
  }
  if ('finalik_replays' in row) result.finalik_replays = array(row.finalik_replays, `${field}.finalik_replays`, parseReplay)
  return result
}

const parseCpuActionReference = (value: unknown, field: string): CharacterCpuActionReference => {
  const row = object(value, field)
  if (row.execution_available !== false) return invalid(`${field}.execution_available`, 'false')
  return {
    reference_id: parseSha256(row.reference_id, `${field}.reference_id`),
    state_name: text(row.state_name, `${field}.state_name`),
    clip_cab: text(row.clip_cab, `${field}.clip_cab`),
    clip_path_id: text(row.clip_path_id, `${field}.clip_path_id`),
    logical_provider: text(row.logical_provider, `${field}.logical_provider`),
    execution_available: false,
    request_available: bool(row.request_available, `${field}.request_available`),
  }
}

const parseCpuActionReferences = (value: unknown, field: string): CharacterCpuActionReferences => {
  const row = object(value, field)
  if (row.format !== 'EndfieldNpcCpuActionReferences/1') return invalid(`${field}.format`, 'EndfieldNpcCpuActionReferences/1')
  if (row.status !== 'not_configured' && row.status !== 'not_present' && row.status !== 'exact_avatar_base_clip_references') {
    return invalid(`${field}.status`, 'a supported CPU reference status')
  }
  if (row.execution_available !== false) return invalid(`${field}.execution_available`, 'false')
  const actions = array(row.actions, `${field}.actions`, parseCpuActionReference)
  if (row.status !== 'exact_avatar_base_clip_references') {
    if (actions.length !== 0) return invalid(`${field}.actions`, 'an empty array when CPU references are unavailable')
    return {
      format: 'EndfieldNpcCpuActionReferences/1',
      status: row.status,
      execution_available: false,
      actions: [],
      unavailable_reason: text(row.unavailable_reason, `${field}.unavailable_reason`),
    }
  }
  const source = object(row.cpu_source, `${field}.cpu_source`)
  const avatar = object(row.avatar, `${field}.avatar`)
  if (row.template_namespace !== 'Humanoid' && row.template_namespace !== 'Generic') {
    return invalid(`${field}.template_namespace`, 'Humanoid or Generic')
  }
  return {
    format: 'EndfieldNpcCpuActionReferences/1',
    status: 'exact_avatar_base_clip_references',
    execution_available: false,
    actions,
    source_identity: text(row.source_identity, `${field}.source_identity`),
    selection_id: text(row.selection_id, `${field}.selection_id`),
    template_id: text(row.template_id, `${field}.template_id`),
    part_name: text(row.part_name, `${field}.part_name`),
    cpu_animation_template: text(row.cpu_animation_template, `${field}.cpu_animation_template`),
    template_namespace: row.template_namespace,
    cpu_source: {
      cab: text(source.cab, `${field}.cpu_source.cab`),
      path_id: text(source.path_id, `${field}.cpu_source.path_id`),
      sha256: parseSha256(source.sha256, `${field}.cpu_source.sha256`),
    },
    avatar: {
      cab: text(avatar.cab, `${field}.avatar.cab`),
      path_id: text(avatar.path_id, `${field}.avatar.path_id`),
      binding_status: text(avatar.binding_status, `${field}.avatar.binding_status`),
    },
    request_available: bool(row.request_available, `${field}.request_available`),
    request_unavailable_reason: nullableText(row.request_unavailable_reason, `${field}.request_unavailable_reason`),
    limitations: stringArray(row.limitations, `${field}.limitations`),
  }
}

const parseUnsupportedAction = (value: unknown, field: string): CharacterUnsupportedAction => {
  const row = object(value, field)
  return {
    state_name: text(row.state_name, `${field}.state_name`),
    clip_name: text(row.clip_name, `${field}.clip_name`),
    binding_status: text(row.binding_status, `${field}.binding_status`),
    reason: text(row.reason, `${field}.reason`),
  }
}

const parseAnimationCoverage = (value: unknown, field: string): CharacterAnimationCoverage => {
  const row = object(value, field)
  const result: CharacterAnimationCoverage = {
    scope: text(row.scope, `${field}.scope`),
    available_action_count: integer(row.available_action_count, `${field}.available_action_count`, 0),
    unsupported_action_count: integer(row.unsupported_action_count, `${field}.unsupported_action_count`, 0),
  }
  if ('source_state_count' in row) result.source_state_count = integer(row.source_state_count, `${field}.source_state_count`, 0)
  if ('source_reference_count' in row) result.source_reference_count = integer(row.source_reference_count, `${field}.source_reference_count`, 0)
  if ('runtime_state_machine_reconstructed' in row) {
    if (row.runtime_state_machine_reconstructed !== false) return invalid(`${field}.runtime_state_machine_reconstructed`, 'false')
    result.runtime_state_machine_reconstructed = false
  }
  return result
}

export const parseCharacterAnimationCatalog = (value: unknown): CharacterAnimationCatalog => {
  const row = object(value, 'CharacterAnimationCatalog')
  if (row.format !== 'CharacterAnimationCatalog/1') {
    return invalid('CharacterAnimationCatalog.format', 'CharacterAnimationCatalog/1')
  }
  const result: CharacterAnimationCatalog = {
    format: 'CharacterAnimationCatalog/1',
    source_identity: text(row.source_identity, 'CharacterAnimationCatalog.source_identity'),
    selection_id: text(row.selection_id, 'CharacterAnimationCatalog.selection_id'),
    variant_id: text(row.variant_id, 'CharacterAnimationCatalog.variant_id'),
    offset: integer(row.offset, 'CharacterAnimationCatalog.offset', 0),
    limit: integer(row.limit, 'CharacterAnimationCatalog.limit', 1),
    total: integer(row.total, 'CharacterAnimationCatalog.total', 0),
    animations: array(row.animations, 'CharacterAnimationCatalog.animations', parseAnimation),
    limitations: stringArray(row.limitations, 'CharacterAnimationCatalog.limitations'),
  }
  if (result.limit > 500) return invalid('CharacterAnimationCatalog.limit', 'an integer <= 500')
  if (result.animations.length > result.limit) {
    return invalid('CharacterAnimationCatalog.animations', 'a page no larger than the declared limit')
  }
  if ('binding_status' in row) result.binding_status = text(row.binding_status, 'CharacterAnimationCatalog.binding_status')
  if ('unavailable_reason' in row) {
    result.unavailable_reason = nullableText(row.unavailable_reason, 'CharacterAnimationCatalog.unavailable_reason')
  }
  if ('source_cpu_action_references' in row) {
    result.source_cpu_action_references = parseCpuActionReferences(row.source_cpu_action_references, 'CharacterAnimationCatalog.source_cpu_action_references')
  }
  if ('unsupported_actions' in row) {
    result.unsupported_actions = array(row.unsupported_actions, 'CharacterAnimationCatalog.unsupported_actions', parseUnsupportedAction)
  }
  if ('coverage_counts' in row) {
    result.coverage_counts = parseAnimationCoverage(row.coverage_counts, 'CharacterAnimationCatalog.coverage_counts')
  }
  return result
}

const parsePreparationRequest = (value: unknown): CharacterPreparationRequest => {
  const row = object(value, 'CharacterPreparationRequest')
  if (row.format !== 'CharacterPreparationRequest/1') return invalid('CharacterPreparationRequest.format', 'CharacterPreparationRequest/1')
  const source = object(row.source, 'CharacterPreparationRequest.source')
  return {
    format: 'CharacterPreparationRequest/1',
    request_id: parseUuid(row.request_id, 'CharacterPreparationRequest.request_id'),
    source: {
      game_root: text(source.game_root, 'CharacterPreparationRequest.source.game_root'),
      blender_exe: text(source.blender_exe, 'CharacterPreparationRequest.source.blender_exe'),
      cache_root: text(source.cache_root, 'CharacterPreparationRequest.source.cache_root'),
      output_root: text(source.output_root, 'CharacterPreparationRequest.source.output_root'),
    },
  }
}

const parseMaterials = (value: unknown, field: string): CharacterMaterialOptions => {
  const row = object(value, field)
  if (row.mode !== 'original' && row.mode !== 'simplified') return invalid(`${field}.mode`, 'original or simplified')
  if (row.lighting !== 'full' && row.lighting !== 'simple') return invalid(`${field}.lighting`, 'full or simple')
  const weather = object(row.weather, `${field}.weather`)
  if (weather.quality !== 'full' && weather.quality !== 'simple') return invalid(`${field}.weather.quality`, 'full or simple')
  const effects = array<CharacterWeatherEffect>(weather.effects, `${field}.weather.effects`, (effect, effectField) => {
    if (effect !== 'wet' && effect !== 'snow' && effect !== 'rain') return invalid(effectField, 'wet, snow or rain')
    return effect
  })
  if (new Set(effects).size !== effects.length) return invalid(`${field}.weather.effects`, 'unique effects')
  if (row.mode === 'original' && (row.lighting !== 'full' || weather.quality !== 'full' || effects.length !== 3)) {
    return invalid(field, 'original mode with full lighting, full weather and wet, snow, rain')
  }
  return { mode: row.mode, lighting: row.lighting, weather: { quality: weather.quality, effects: WEATHER_EFFECTS.filter(effect => effects.includes(effect)) } }
}

const parseAppearanceOptions = (value: unknown, field: string): CharacterAppearanceOptions => {
  const row = object(value, field)
  if (row.potential !== 'base' && row.potential !== 'maximum') return invalid(`${field}.potential`, 'base or maximum')
  if (row.fur !== 'existing' && row.fur !== 'source_shell' && row.fur !== 'charcoal_embers') return invalid(`${field}.fur`, 'existing, source_shell or charcoal_embers')
  return { potential: row.potential, fur: row.fur }
}

const parseAppearanceReport = (value: unknown, options: CharacterAppearanceOptions): CharacterAppearanceReport => {
  const field = 'CharacterJob.result.appearance_report'
  const row = object(value, field)
  if (row.format !== 'EndfieldCharacterAppearance/1') return invalid(`${field}.format`, 'EndfieldCharacterAppearance/1')
  if (row.status !== 'completed' && row.status !== 'partial') return invalid(`${field}.status`, 'completed or partial')
  const potential = object(row.potential, `${field}.potential`)
  const fur = object(row.fur, `${field}.fur`)
  if (potential.requested !== options.potential || fur.requested !== options.fur) return invalid(field, 'requested values matching result.appearance_options')
  if (potential.status !== 'applied' && potential.status !== 'not_applicable' && potential.status !== 'partial') return invalid(`${field}.potential.status`, 'applied, not_applicable or partial')
  if (fur.status !== 'applied' && fur.status !== 'not_applicable' && fur.status !== 'partial' && fur.status !== 'unchanged') return invalid(`${field}.fur.status`, 'applied, not_applicable, partial or unchanged')
  if (row.status === 'completed' && (potential.status === 'partial' || fur.status === 'partial')) return invalid(`${field}.status`, 'partial when a component is partial')
  // Evidence entries have source-specific shapes; preserve them as inert JSON text.
  const evidence = (items: unknown, path: string): string[] => array(items, path, (item, itemField) => {
    const serialized = JSON.stringify(item)
    return serialized === undefined ? invalid(itemField, 'JSON evidence') : serialized
  })
  const result: CharacterAppearanceReport = {
    format: 'EndfieldCharacterAppearance/1', status: row.status,
    potential: { requested: options.potential, status: potential.status },
    fur: { requested: options.fur, status: fur.status },
  }
  if ('scope' in potential) result.potential.scope = text(potential.scope, `${field}.potential.scope`)
  if ('pending' in potential) result.potential.pending = evidence(potential.pending, `${field}.potential.pending`)
  if ('pending' in fur) result.fur.pending = evidence(fur.pending, `${field}.fur.pending`)
  if ('verification' in fur) {
    const verification = object(fur.verification, `${field}.fur.verification`)
    result.fur.verification = {}
    for (const key of ['source_shell_count', 'artist_applied_count', 'artist_skipped_count', 'skipped_hidden_source_count'] as const) {
      if (key in verification) result.fur.verification[key] = integer(verification[key], `${field}.fur.verification.${key}`, 0)
    }
  }
  return result
}

const parseExportOptions = (value: unknown): CharacterExportOptions => {
  const row = object(value, 'CharacterExportRequest.options')
  const result: CharacterExportOptions = {}
  if ('materials' in row) result.materials = parseMaterials(row.materials, 'CharacterExportRequest.options.materials')
  if ('appearance' in row) {
    result.appearance = parseAppearanceOptions(row.appearance, 'CharacterExportRequest.options.appearance')
    if (result.materials?.mode === 'simplified') return invalid('CharacterExportRequest.options.appearance', 'original materials')
  }
  if ('raw_expressions' in row) result.raw_expressions = bool(row.raw_expressions, 'CharacterExportRequest.options.raw_expressions')
  if ('basic_mmd' in row) result.basic_mmd = bool(row.basic_mmd, 'CharacterExportRequest.options.basic_mmd')
  if ('native_expression_replay_id' in row) {
    result.native_expression_replay_id = parseSha256(row.native_expression_replay_id, 'CharacterExportRequest.options.native_expression_replay_id')
  }
  if ('animation' in row && row.animation !== null) {
    const animation = object(row.animation, 'CharacterExportRequest.options.animation')
    const frames = integer(animation.maximum_stored_frames, 'CharacterExportRequest.options.animation.maximum_stored_frames', 2)
    if (frames > 18000) return invalid('CharacterExportRequest.options.animation.maximum_stored_frames', 'an integer <= 18000')
    const hasAction = 'action_id' in animation
    const hasReference = 'reference_id' in animation
    if (hasAction === hasReference) return invalid('CharacterExportRequest.options.animation', 'exactly one action_id or reference_id')
    if (hasAction) {
      const action: CharacterActionAnimationSelection = {
        action_id: text(animation.action_id, 'CharacterExportRequest.options.animation.action_id'),
        maximum_stored_frames: frames,
      }
      if ('replay_id' in animation) action.replay_id = parseSha256(animation.replay_id, 'CharacterExportRequest.options.animation.replay_id')
      if ('finalik_replay_id' in animation) {
        action.finalik_replay_id = parseSha256(animation.finalik_replay_id, 'CharacterExportRequest.options.animation.finalik_replay_id')
      }
      result.animation = action
    } else {
      if ('replay_id' in animation || 'finalik_replay_id' in animation) {
        return invalid('CharacterExportRequest.options.animation', 'replay selectors only with action_id')
      }
      result.animation = {
        reference_id: parseSha256(animation.reference_id, 'CharacterExportRequest.options.animation.reference_id'),
        maximum_stored_frames: frames,
      }
    }
  }
  const selectedAnimation = result.animation
  if (result.native_expression_replay_id !== undefined &&
    (selectedAnimation === undefined || !('action_id' in selectedAnimation))) {
    return invalid('CharacterExportRequest.options.native_expression_replay_id', 'an explicit action_id animation')
  }
  return result
}

export const parseCharacterExportRequest = (value: unknown): CharacterExportRequest => {
  const row = object(value, 'CharacterExportRequest')
  if (row.format !== 'CharacterExportRequest/1') return invalid('CharacterExportRequest.format', 'CharacterExportRequest/1')
  const request: CharacterExportRequest = {
    format: 'CharacterExportRequest/1',
    request_id: parseUuid(row.request_id, 'CharacterExportRequest.request_id'),
    catalog_id: text(row.catalog_id, 'CharacterExportRequest.catalog_id'),
    source_identity: text(row.source_identity, 'CharacterExportRequest.source_identity'),
    selection_id: text(row.selection_id, 'CharacterExportRequest.selection_id'),
    variant_id: text(row.variant_id, 'CharacterExportRequest.variant_id'),
    export_profile_id: row.export_profile_id === 'hgrp-preview'
      ? 'hgrp-preview'
      : invalid('CharacterExportRequest.export_profile_id', 'hgrp-preview'),
    output_label: typeof row.output_label === 'string' && SAFE_LABEL.test(row.output_label)
      ? row.output_label
      : invalid('CharacterExportRequest.output_label', '1-80 filename-safe characters'),
  }
  if ('options' in row) request.options = parseExportOptions(row.options)
  return request
}

export const parseCharacterPreparationRequestForStorage = (value: unknown): CharacterPreparationRequest =>
  parsePreparationRequest(value)

export const parseCharacterExportRequestForStorage = (value: unknown): CharacterExportRequest =>
  parseCharacterExportRequest(value)

const parsePublicRequest = (value: unknown): CharacterPublicRequest => {
  const row = object(value, 'CharacterJob.request')
  if (row.format === 'CharacterPreparationRequest/1') {
    return {
      format: 'CharacterPreparationRequest/1',
      request_id: parseUuid(row.request_id, 'CharacterJob.request.request_id'),
    }
  }
  return parseCharacterExportRequest(row)
}

const parseValidationIssue = (value: unknown, field: string): CharacterValidationIssue => {
  const row = object(value, field)
  return {
    code: text(row.code, `${field}.code`),
    message: text(row.message, `${field}.message`),
    field: nullableText(row.field, `${field}.field`),
  }
}

const parseValidation = (value: unknown): CharacterRequestValidation => {
  const row = object(value, 'CharacterRequestValidation')
  if (row.format !== 'CharacterRequestValidation/1' || row.status !== 'valid') {
    return invalid('CharacterRequestValidation', 'a valid CharacterRequestValidation/1 response')
  }
  if (row.kind !== 'preparation' && row.kind !== 'export') return invalid('CharacterRequestValidation.kind', 'preparation or export')
  const issues = array(row.issues, 'CharacterRequestValidation.issues', parseValidationIssue)
  if (issues.length > 0) return invalid('CharacterRequestValidation.issues', 'empty for status valid')
  return {
    format: 'CharacterRequestValidation/1',
    status: 'valid',
    kind: row.kind,
    canonical_request: parsePublicRequest(row.canonical_request),
    issues,
  }
}

const parseDeliveryFile = (value: unknown, field: string): CharacterDeliveryFile => {
  const row = object(value, field)
  const relativePath = text(row.relative_path, `${field}.relative_path`)
  if (relativePath.startsWith('/') || relativePath.startsWith('\\') || relativePath.includes('\\') ||
    /^[A-Za-z]:/.test(relativePath) || relativePath.split('/').some((segment) => segment === '' || segment === '.' || segment === '..')) {
    return invalid(`${field}.relative_path`, 'a safe relative path')
  }
  if (typeof row.role !== 'string' || !DELIVERY_ROLES.includes(row.role as CharacterDeliveryFile['role'])) {
    return invalid(`${field}.role`, 'a supported delivery role')
  }
  return {
    relative_path: relativePath,
    role: row.role as CharacterDeliveryFile['role'],
    bytes: integer(row.bytes, `${field}.bytes`, 0),
    sha256: parseSha256(row.sha256, `${field}.sha256`),
  }
}

const parseJobResult = (value: unknown, kind: CharacterJobKind): CharacterJobResult => {
  const row = object(value, 'CharacterJob.result')
  const expectedFormat = kind === 'preparation' ? 'CharacterPreparationResult/1' : 'CharacterExportResult/1'
  if (row.format !== expectedFormat) return invalid('CharacterJob.result.format', expectedFormat)
  if (row.status !== 'completed' && row.status !== 'partial') return invalid('CharacterJob.result.status', 'completed or partial')
  const appearance: Pick<CharacterJobResult, 'appearance_options' | 'appearance_report'> = {}
  if ('appearance_options' in row || 'appearance_report' in row) {
    if (kind !== 'export' || !('appearance_options' in row) || !('appearance_report' in row)) return invalid('CharacterJob.result.appearance_options', 'paired options and report on an export')
    appearance.appearance_options = parseAppearanceOptions(row.appearance_options, 'CharacterJob.result.appearance_options')
    appearance.appearance_report = parseAppearanceReport(row.appearance_report, appearance.appearance_options)
  }
  return {
    ...appearance,
    ...(kind === 'export' && 'material_options' in row ? { material_options: parseMaterials(row.material_options, 'CharacterJob.result.material_options') } : {}),
    format: expectedFormat,
    status: row.status,
    output_directory: text(row.output_directory, 'CharacterJob.result.output_directory'),
    files: array(row.files, 'CharacterJob.result.files', parseDeliveryFile),
    pending: array(row.pending, 'CharacterJob.result.pending', (item, field) => {
      const pending = object(item, field)
      return {
        code: text(pending.code, `${field}.code`),
        message: text(pending.message, `${field}.message`),
      }
    }),
  }
}

const parseJobError = (value: unknown): CharacterJobError => {
  const row = object(value, 'CharacterJob.error')
  return {
    code: text(row.code, 'CharacterJob.error.code'),
    message: text(row.message, 'CharacterJob.error.message'),
    stage: text(row.stage, 'CharacterJob.error.stage'),
    retryable: bool(row.retryable, 'CharacterJob.error.retryable'),
    diagnostic_id: text(row.diagnostic_id, 'CharacterJob.error.diagnostic_id'),
  }
}

export const parseCharacterJob = (value: unknown): CharacterJob => {
  const row = object(value, 'CharacterJob')
  if (row.format !== 'CharacterJob/1') return invalid('CharacterJob.format', 'CharacterJob/1')
  if (row.kind !== 'preparation' && row.kind !== 'export') return invalid('CharacterJob.kind', 'preparation or export')
  if (typeof row.state !== 'string' || !JOB_STATES.includes(row.state as CharacterJobState)) {
    return invalid('CharacterJob.state', 'a supported task state')
  }
  if (typeof row.phase !== 'string' || !JOB_PHASES.includes(row.phase as CharacterJobPhase)) {
    return invalid('CharacterJob.phase', 'a supported task phase')
  }
  const jobId = parseUuid(row.job_id, 'CharacterJob.job_id')
  const request = parsePublicRequest(row.request)
  if (request.request_id !== jobId || request.format !== (row.kind === 'preparation'
    ? 'CharacterPreparationRequest/1'
    : 'CharacterExportRequest/1')) {
    return invalid('CharacterJob.request', 'the matching job identity and kind')
  }
  const result = row.result === null ? null : parseJobResult(row.result, row.kind)
  const error = row.error === null ? null : parseJobError(row.error)
  if (result !== null && (row.state !== result.status || row.phase !== result.status)) {
    return invalid('CharacterJob.result', 'state and phase matching the result status')
  }
  if (row.state === 'failed' && error === null) return invalid('CharacterJob.error', 'a failure description for failed state')
  return {
    format: 'CharacterJob/1',
    job_id: jobId,
    kind: row.kind,
    state: row.state as CharacterJobState,
    phase: row.phase as CharacterJobPhase,
    progress: finiteNumber(row.progress, 'CharacterJob.progress', 0, 1),
    message: typeof row.message === 'string' ? row.message : invalid('CharacterJob.message', 'text'),
    cancel_requested: bool(row.cancel_requested, 'CharacterJob.cancel_requested'),
    request,
    result,
    error,
  }
}

const parseJobEvent = (value: unknown, field: string): CharacterJobEvent => {
  const row = object(value, field)
  if (typeof row.state !== 'string' || !JOB_STATES.includes(row.state as CharacterJobState)) {
    return invalid(`${field}.state`, 'a supported task state')
  }
  if (typeof row.phase !== 'string' || !JOB_PHASES.includes(row.phase as CharacterJobPhase)) {
    return invalid(`${field}.phase`, 'a supported task phase')
  }
  const timestamp = text(row.timestamp, `${field}.timestamp`)
  if (!Number.isFinite(Date.parse(timestamp))) return invalid(`${field}.timestamp`, 'an ISO date-time')
  return {
    sequence: integer(row.sequence, `${field}.sequence`, 1),
    timestamp,
    state: row.state as CharacterJobState,
    phase: row.phase as CharacterJobPhase,
    progress: finiteNumber(row.progress, `${field}.progress`, 0, 1),
    message: typeof row.message === 'string' ? row.message : invalid(`${field}.message`, 'text'),
    cancel_requested: bool(row.cancel_requested, `${field}.cancel_requested`),
  }
}

const parseEventsResponse = (value: unknown): CharacterJobEventsResponse => {
  const row = object(value, 'CharacterJobEvents')
  if (row.format !== 'CharacterJobEvents/1') return invalid('CharacterJobEvents.format', 'CharacterJobEvents/1')
  return {
    format: 'CharacterJobEvents/1',
    job_id: parseUuid(row.job_id, 'CharacterJobEvents.job_id'),
    events: array(row.events, 'CharacterJobEvents.events', parseJobEvent),
    next_after: integer(row.next_after, 'CharacterJobEvents.next_after', 0),
    has_more: bool(row.has_more, 'CharacterJobEvents.has_more'),
  }
}

const parseCancelAcknowledgement = (value: unknown): CharacterCancelAcknowledgement => {
  const row = object(value, 'CharacterJobCancelAck')
  if (row.format !== 'CharacterJobCancelAck/1' || row.accepted !== true) {
    return invalid('CharacterJobCancelAck', 'an accepted CharacterJobCancelAck/1 response')
  }
  if (row.state !== 'accepted' && row.state !== 'already_terminal') {
    return invalid('CharacterJobCancelAck.state', 'accepted or already_terminal')
  }
  return {
    format: 'CharacterJobCancelAck/1',
    job_id: parseUuid(row.job_id, 'CharacterJobCancelAck.job_id'),
    accepted: true,
    state: row.state,
  }
}

const requestParsed = async <T>(
  path: string,
  init: RequestInit,
  parse: (payload: unknown) => T,
  verify: (value: T) => void,
): Promise<T> => {
  let parsed: T | undefined
  const validate = (payload: unknown): payload is unknown => {
    try {
      const result = parse(payload)
      verify(result)
      parsed = result
      return true
    } catch {
      return false
    }
  }
  await requestJson<unknown>(path, init, validate)
  if (parsed === undefined) throw new Error(`${init.method ?? 'GET'} ${path} returned no parsed response`)
  return parsed
}

const postParsed = async <TBody, TResponse>(
  path: string,
  body: TBody,
  signal: AbortSignal,
  parse: (payload: unknown) => TResponse,
  verify: (value: TResponse) => void,
): Promise<TResponse> => {
  let serializedBody: string
  try {
    serializedBody = JSON.stringify(body)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`POST ${path} request body could not be serialized: ${detail}`, { cause: error })
  }
  return requestParsed(
    path,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: serializedBody, signal },
    parse,
    verify,
  )
}

export const getCharacterCatalog = (signal: AbortSignal): Promise<CharacterCatalog> =>
  requestParsed(CHARACTERS_PATH, { method: 'GET', signal }, parseCharacterCatalog, () => undefined)

export const getCharacterDetail = (
  selectionId: string,
  expectedCatalogId: string,
  signal: AbortSignal,
): Promise<CharacterDetail> => requestParsed(
  `${CHARACTERS_PATH}/${encodeURIComponent(selectionId)}`,
  { method: 'GET', signal },
  parseCharacterDetail,
  (detail) => {
    if (detail.selection_id !== selectionId || detail.catalog_id !== expectedCatalogId) {
      throw new Error('Character detail identity differs from the current catalog selection')
    }
  },
)

export const getCharacterAnimations = (
  selectionId: string,
  variantId: string,
  expectedSourceIdentity: string,
  offset: number,
  limit: number,
  signal: AbortSignal,
): Promise<CharacterAnimationCatalog> => {
  if (!Number.isInteger(offset) || offset < 0 || !Number.isInteger(limit) || limit < 1 || limit > 500) {
    throw new Error('Character animation pagination requires offset >= 0 and limit within 1..500')
  }
  const query = new URLSearchParams({ variant_id: variantId, offset: String(offset), limit: String(limit) })
  return requestParsed(
    `${CHARACTERS_PATH}/${encodeURIComponent(selectionId)}/animations?${query.toString()}`,
    { method: 'GET', signal },
    parseCharacterAnimationCatalog,
    (catalog) => {
      if (catalog.selection_id !== selectionId || catalog.variant_id !== variantId ||
        catalog.source_identity !== expectedSourceIdentity || catalog.offset !== offset || catalog.limit !== limit) {
        throw new Error('Character animation page identity differs from the current selection')
      }
    },
  )
}

const verifyValidation = (
  validation: CharacterRequestValidation,
  kind: CharacterJobKind,
  requestId: string,
): void => {
  if (validation.kind !== kind || validation.canonical_request.request_id !== requestId ||
    validation.canonical_request.format !== (kind === 'preparation'
      ? 'CharacterPreparationRequest/1'
      : 'CharacterExportRequest/1')) {
    throw new Error('Character validation response does not match the submitted request')
  }
}

export const validateCharacterExport = async (
  request: CharacterExportRequest,
  signal: AbortSignal,
): Promise<CharacterRequestValidation> => {
  const validation = await requestParsed(
    `${JOBS_PATH}/validate`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request), signal },
    parseValidation,
    (value) => verifyValidation(value, 'export', request.request_id),
  )
  if (validation.canonical_request.format === 'CharacterExportRequest/1') {
    assertMaterialAcknowledgement(request.options?.materials, validation.canonical_request.options?.materials)
    assertAppearanceAcknowledgement(request.options?.appearance, validation.canonical_request.options?.appearance)
  }
  return validation
}

export const submitCharacterExport = (
  request: CharacterExportRequest,
  signal: AbortSignal,
): Promise<CharacterJob> => postParsed(
  JOBS_PATH,
  request,
  signal,
  parseCharacterJob,
  (job) => {
    if (job.kind !== 'export' || job.job_id !== request.request_id) {
      throw new Error('Character export submission returned a different job identity')
    }
  },
)

export const validateCharacterPreparation = (
  request: CharacterPreparationRequest,
  signal: AbortSignal,
): Promise<CharacterRequestValidation> => requestParsed(
  `${PREPARATION_PATH}/validate`,
  { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request), signal },
  parseValidation,
  (validation) => verifyValidation(validation, 'preparation', request.request_id),
)

export const submitCharacterPreparation = (
  request: CharacterPreparationRequest,
  signal: AbortSignal,
): Promise<CharacterJob> => postParsed(
  PREPARATION_PATH,
  request,
  signal,
  parseCharacterJob,
  (job) => {
    if (job.kind !== 'preparation' || job.job_id !== request.request_id) {
      throw new Error('Character preparation submission returned a different job identity')
    }
  },
)

const jobPath = (kind: CharacterJobKind, jobId: string): string => {
  const root = kind === 'preparation' ? PREPARATION_PATH : JOBS_PATH
  return `${root}/${encodeURIComponent(jobId)}`
}

export const getCharacterJob = (
  kind: CharacterJobKind,
  jobId: string,
  requestId: string,
  signal: AbortSignal,
): Promise<CharacterJob> => requestParsed(
  jobPath(kind, jobId),
  { method: 'GET', signal },
  parseCharacterJob,
  (job) => {
    if (job.kind !== kind || job.job_id !== jobId || job.request.request_id !== requestId) {
      throw new Error('Character task response identity differs from the saved request')
    }
  },
)

export const getCharacterJobEvents = (
  kind: CharacterJobKind,
  jobId: string,
  after: number,
  limit: number,
  signal: AbortSignal,
): Promise<CharacterJobEventsResponse> => {
  if (!Number.isInteger(after) || after < 0 || !Number.isInteger(limit) || limit < 1 || limit > 500) {
    throw new Error('Character event pagination requires after >= 0 and limit within 1..500')
  }
  const query = new URLSearchParams({ after: String(after), limit: String(limit) })
  return requestParsed(
    `${jobPath(kind, jobId)}/events?${query.toString()}`,
    { method: 'GET', signal },
    parseEventsResponse,
    (response) => {
      if (response.job_id !== jobId || response.events.some((event, index) =>
        index > 0 && event.sequence !== response.events[index - 1].sequence + 1) ||
        response.next_after !== after + response.events.length) {
        throw new Error('Character event page identity or sequence is invalid')
      }
    },
  )
}

export const requestCharacterCancellation = (
  kind: CharacterJobKind,
  jobId: string,
  signal: AbortSignal,
): Promise<CharacterCancelAcknowledgement> => postParsed(
  `${jobPath(kind, jobId)}/cancel`,
  {},
  signal,
  parseCancelAcknowledgement,
  (acknowledgement) => {
    if (acknowledgement.job_id !== jobId) throw new Error('Character cancellation acknowledgement has a different job identity')
  },
)

export const isTerminalCharacterJob = (job: CharacterJob): boolean =>
  job.state === 'completed' || job.state === 'partial' || job.state === 'failed' || job.state === 'cancelled'

export const hasSameCharacterRequestIdentity = (
  left: CharacterPublicRequest,
  right: CharacterPublicRequest,
): boolean => left.format === right.format && left.request_id === right.request_id
