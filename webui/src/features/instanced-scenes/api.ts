import { BackendRequestError, isJsonObject, requestJson } from '../../lib/backendClient'
import type {
  InstancedSceneCancelAcknowledgement,
  InstancedSceneCatalog,
  InstancedSceneComponentSummary,
  InstancedSceneDetail,
  InstancedSceneDevelopmentPreview,
  InstancedSceneExportFile,
  InstancedSceneExportGroup,
  InstancedSceneExportMode,
  InstancedSceneExportRequest,
  InstancedSceneExportResult,
  InstancedSceneExportValidation,
  InstancedSceneForm,
  InstancedSceneJob,
  InstancedSceneJobError,
  InstancedSceneJobEvent,
  InstancedSceneJobEventsResponse,
  InstancedSceneJobPhase,
  InstancedSceneJobState,
  InstancedScenePendingItem,
  InstancedScenePreview,
  InstancedSceneSummary,
  InstancedSceneValidationIssue,
} from './types'

const EXPORT_MODES: readonly InstancedSceneExportMode[] = ['data_package', 'blend']
const EXPORT_GROUPS: readonly InstancedSceneExportGroup[] = [
  'building',
  'prop',
  'lighting',
  'particle',
  'vegetation',
  'terrain',
  'road',
  'water',
  'effects',
  'unknown',
]
const SCENE_FORMS: readonly InstancedSceneForm[] = ['maze', 'water', 'tower', 'office', 'ship', 'blueprint']
const JOB_STATES: readonly InstancedSceneJobState[] = [
  'queued',
  'preparing',
  'extracting',
  'building',
  'auditing',
  'completed',
  'partial',
  'failed',
  'cancelled',
]
const JOB_PHASES: readonly InstancedSceneJobPhase[] = JOB_STATES
const FILE_ROLES: readonly InstancedSceneExportFile['role'][] = [
  'scene',
  'audit',
  'plan',
  'manifest',
  'data_package',
]
const SAFE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const SAFE_LABEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SHA256 = /^[a-fA-F0-9]{64}$/
const JOBS_PATH = '/api/v2/instanced-scene-jobs'
const CATALOG_PATH = '/api/v2/instanced-scenes'

const isNonEmptyString = (value: unknown): value is string =>
  typeof value === 'string' && value.trim().length > 0

const isNullableNonEmptyString = (value: unknown): value is string | null =>
  value === null || isNonEmptyString(value)

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value)

const isCount = (value: unknown): value is number =>
  Number.isInteger(value) && (value as number) >= 0

const isExportMode = (value: unknown): value is InstancedSceneExportMode =>
  typeof value === 'string' && EXPORT_MODES.includes(value as InstancedSceneExportMode)

const isExportGroup = (value: unknown): value is InstancedSceneExportGroup =>
  typeof value === 'string' && EXPORT_GROUPS.includes(value as InstancedSceneExportGroup)

const isSceneForm = (value: unknown): value is InstancedSceneForm =>
  typeof value === 'string' && SCENE_FORMS.includes(value as InstancedSceneForm)

const hasUniqueValues = (values: readonly string[]): boolean =>
  new Set(values).size === values.length

const isIdentifier = (value: unknown): value is string =>
  typeof value === 'string' && SAFE_IDENTIFIER.test(value)

const isUuid = (value: unknown): value is string =>
  typeof value === 'string' && UUID.test(value)

const isExportModes = (value: unknown): value is InstancedSceneExportMode[] =>
  Array.isArray(value) && value.every(isExportMode) && hasUniqueValues(value)

const isExportCapability = (value: unknown): boolean =>
  isJsonObject(value) &&
  typeof value.ready === 'boolean' &&
  typeof value.reason === 'string' &&
  isExportModes(value.export_modes) &&
  (!value.ready || value.export_modes.length > 0)

export const isInstancedSceneSummary = (value: unknown): value is InstancedSceneSummary =>
  isJsonObject(value) &&
  isIdentifier(value.scene_id) &&
  isNonEmptyString(value.name) &&
  isNonEmptyString(value.group_id) &&
  isSceneForm(value.form) &&
  isExportCapability(value.export_capability)

export const isInstancedSceneCatalog = (value: unknown): value is InstancedSceneCatalog => {
  if (!isJsonObject(value) || value.format !== 'InstancedSceneCatalog/1' ||
    !isIdentifier(value.catalog_id) || !isNullableNonEmptyString(value.first_run_identity) ||
    !isJsonObject(value.availability) || typeof value.availability.ready !== 'boolean' ||
    typeof value.availability.reason !== 'string' || !isJsonObject(value.capabilities) ||
    !isExportModes(value.capabilities.export_modes) ||
    typeof value.capabilities.requires_first_run !== 'boolean' ||
    !Array.isArray(value.groups) || !Array.isArray(value.scenes)) return false

  const groups = value.groups
  if (!groups.every((group) => isJsonObject(group) && isNonEmptyString(group.group_id) && isNonEmptyString(group.name))) return false
  const groupIds = groups.map((group) => group.group_id as string)
  if (!hasUniqueValues(groupIds)) return false

  const scenes = value.scenes
  if (!scenes.every(isInstancedSceneSummary)) return false
  const sceneIds = scenes.map((scene) => scene.scene_id)
  if (!hasUniqueValues(sceneIds)) return false
  const availableGroupIds = new Set(groupIds)
  return scenes.every((scene) => availableGroupIds.has(scene.group_id))
}

const isDimensions = (value: unknown): boolean =>
  isJsonObject(value) &&
  isFiniteNumber(value.x) && value.x > 0 &&
  isFiniteNumber(value.y) && value.y > 0 &&
  isFiniteNumber(value.z) && value.z > 0

const isComponentCounts = (value: unknown): boolean =>
  isJsonObject(value) && EXPORT_GROUPS.every((group) => value[group] === null || isCount(value[group]))

export const isInstancedSceneDetail = (value: unknown): value is InstancedSceneDetail =>
  isJsonObject(value) &&
  value.format === 'InstancedSceneDetail/1' &&
  isIdentifier(value.scene_id) &&
  isIdentifier(value.catalog_id) &&
  isNullableNonEmptyString(value.first_run_identity) &&
  isNonEmptyString(value.name) &&
  isNonEmptyString(value.group_id) &&
  isNonEmptyString(value.source_label) &&
  (value.dimensions === null || isDimensions(value.dimensions)) &&
  isComponentCounts(value.component_counts) &&
  isExportCapability(value.export_capability) &&
  Array.isArray(value.limitations) && value.limitations.every((item) => typeof item === 'string')

export const isSameOriginApiAssetUrl = (value: unknown): value is string => {
  if (typeof value !== 'string' || value.startsWith('//') || value.includes('\\') ||
    /[\u0000-\u001f\u007f]/.test(value) || /%(?:2f|5c|0[0-9a-f]|1[0-9a-f]|7f)/i.test(value)) return false
  const rawPath = value.split(/[?#]/, 1)[0]
  if (!rawPath.startsWith('/api/v2/') && !rawPath.startsWith('/api/v3/')) return false
  let parsed: URL
  try {
    parsed = new URL(value, 'https://endfield.invalid')
  } catch {
    return false
  }
  let decodedPath: string
  try {
    decodedPath = decodeURIComponent(rawPath)
  } catch {
    return false
  }
  const unsafeSegment = decodedPath.split('/').some((segment) => segment === '.' || segment === '..')
  return parsed.pathname === rawPath && !unsafeSegment && !/[\u0000-\u001f\u007f]/.test(decodedPath) &&
    parsed.origin === 'https://endfield.invalid' && !parsed.username && !parsed.password && !parsed.hash
}

const isPreviewAsset = (value: unknown): boolean =>
  isJsonObject(value) &&
  isSameOriginApiAssetUrl(value.url) &&
  (value.mime === 'image/png' || value.mime === 'image/jpeg') &&
  isCount(value.bytes) && value.bytes > 0 &&
  typeof value.sha256 === 'string' && SHA256.test(value.sha256) &&
  isCount(value.width) && value.width > 0 &&
  isCount(value.height) && value.height > 0

export const isInstancedScenePreview = (value: unknown): value is InstancedScenePreview => {
  if (!isJsonObject(value) || value.format !== 'InstancedScenePreview/1' ||
    !isIdentifier(value.scene_id) ||
    !isIdentifier(value.catalog_id) ||
    !isNullableNonEmptyString(value.first_run_identity) ||
    (value.status !== 'ready' && value.status !== 'missing' && value.status !== 'unavailable') ||
    (value.scope !== null && value.scope !== 'whole_scene' && value.scope !== 'bounded_region') ||
    typeof value.reason !== 'string') return false
  if (value.status === 'ready') return value.scope !== null &&
    (value.scope !== 'bounded_region' || isNonEmptyString(value.reason)) &&
    isPreviewAsset(value.asset)
  return value.scope === null && value.asset === null
}

export const isInstancedSceneDevelopmentPreview = (
  value: unknown,
): value is InstancedSceneDevelopmentPreview => {
  if (!isJsonObject(value) || !isInstancedSceneCatalog(value.catalog) ||
    !Array.isArray(value.details) || !value.details.every(isInstancedSceneDetail) ||
    !Array.isArray(value.previews) || !value.previews.every(isInstancedScenePreview)) return false
  const sceneIds = new Set(value.catalog.scenes.map((scene) => scene.scene_id))
  const detailIds = value.details.map((detail) => detail.scene_id)
  const previewIds = value.previews.map((preview) => preview.scene_id)
  return hasUniqueValues(detailIds) && hasUniqueValues(previewIds) &&
    detailIds.every((sceneId) => sceneIds.has(sceneId)) &&
    previewIds.every((sceneId) => sceneIds.has(sceneId))
}

const isSceneSourcePaths = (value: unknown, exportMode: InstancedSceneExportMode): boolean =>
  isJsonObject(value) &&
  isNonEmptyString(value.game_root) &&
  isNonEmptyString(value.cache_root) &&
  typeof value.blender_exe === 'string' &&
  (exportMode !== 'blend' || isNonEmptyString(value.blender_exe))

export const isInstancedSceneExportRequest = (value: unknown): value is InstancedSceneExportRequest => {
  if (!isJsonObject(value) || value.format !== 'InstancedSceneExportRequest/1' ||
    !isUuid(value.request_id) ||
    !isIdentifier(value.scene_id) || !isIdentifier(value.catalog_id) ||
    !isNullableNonEmptyString(value.first_run_identity) || !isExportMode(value.export_mode) ||
    !Array.isArray(value.export_groups) || !value.export_groups.every(isExportGroup) ||
    !hasUniqueValues(value.export_groups) || value.export_groups.length === 0 ||
    value.effects_mode !== 'full_system_by_anchor' || !isSceneSourcePaths(value.source, value.export_mode) ||
    !isJsonObject(value.output) || !isNonEmptyString(value.output.root) ||
    typeof value.output.label !== 'string' || !SAFE_LABEL.test(value.output.label)) return false
  return true
}

const isValidationIssue = (value: unknown): value is InstancedSceneValidationIssue =>
  isJsonObject(value) &&
  isNonEmptyString(value.code) &&
  isNonEmptyString(value.message) &&
  (value.field === null || isNonEmptyString(value.field))

export const isInstancedSceneExportValidation = (value: unknown): value is InstancedSceneExportValidation =>
  isJsonObject(value) &&
  value.format === 'InstancedSceneExportValidation/1' &&
  (value.status === 'valid' || value.status === 'invalid') &&
  isInstancedSceneExportRequest(value.canonical_request) &&
  Array.isArray(value.issues) && value.issues.every(isValidationIssue) &&
  Array.isArray(value.warnings) && value.warnings.every((warning) => typeof warning === 'string') &&
  (value.status === 'valid' ? value.issues.length === 0 : value.issues.length > 0)

const isExportFile = (value: unknown): value is InstancedSceneExportFile => {
  if (!isJsonObject(value) || typeof value.relative_path !== 'string' ||
    !value.relative_path.trim() || value.relative_path.startsWith('/') ||
    value.relative_path.startsWith('\\') || value.relative_path.includes('\\') ||
    /^[A-Za-z]:/.test(value.relative_path) ||
    value.relative_path.split('/').some((segment) => segment === '' || segment === '.' || segment === '..') ||
    typeof value.role !== 'string' || !FILE_ROLES.includes(value.role as InstancedSceneExportFile['role']) ||
    !isCount(value.bytes) || value.bytes === 0 ||
    typeof value.sha256 !== 'string' || !SHA256.test(value.sha256)) return false
  return true
}

const isComponentSummary = (value: unknown): value is InstancedSceneComponentSummary => {
  if (!isJsonObject(value)) return false
  return EXPORT_GROUPS.every((group) => {
    const summary = value[group]
    return isJsonObject(summary) &&
      (summary.exported === null || isCount(summary.exported)) &&
      (summary.pending === null || isCount(summary.pending)) &&
      (summary.unsupported === null || isCount(summary.unsupported))
  })
}

const isPendingItem = (value: unknown): value is InstancedScenePendingItem =>
  isJsonObject(value) &&
  isExportGroup(value.group_id) &&
  isNonEmptyString(value.code) &&
  isNonEmptyString(value.message)

export const isInstancedSceneExportResult = (value: unknown): value is InstancedSceneExportResult => {
  if (!isJsonObject(value) || (value.status !== 'completed' && value.status !== 'partial') ||
    !isNonEmptyString(value.output_directory) || !Array.isArray(value.files) ||
    !value.files.every(isExportFile) || !Array.isArray(value.pending) ||
    !value.pending.every(isPendingItem)) return false
  const componentSummary = value.component_summary
  if (!isComponentSummary(componentSummary)) return false
  const hasPending = value.pending.length > 0 || EXPORT_GROUPS.some((group) => {
    const summary = componentSummary[group]
    return (summary.pending ?? 0) > 0 || (summary.unsupported ?? 0) > 0
  })
  return value.status === 'partial' ? hasPending : !hasPending
}

const hasCompletedArtifacts = (
  request: InstancedSceneExportRequest,
  result: InstancedSceneExportResult,
): boolean => {
  const roles = new Set(result.files.map((file) => file.role))
  if (request.export_mode === 'blend') return roles.has('scene') && roles.has('audit')
  return roles.has('data_package') || (roles.has('manifest') && roles.has('audit'))
}

const isJobError = (value: unknown): value is InstancedSceneJobError =>
  isJsonObject(value) &&
  isNonEmptyString(value.code) &&
  isNonEmptyString(value.message) &&
  typeof value.retryable === 'boolean' &&
  (value.stage === null || isNonEmptyString(value.stage))

const isTerminalState = (state: InstancedSceneJobState): boolean =>
  state === 'completed' || state === 'partial' || state === 'failed' || state === 'cancelled'

export const isInstancedSceneJob = (value: unknown): value is InstancedSceneJob => {
  if (!isJsonObject(value) || value.format !== 'InstancedSceneJob/1' ||
    !isIdentifier(value.job_id) || typeof value.state !== 'string' ||
    !JOB_STATES.includes(value.state as InstancedSceneJobState) ||
    typeof value.phase !== 'string' || !JOB_PHASES.includes(value.phase as InstancedSceneJobPhase) ||
    !isFiniteNumber(value.progress) || value.progress < 0 || value.progress > 1 ||
    typeof value.message !== 'string' || typeof value.cancel_requested !== 'boolean' ||
    !isInstancedSceneExportRequest(value.request) ||
    value.job_id !== value.request.request_id ||
    (value.result !== null && !isInstancedSceneExportResult(value.result)) ||
    (value.error !== null && !isJobError(value.error))) return false

  const state = value.state as InstancedSceneJobState
  const result = value.result as InstancedSceneExportResult | null
  const error = value.error as InstancedSceneJobError | null
  if (!isTerminalState(state)) return result === null && error === null
  if (state === 'completed') {
    return result?.status === 'completed' && error === null && hasCompletedArtifacts(value.request, result)
  }
  if (state === 'partial') return result?.status === 'partial' && error === null
  if (state === 'failed') return error !== null && (result === null || result.status === 'partial')
  return error === null && (result === null || result.status === 'partial')
}

const isJobEvent = (value: unknown): value is InstancedSceneJobEvent =>
  isJsonObject(value) &&
  isCount(value.sequence) &&
  typeof value.timestamp === 'string' && Number.isFinite(Date.parse(value.timestamp)) &&
  typeof value.state === 'string' && JOB_STATES.includes(value.state as InstancedSceneJobState) &&
  typeof value.phase === 'string' && JOB_PHASES.includes(value.phase as InstancedSceneJobPhase) &&
  isFiniteNumber(value.progress) && value.progress >= 0 && value.progress <= 1 &&
  typeof value.message === 'string' &&
  typeof value.cancel_requested === 'boolean'

export const isInstancedSceneJobEventsResponse = (value: unknown): value is InstancedSceneJobEventsResponse => {
  if (!isJsonObject(value) || value.format !== 'InstancedSceneJobEvents/1' ||
    !isUuid(value.job_id) || !Array.isArray(value.events) ||
    !value.events.every(isJobEvent) || !isCount(value.next_after)) return false
  const sequences = value.events.map((event) => event.sequence)
  return sequences.every((sequence, index) => index === 0 || sequence > sequences[index - 1]) &&
    (!sequences.length || value.next_after >= sequences[sequences.length - 1])
}

export const isInstancedSceneCancelAcknowledgement = (
  value: unknown,
): value is InstancedSceneCancelAcknowledgement =>
  isJsonObject(value) &&
  value.format === 'InstancedSceneJobCancelAck/1' &&
  isUuid(value.job_id) &&
  value.accepted === true &&
  (value.state === 'accepted' || value.state === 'already_terminal')

export const isInstancedSceneCatalogForIdentity = (
  payload: unknown,
  expectedFirstRunIdentity: string | null,
): payload is InstancedSceneCatalog =>
  isInstancedSceneCatalog(payload) && payload.first_run_identity === expectedFirstRunIdentity

export const isInstancedSceneDetailForId = (
  payload: unknown,
  sceneId: string,
): payload is InstancedSceneDetail =>
  isInstancedSceneDetail(payload) && payload.scene_id === sceneId

export const isInstancedSceneDetailForCatalog = (
  payload: unknown,
  sceneId: string,
  catalogId: string,
  firstRunIdentity: string | null,
): payload is InstancedSceneDetail =>
  isInstancedSceneDetail(payload) && payload.scene_id === sceneId &&
  payload.catalog_id === catalogId && payload.first_run_identity === firstRunIdentity

export const isInstancedScenePreviewForId = (
  payload: unknown,
  sceneId: string,
): payload is InstancedScenePreview =>
  isInstancedScenePreview(payload) && payload.scene_id === sceneId

export const isInstancedScenePreviewForCatalog = (
  payload: unknown,
  sceneId: string,
  catalogId: string,
  firstRunIdentity: string | null,
): payload is InstancedScenePreview =>
  isInstancedScenePreview(payload) && payload.scene_id === sceneId &&
  payload.catalog_id === catalogId && payload.first_run_identity === firstRunIdentity

const normalizePathForComparison = (value: string): string => {
  const slashesNormalized = value.trim().replace(/\//g, '\\')
  const uncPrefix = slashesNormalized.startsWith('\\\\') ? '\\\\' : ''
  const collapsed = uncPrefix + slashesNormalized.slice(uncPrefix.length).replace(/\\+/g, '\\')
  const isDriveRoot = /^[a-z]:\\$/i.test(collapsed)
  return (isDriveRoot ? collapsed : collapsed.replace(/\\+$/, '')).toLowerCase()
}

export const hasSameInstancedSceneRequestSemantics = (
  left: InstancedSceneExportRequest,
  right: InstancedSceneExportRequest,
): boolean => left.request_id === right.request_id &&
  left.scene_id === right.scene_id &&
  left.catalog_id === right.catalog_id &&
  left.first_run_identity === right.first_run_identity &&
  left.export_mode === right.export_mode &&
  left.export_groups.length === right.export_groups.length &&
  left.export_groups.every((group, index) => group === right.export_groups[index]) &&
  left.effects_mode === right.effects_mode &&
  normalizePathForComparison(left.source.game_root) === normalizePathForComparison(right.source.game_root) &&
  normalizePathForComparison(left.source.cache_root) === normalizePathForComparison(right.source.cache_root) &&
  normalizePathForComparison(left.source.blender_exe) === normalizePathForComparison(right.source.blender_exe) &&
  normalizePathForComparison(left.output.root) === normalizePathForComparison(right.output.root) &&
  left.output.label === right.output.label

export const isInstancedSceneExportValidationForRequest = (
  payload: unknown,
  request: InstancedSceneExportRequest,
): payload is InstancedSceneExportValidation =>
  isInstancedSceneExportValidation(payload) &&
  hasSameInstancedSceneRequestSemantics(payload.canonical_request, request)

export const isInstancedSceneJobForId = (
  payload: unknown,
  jobId: string,
): payload is InstancedSceneJob =>
  isInstancedSceneJob(payload) && payload.job_id === jobId

export const isInstancedSceneJobForRequest = (
  payload: unknown,
  request: InstancedSceneExportRequest,
): payload is InstancedSceneJob =>
  isInstancedSceneJob(payload) &&
  payload.job_id === request.request_id &&
  hasSameInstancedSceneRequestSemantics(payload.request, request)

export const isInstancedSceneJobEventsForRequest = (
  payload: unknown,
  jobId: string,
  after: number,
): payload is InstancedSceneJobEventsResponse =>
  isInstancedSceneJobEventsResponse(payload) &&
  payload.job_id === jobId &&
  payload.events.every((event) => event.sequence > after)

export const parseInstancedSceneCancelAcknowledgement = (
  status: number,
  responseBody: string,
  jobId: string,
): InstancedSceneCancelAcknowledgement => {
  const path = `${JOBS_PATH}/${encodeURIComponent(jobId)}/cancel`
  if (!isUuid(jobId)) {
    throw new BackendRequestError('configuration', 'POST', path, null, null, `Invalid instanced scene job id: ${jobId}`)
  }
  if (status < 200 || status >= 300) {
    throw new BackendRequestError(
      'http',
      'POST',
      path,
      status,
      responseBody,
      `POST ${path} failed with HTTP ${status}: ${responseBody || '(empty response body)'}`,
    )
  }
  if (status === 202 && !responseBody.trim()) {
    return {
      format: 'InstancedSceneJobCancelAck/1',
      job_id: jobId,
      accepted: true,
      state: 'accepted',
    }
  }

  let payload: unknown
  try {
    payload = JSON.parse(responseBody) as unknown
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new BackendRequestError(
      'invalid_json',
      'POST',
      path,
      status,
      responseBody,
      `POST ${path} returned invalid cancellation JSON: ${detail}`,
    )
  }
  if (!isInstancedSceneCancelAcknowledgement(payload) || payload.job_id !== jobId) {
    throw new BackendRequestError(
      'invalid_response',
      'POST',
      path,
      status,
      responseBody,
      `POST ${path} returned an invalid cancellation acknowledgement: ${responseBody}`,
    )
  }
  return payload
}

const postContract = async <TResponse>(
  path: string,
  body: InstancedSceneExportRequest,
  validate: (payload: unknown) => payload is TResponse,
  signal: AbortSignal,
): Promise<TResponse> => {
  let serializedBody: string | undefined
  try {
    serializedBody = JSON.stringify(body)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new BackendRequestError(
      'configuration',
      'POST',
      path,
      null,
      null,
      `POST ${path} request body could not be serialized as JSON: ${detail}`,
    )
  }
  if (typeof serializedBody !== 'string') {
    throw new BackendRequestError(
      'configuration',
      'POST',
      path,
      null,
      null,
      `POST ${path} request body must serialize to JSON text.`,
    )
  }
  return requestJson(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: serializedBody,
    signal,
  }, validate)
}

export const getInstancedSceneCatalog = (
  expectedFirstRunIdentity: string | null,
  signal: AbortSignal,
): Promise<InstancedSceneCatalog> => {
  const validate = (payload: unknown): payload is InstancedSceneCatalog =>
    isInstancedSceneCatalogForIdentity(payload, expectedFirstRunIdentity)
  return requestJson(CATALOG_PATH, { method: 'GET', signal }, validate)
}

export const getInstancedSceneDetail = (
  sceneId: string,
  catalogId: string,
  firstRunIdentity: string | null,
  signal: AbortSignal,
): Promise<InstancedSceneDetail> => {
  if (!isIdentifier(sceneId) || !isIdentifier(catalogId)) {
    throw new BackendRequestError('configuration', 'GET', CATALOG_PATH, null, null, `Invalid instanced scene id: ${sceneId}`)
  }
  const path = `${CATALOG_PATH}/${encodeURIComponent(sceneId)}?catalog_id=${encodeURIComponent(catalogId)}`
  const validate = (payload: unknown): payload is InstancedSceneDetail =>
    isInstancedSceneDetailForCatalog(payload, sceneId, catalogId, firstRunIdentity)
  return requestJson(path, { method: 'GET', signal }, validate)
}

export const getInstancedScenePreview = (
  sceneId: string,
  catalogId: string,
  firstRunIdentity: string | null,
  signal: AbortSignal,
): Promise<InstancedScenePreview> => {
  if (!isIdentifier(sceneId) || !isIdentifier(catalogId)) {
    throw new BackendRequestError('configuration', 'GET', CATALOG_PATH, null, null, `Invalid instanced scene id: ${sceneId}`)
  }
  const path = `${CATALOG_PATH}/${encodeURIComponent(sceneId)}/preview?catalog_id=${encodeURIComponent(catalogId)}`
  const validate = (payload: unknown): payload is InstancedScenePreview =>
    isInstancedScenePreviewForCatalog(payload, sceneId, catalogId, firstRunIdentity)
  return requestJson(path, { method: 'GET', signal }, validate)
}

export const validateInstancedSceneExport = (
  request: InstancedSceneExportRequest,
  signal: AbortSignal,
): Promise<InstancedSceneExportValidation> => {
  const validate = (payload: unknown): payload is InstancedSceneExportValidation =>
    isInstancedSceneExportValidationForRequest(payload, request)
  return postContract(`${JOBS_PATH}/validate`, request, validate, signal)
}

export const submitInstancedSceneExport = (
  request: InstancedSceneExportRequest,
  signal: AbortSignal,
): Promise<InstancedSceneJob> => {
  const validate = (payload: unknown): payload is InstancedSceneJob =>
    isInstancedSceneJobForRequest(payload, request)
  return postContract(JOBS_PATH, request, validate, signal)
}

export const getInstancedSceneJob = (
  jobId: string,
  request: InstancedSceneExportRequest,
  signal: AbortSignal,
): Promise<InstancedSceneJob> => {
  if (!isUuid(jobId) || request.request_id !== jobId) {
    throw new BackendRequestError('configuration', 'GET', JOBS_PATH, null, null, `Invalid instanced scene job id: ${jobId}`)
  }
  const path = `${JOBS_PATH}/${encodeURIComponent(jobId)}`
  const validate = (payload: unknown): payload is InstancedSceneJob =>
    isInstancedSceneJobForRequest(payload, request)
  return requestJson(path, { method: 'GET', signal }, validate)
}

export const getInstancedSceneJobEvents = async (
  jobId: string,
  after: number,
  signal: AbortSignal,
): Promise<InstancedSceneJobEventsResponse> => {
  if (!isUuid(jobId) || !isCount(after)) {
    throw new BackendRequestError(
      'configuration',
      'GET',
      JOBS_PATH,
      null,
      null,
      `Invalid instanced scene event cursor: jobId=${jobId}, after=${after}`,
    )
  }
  const path = `${JOBS_PATH}/${encodeURIComponent(jobId)}/events?after=${after}`
  const validate = (payload: unknown): payload is InstancedSceneJobEventsResponse =>
    isInstancedSceneJobEventsForRequest(payload, jobId, after)
  return requestJson(path, { method: 'GET', signal }, validate)
}

export const requestInstancedSceneJobCancellation = async (
  jobId: string,
  signal: AbortSignal,
): Promise<InstancedSceneCancelAcknowledgement> => {
  if (!isUuid(jobId)) {
    throw new BackendRequestError('configuration', 'POST', JOBS_PATH, null, null, `Invalid instanced scene job id: ${jobId}`)
  }
  const path = `${JOBS_PATH}/${encodeURIComponent(jobId)}/cancel`
  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      signal,
    })
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new BackendRequestError('request', 'POST', path, null, null, `POST ${path} could not reach the backend: ${detail}`)
  }

  let responseBody: string
  try {
    responseBody = await response.text()
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new BackendRequestError(
      'request',
      'POST',
      path,
      response.status,
      null,
      `POST ${path} could not read the HTTP ${response.status} response: ${detail}`,
    )
  }
  return parseInstancedSceneCancelAcknowledgement(response.status, responseBody, jobId)
}
