import { BackendRequestError, requestJson } from '../../lib/backendClient'
import type {
  AssetArtifact, AssetCancelAcknowledgement, AssetCatalogByKind, AssetExportRequest, AssetExportResult,
  AssetExportSelection, AssetIllustrationResult, AssetJob, AssetKind, AssetModelResult,
  AssetRequestValidation, DisplayPropBinding, DisplayPropCatalog, IllustrationArtifact,
  IllustrationCatalog, TerminalAssetJob, UnresolvedDisplayPropBinding, WeaponCatalog, WeaponEntry,
} from './types'

type JsonValue = null | string | number | boolean | JsonValue[] | { [key: string]: JsonValue }
type JsonObject = { [key: string]: JsonValue }
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const fail = (field: string, expected: string): never => { throw new TypeError(`${field}: expected ${expected}`) }
const object = (value: JsonValue | undefined, field: string): JsonObject =>
  typeof value === 'object' && value !== null && !Array.isArray(value) ? value : fail(field, 'an object')
const text = (value: JsonValue | undefined, field: string): string =>
  typeof value === 'string' && value.trim() ? value : fail(field, 'non-empty text')
const nullableText = (value: JsonValue | undefined, field: string): string | null => value === null ? null : text(value, field)
const string = (value: JsonValue | undefined, field: string): string => typeof value === 'string' ? value : fail(field, 'text')
const number = (value: JsonValue | undefined, field: string): number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : fail(field, 'a finite nonnegative number')
const integer = (value: JsonValue | undefined, field: string): number => {
  const parsed = number(value, field)
  return Number.isInteger(parsed) ? parsed : fail(field, 'an integer')
}
const choice = <T extends string>(value: JsonValue | undefined, field: string, choices: readonly T[]): T =>
  choices.includes(value as T) ? value as T : fail(field, choices.join(' | '))
const array = <T>(value: JsonValue | undefined, field: string, parse: (item: JsonValue, field: string) => T): T[] =>
  Array.isArray(value) ? value.map((item, index) => parse(item, `${field}[${index}]`)) : fail(field, 'an array')
const uuid = (value: JsonValue | undefined, field: string): string => {
  const parsed = text(value, field)
  return UUID.test(parsed) ? parsed : fail(field, 'a canonical lowercase UUID')
}
const hash = (value: JsonValue | undefined, field: string): string => {
  const parsed = text(value, field)
  return /^[0-9a-f]{64}$/.test(parsed) ? parsed : fail(field, 'a SHA-256 hash')
}
const kind = (value: JsonValue | undefined, field: string): AssetKind => choice(value, field, ['weapon', 'illustration', 'display_prop'])
const provider = (value: JsonValue | undefined, field: string): 'ready' | 'missing' | 'ambiguous' => choice(value, field, ['ready', 'missing', 'ambiguous'])
const variant = (value: JsonValue | undefined, field: string): 'base' | 'refined_candidate' => choice(value, field, ['base', 'refined_candidate'])

export const parseAssetExportRequest = (value: JsonValue): AssetExportRequest => {
  const row = object(value, 'request')
  const parsedKind = kind(row.kind, 'request.kind')
  const itemIds = array(row.item_ids, 'request.item_ids', text)
  if (!itemIds.length || new Set(itemIds).size !== itemIds.length) fail('request.item_ids', 'distinct non-empty selectors')
  const base = {
    format: choice(row.format, 'request.format', ['AssetExportRequest/1']),
    request_id: uuid(row.request_id, 'request.request_id'), catalog_id: text(row.catalog_id, 'request.catalog_id'), item_ids: itemIds,
  }
  if (parsedKind === 'weapon') return { ...base, kind: parsedKind, variant: variant(row.variant, 'request.variant') }
  if ('variant' in row) fail('request.variant', 'no variant for illustrations or display props')
  return { ...base, kind: parsedKind }
}

export const createAssetExportRequest = (selection: AssetExportSelection): AssetExportRequest =>
  parseAssetExportRequest({ ...selection, format: 'AssetExportRequest/1', request_id: crypto.randomUUID() })

const parseWeapon = (value: JsonValue, field: string): WeaponEntry => {
  const row = object(value, field)
  return {
    weapon_id: text(row.weapon_id, `${field}.weapon_id`), name: nullableText(row.name, `${field}.name`),
    name_status: choice(row.name_status, `${field}.name_status`, ['resolved', 'missing_item', 'missing_text']),
    rarity: integer(row.rarity, `${field}.rarity`), weapon_type: integer(row.weapon_type, `${field}.weapon_type`),
    model_status: choice(row.model_status, `${field}.model_status`, ['resolved', 'missing_resource', 'ambiguous_resource', 'missing_provider', 'ambiguous_provider']),
    variants: array(row.variants, `${field}.variants`, (value, field) => {
      const row = object(value, field)
      const selected = variant(row.variant, `${field}.variant`)
      return {
        item_id: text(row.item_id, `${field}.item_id`), variant: selected, resource_path: text(row.resource_path, `${field}.resource_path`),
        semantic_status: choice(row.semantic_status, `${field}.semantic_status`, selected === 'base' ? ['source_base'] : ['unverified_candidate']),
      }
    }),
  }
}
const parseBinding = (value: JsonValue, field: string): DisplayPropBinding => {
  const row = object(value, field)
  return {
    character_id: text(row.character_id, `${field}.character_id`), character_name: nullableText(row.character_name, `${field}.character_name`),
    prefab_path: text(row.prefab_path, `${field}.prefab_path`), mount_field: choice(row.mount_field, `${field}.mount_field`, ['mountPoint', 'boneName']),
    mount_point: string(row.mount_point, `${field}.mount_point`), item_index: integer(row.item_index, `${field}.item_index`),
    evidence: 'CharacterDisplayConfig.decoItemConfig.decoItemData',
  }
}
const parseUnresolvedBinding = (value: JsonValue, field: string): UnresolvedDisplayPropBinding => {
  const row = object(value, field)
  const { evidence: _evidence, ...base } = parseBinding(value, field)
  return { ...base, character_name_id: nullableText(row.character_name_id, `${field}.character_name_id`),
    resource_paths: array(row.resource_paths, `${field}.resource_paths`, text),
    status: choice(row.status, `${field}.status`, ['missing_resource', 'ambiguous_resource']) }
}
const numberPair = (value: JsonValue, field: string): [number, number] => {
  const values = array(value, field, integer)
  return values.length === 2 ? [values[0], values[1]] : fail(field, 'a pair of integers')
}
export const parseAssetCatalog = (value: JsonValue): WeaponCatalog | IllustrationCatalog | DisplayPropCatalog => {
  const row = object(value, 'catalog')
  const parsedKind = kind(row.kind, 'catalog.kind')
  const catalogId = text(row.catalog_id, 'catalog.catalog_id')
  if (parsedKind === 'weapon') {
    const summary = object(row.summary, 'catalog.summary')
    return {
      kind: parsedKind, format: choice(row.format, 'catalog.format', ['EndfieldWeaponCatalog/1']), catalog_id: catalogId,
      entries: array(row.entries, 'catalog.entries', parseWeapon),
      summary: {
        weapon_count: integer(summary.weapon_count, 'summary.weapon_count'), resolved_names: integer(summary.resolved_names, 'summary.resolved_names'),
        resolved_models: integer(summary.resolved_models, 'summary.resolved_models'), missing_models: integer(summary.missing_models, 'summary.missing_models'),
        ambiguous_models: integer(summary.ambiguous_models, 'summary.ambiguous_models'), refined_candidate_weapons: integer(summary.refined_candidate_weapons, 'summary.refined_candidate_weapons'),
        rarity_counts: array(summary.rarity_counts, 'summary.rarity_counts', numberPair), weapon_type_counts: array(summary.weapon_type_counts, 'summary.weapon_type_counts', numberPair),
        duplicate_name_groups: array(summary.duplicate_name_groups, 'summary.duplicate_name_groups', (value, field): [string, string[]] => {
          if (!Array.isArray(value) || value.length !== 2) return fail(field, 'a name and ID list pair')
          return [text(value[0], `${field}.name`), array(value[1], `${field}.ids`, text)]
        }),
      },
    }
  }
  if (parsedKind === 'illustration') return {
    kind: parsedKind, format: choice(row.format, 'catalog.format', ['EndfieldIllustrationCatalog/1']), catalog_id: catalogId,
    entries: array(row.entries, 'catalog.entries', (value, field) => {
      const row = object(value, field)
      return { illustration_id: text(row.illustration_id, `${field}.illustration_id`), character_id: nullableText(row.character_id, `${field}.character_id`),
        name: nullableText(row.name, `${field}.name`), resource_path: text(row.resource_path, `${field}.resource_path`), provider_status: provider(row.provider_status, `${field}.provider_status`) }
    }),
  }
  return {
    kind: parsedKind, format: choice(row.format, 'catalog.format', ['EndfieldDisplayPropCatalog/1']), catalog_id: catalogId,
    entries: array(row.entries, 'catalog.entries', (value, field) => {
      const row = object(value, field)
      return { prop_id: text(row.prop_id, `${field}.prop_id`), resource_path: text(row.resource_path, `${field}.resource_path`),
        provider_status: provider(row.provider_status, `${field}.provider_status`), geometry_status: choice(row.geometry_status, `${field}.geometry_status`, ['not_verified']),
        bindings: array(row.bindings, `${field}.bindings`, (value, field) => {
          const binding = object(value, field)
          choice(binding.evidence, `${field}.evidence`, ['CharacterDisplayConfig.decoItemConfig.decoItemData'])
          return parseBinding(value, field)
        }) }
    }), unresolved_bindings: array(row.unresolved_bindings, 'catalog.unresolved_bindings', parseUnresolvedBinding),
  }
}
const artifact = (value: JsonValue | undefined, field: string): AssetArtifact => {
  const row = object(value, field)
  return { path: text(row.path, `${field}.path`), bytes: integer(row.bytes, `${field}.bytes`), sha256: hash(row.sha256, `${field}.sha256`) }
}
const illustrationArtifact = (value: JsonValue, field: string): IllustrationArtifact => {
  const row = object(value, field)
  return {
    illustration_id: text(row.illustration_id, `${field}.illustration_id`), character_id: nullableText(row.character_id, `${field}.character_id`),
    name: nullableText(row.name, `${field}.name`), resource_path: text(row.resource_path, `${field}.resource_path`), source: text(row.source, `${field}.source`),
    path_id: text(row.path_id, `${field}.path_id`), relative_path: text(row.relative_path, `${field}.relative_path`),
    width: integer(row.width, `${field}.width`), height: integer(row.height, `${field}.height`), mode: text(row.mode, `${field}.mode`),
    alpha_extrema: numberPair(row.alpha_extrema ?? null, `${field}.alpha_extrema`), bytes: integer(row.bytes, `${field}.bytes`), sha256: hash(row.sha256, `${field}.sha256`),
  }
}
const resultItem = (value: JsonValue, field: string): AssetModelResult | AssetIllustrationResult => {
  const row = object(value, field)
  const format = choice(row.format, `${field}.format`, ['EndfieldWeaponExport/1', 'EndfieldDisplayPropExport/1', 'EndfieldIllustrationExport/1'])
  if (format === 'EndfieldIllustrationExport/1') return {
    format, status: choice(row.status, `${field}.status`, ['completed']), catalog_id: text(row.catalog_id, `${field}.catalog_id`),
    files: array(row.files, `${field}.files`, illustrationArtifact), elapsed_seconds: number(row.elapsed_seconds, `${field}.elapsed_seconds`),
  }
  const base = { status: choice(row.status, `${field}.status`, ['completed', 'partial']), blend: artifact(row.blend, `${field}.blend`),
    preparation_receipt: text(row.preparation_receipt, `${field}.preparation_receipt`), build_receipt: text(row.build_receipt, `${field}.build_receipt`),
    reopen_audit: text(row.reopen_audit, `${field}.reopen_audit`), pending: array(row.pending, `${field}.pending`, text), limitations: array(row.limitations, `${field}.limitations`, text) }
  if (format === 'EndfieldWeaponExport/1') return { ...base, format, weapon_id: text(row.weapon_id, `${field}.weapon_id`), name: nullableText(row.name, `${field}.name`),
    variant: variant(row.variant, `${field}.variant`), variant_semantics: choice(row.variant_semantics, `${field}.variant_semantics`, ['source_base', 'unverified_candidate']),
    catalog_identity: text(row.catalog_identity, `${field}.catalog_identity`), source_resource: text(row.source_resource, `${field}.source_resource`) }
  return { ...base, format, status: choice(row.status, `${field}.status`, ['completed']), catalog_id: text(row.catalog_id, `${field}.catalog_id`), prop_id: text(row.prop_id, `${field}.prop_id`), resource_path: text(row.resource_path, `${field}.resource_path`) }
}
export const parseAssetExportResult = (value: JsonValue | undefined): AssetExportResult => {
  const row = object(value, 'result')
  const result: AssetExportResult = { format: choice(row.format, 'result.format', ['AssetExportResult/1']), kind: kind(row.kind, 'result.kind'),
    status: choice(row.status, 'result.status', ['completed', 'partial']), catalog_id: text(row.catalog_id, 'result.catalog_id'),
    output_directory: text(row.output_directory, 'result.output_directory'), request: parseAssetExportRequest(row.request ?? null), items: array(row.items, 'result.items', resultItem) }
  if (!result.items.length) fail('result.items', 'one or more export receipts')
  for (const item of result.items) {
    if (result.kind === 'weapon') {
      if (item.format !== 'EndfieldWeaponExport/1' || item.catalog_identity !== result.catalog_id || result.request.kind !== 'weapon' ||
        item.variant !== result.request.variant || item.variant_semantics !== (item.variant === 'base' ? 'source_base' : 'unverified_candidate')) fail('result.items', 'weapon receipts matching the selected variant and catalog')
    } else if (result.kind === 'display_prop') {
      if (item.format !== 'EndfieldDisplayPropExport/1' || item.catalog_id !== result.catalog_id || !result.request.item_ids.includes(item.prop_id)) fail('result.items', 'display prop receipts matching selected IDs and catalog')
    } else if (item.format !== 'EndfieldIllustrationExport/1' || item.catalog_id !== result.catalog_id || item.files.some(file => !result.request.item_ids.includes(file.illustration_id))) {
      fail('result.items', 'illustration receipts matching selected IDs and catalog')
    }
  }
  return result
}
export const parseAssetJob = (value: JsonValue): AssetJob => {
  const row = object(value, 'job')
  const request = parseAssetExportRequest(row.request ?? null)
  const jobId = uuid(row.job_id, 'job.job_id')
  const parsedKind = kind(row.kind, 'job.kind')
  const state = choice(row.state, 'job.state', ['queued', 'running', 'completed', 'partial', 'failed', 'cancelled'])
  const progress = number(row.progress, 'job.progress')
  if (progress > 1) fail('job.progress', 'a fraction within [0, 1]')
  if (jobId !== request.request_id || parsedKind !== request.kind) fail('job', 'matching job and request identity')
  const result = row.result === null ? null : parseAssetExportResult(row.result)
  if (state === 'completed' || state === 'partial') {
    if (!result || result.status !== state) fail('job.result', 'a result matching terminal state')
  }
  if (result && (result.kind !== parsedKind || result.catalog_id !== request.catalog_id || JSON.stringify(result.request) !== JSON.stringify(request))) fail('job.result', 'matching request identity')
  let error: AssetJob['error'] = null
  if (row.error !== null) {
    const errorRow = object(row.error, 'job.error')
    error = { message: text(errorRow.message, 'job.error.message'), diagnostic_id: uuid(errorRow.diagnostic_id, 'job.error.diagnostic_id') }
  }
  return { format: choice(row.format, 'job.format', ['AssetJob/1']), job_id: jobId, kind: parsedKind, state,
    phase: text(row.phase, 'job.phase'), progress, message: text(row.message, 'job.message'), request, result, error }
}
export const isTerminalAssetJob = (job: AssetJob): job is TerminalAssetJob => job.state !== 'queued' && job.state !== 'running'

const read = async (path: string, init: RequestInit): Promise<JsonValue> =>
  requestJson(path, init, (payload): payload is JsonValue => payload !== undefined)
const get = async (path: string, signal: AbortSignal): Promise<JsonValue> => {
  for (let attempt = 0; ; attempt += 1) {
    try { return await read(path, { method: 'GET', signal }) }
    catch (error) {
      if (signal.aborted || attempt >= 2 || !(error instanceof BackendRequestError) ||
        !(error.kind === 'request' || error.kind === 'http' && [502, 503, 504].includes(error.status ?? 0))) throw error
      console.warn('asset_api_read_retry', { path, attempt: attempt + 1, status: error.status, message: error.message })
      await new Promise<void>(resolve => setTimeout(resolve, 500 * (attempt + 1)))
    }
  }
}
const post = (path: string, body: AssetExportRequest | Record<string, never>, signal: AbortSignal): Promise<JsonValue> =>
  read(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
export const getAssetCatalog = async <K extends AssetKind>(selectedKind: K, signal: AbortSignal): Promise<AssetCatalogByKind[K]> => {
  const parsed = parseAssetCatalog(await get(`/api/v2/asset-catalogs/${selectedKind}`, signal))
  if (parsed.kind !== selectedKind) fail('catalog.kind', selectedKind)
  return parsed as AssetCatalogByKind[K]
}
export const getAssetJob = async (jobId: string, signal: AbortSignal): Promise<AssetJob> => {
  const parsed = parseAssetJob(await get(`/api/v2/asset-jobs/${uuid(jobId, 'job_id')}`, signal))
  if (parsed.job_id !== jobId) fail('job.job_id', jobId)
  return parsed
}
export const validateAssetRequest = async (request: AssetExportRequest, signal: AbortSignal): Promise<AssetRequestValidation> => {
  const row = object(await post('/api/v2/asset-jobs/validate', request, signal), 'validation')
  const canonical = parseAssetExportRequest(row.canonical_request ?? null)
  if (JSON.stringify(canonical) !== JSON.stringify(request)) fail('validation.canonical_request', 'the submitted canonical request')
  if (!Array.isArray(row.issues) || row.issues.length) fail('validation.issues', 'an empty issue list for a valid request')
  return { format: choice(row.format, 'validation.format', ['AssetRequestValidation/1']), status: choice(row.status, 'validation.status', ['valid']), canonical_request: canonical, issues: [] }
}
export const submitAssetRequest = async (request: AssetExportRequest, signal: AbortSignal): Promise<AssetJob> => {
  const parsed = parseAssetJob(await post('/api/v2/asset-jobs', request, signal))
  if (JSON.stringify(parsed.request) !== JSON.stringify(request)) fail('job.request', 'the submitted canonical request')
  return parsed
}
export const cancelAssetJob = async (jobId: string, signal: AbortSignal): Promise<AssetCancelAcknowledgement> => {
  const row = object(await post(`/api/v2/asset-jobs/${uuid(jobId, 'job_id')}/cancel`, {}, signal), 'cancel')
  if (row.job_id !== jobId || row.accepted !== true) fail('cancel', 'a matching accepted cancellation acknowledgement')
  return { format: choice(row.format, 'cancel.format', ['AssetJobCancelAck/1']), job_id: jobId, accepted: true,
    state: choice(row.state, 'cancel.state', ['accepted', 'already_terminal']) }
}
