import assert from 'node:assert/strict'
import path from 'node:path'
import { registerHooks } from 'node:module'

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('.') && path.extname(specifier) === '') {
      return nextResolve(`${specifier}.ts`, context)
    }
    return nextResolve(specifier, context)
  },
})

const api = await import('../src/features/instanced-scenes/api.ts')
const sceneHook = await import('../src/features/instanced-scenes/useInstancedScenes.ts')
const groups = [
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

const capability = {
  ready: true,
  reason: '',
  export_modes: ['blend', 'data_package'],
}

const catalog = {
  format: 'InstancedSceneCatalog/1',
  catalog_id: 'catalog-2026-10-08',
  first_run_identity: 'prepared-runtime-identity',
  availability: { ready: true, reason: '' },
  capabilities: { export_modes: ['blend', 'data_package'], requires_first_run: true },
  groups: [{ group_id: 'puzzle', name: 'Puzzle scenes' }],
  scenes: [{
    scene_id: 'scene-001',
    name: 'North Causeway',
    group_id: 'puzzle',
    form: 'maze',
    export_capability: capability,
  }],
}

const detail = {
  format: 'InstancedSceneDetail/1',
  scene_id: 'scene-001',
  catalog_id: catalog.catalog_id,
  first_run_identity: catalog.first_run_identity,
  name: 'North Causeway',
  group_id: 'puzzle',
  source_label: 'Prepared scene catalog',
  dimensions: { x: 80, y: 18, z: 96 },
  component_counts: Object.fromEntries(groups.map((group) => [group, group === 'unknown' ? null : 0])),
  export_capability: capability,
  limitations: [],
}

const preview = {
  format: 'InstancedScenePreview/1',
  scene_id: 'scene-001',
  catalog_id: catalog.catalog_id,
  first_run_identity: catalog.first_run_identity,
  status: 'ready',
  scope: 'whole_scene',
  reason: '',
  asset: {
    url: '/api/v2/instanced-scenes/scene-001/preview.png',
    mime: 'image/png',
    bytes: 4096,
    sha256: 'a'.repeat(64),
    width: 640,
    height: 480,
  },
}

const request = {
  format: 'InstancedSceneExportRequest/1',
  request_id: '5ef47291-493f-4907-b9b9-41f942ac30f5',
  scene_id: 'scene-001',
  catalog_id: catalog.catalog_id,
  first_run_identity: catalog.first_run_identity,
  export_mode: 'blend',
  export_groups: ['building', 'terrain'],
  effects_mode: 'full_system_by_anchor',
  source: {
    game_root: 'F:\\Games\\Endfield',
    cache_root: 'F:\\EndfieldCache',
    blender_exe: 'C:\\Blender\\blender.exe',
  },
  output: { root: 'F:\\EndfieldExports', label: 'north_causeway' },
}

const componentSummary = Object.fromEntries(groups.map((group) => [group, {
  exported: group === 'unknown' ? null : 0,
  pending: group === 'unknown' ? null : 0,
  unsupported: group === 'unknown' ? null : 0,
}]))

const completedBlendJob = {
  format: 'InstancedSceneJob/1',
  job_id: request.request_id,
  state: 'completed',
  phase: 'completed',
  progress: 1,
  message: 'Scene export completed.',
  cancel_requested: false,
  request,
  result: {
    status: 'completed',
    output_directory: 'F:\\EndfieldExports\\north_causeway',
    files: [
      { relative_path: 'north_causeway.blend', role: 'scene', bytes: 1024, sha256: 'b'.repeat(64) },
      { relative_path: 'scene_audit.json', role: 'audit', bytes: 512, sha256: 'c'.repeat(64) },
    ],
    component_summary: componentSummary,
    pending: [],
  },
  error: null,
}

const developmentPreview = {
  catalog,
  details: [detail],
  previews: [preview],
}

assert.equal(api.isInstancedSceneCatalog(catalog), true)
assert.equal(api.isInstancedSceneDetail(detail), true)
assert.equal(api.isInstancedScenePreview(preview), true)
assert.equal(api.isInstancedSceneDevelopmentPreview(developmentPreview), true)
assert.equal(api.isInstancedSceneExportRequest(request), true)
assert.equal(api.isInstancedSceneJob(completedBlendJob), true)
assert.equal(api.isInstancedSceneCatalogForIdentity(catalog, 'prepared-runtime-identity'), true)
assert.equal(api.isInstancedSceneCatalogForIdentity(catalog, 'other-runtime-identity'), false)
assert.equal(api.isInstancedSceneCatalogForIdentity(catalog, null), false)
assert.equal(api.isInstancedSceneDetailForId(detail, 'scene-001'), true)
assert.equal(api.isInstancedSceneDetailForId(detail, 'scene-002'), false)
assert.equal(api.isInstancedScenePreviewForId(preview, 'scene-001'), true)
assert.equal(api.isInstancedScenePreviewForId(preview, 'scene-002'), false)

const validExportValidation = {
  format: 'InstancedSceneExportValidation/1',
  status: 'valid',
  canonical_request: request,
  issues: [],
  warnings: [],
}
assert.equal(api.isInstancedSceneExportValidationForRequest(validExportValidation, request), true)
for (const field of ['scene_id', 'catalog_id', 'first_run_identity']) {
  const mismatchedValidation = structuredClone(validExportValidation)
  mismatchedValidation.canonical_request[field] = `different-${field}`
  assert.equal(api.isInstancedSceneExportValidationForRequest(mismatchedValidation, request), false, field)
}
assert.equal(api.isInstancedSceneJobForId(completedBlendJob, request.request_id), true)
assert.equal(api.isInstancedSceneJobForId(completedBlendJob, '5ef47291-493f-4907-b9b9-41f942ac30f6'), false)
assert.equal(api.isInstancedSceneJobForRequest(completedBlendJob, request), true)
for (const field of ['scene_id', 'catalog_id', 'first_run_identity']) {
  const mismatchedJob = structuredClone(completedBlendJob)
  mismatchedJob.request[field] = `different-${field}`
  assert.equal(api.isInstancedSceneJobForRequest(mismatchedJob, request), false, field)
}
const changedJobRequest = structuredClone(completedBlendJob)
changedJobRequest.request.output.root = 'F:\\OtherExports'
assert.equal(api.isInstancedSceneJobForRequest(changedJobRequest, request), false)

const jobIdDoesNotMatchRequest = structuredClone(completedBlendJob)
jobIdDoesNotMatchRequest.job_id = '5ef47291-493f-4907-b9b9-41f942ac30f6'
assert.equal(api.isInstancedSceneJob(jobIdDoesNotMatchRequest), false)

const incompleteCatalog = structuredClone(catalog)
delete incompleteCatalog.capabilities
assert.equal(api.isInstancedSceneCatalog(incompleteCatalog), false)

const duplicateSceneCatalog = structuredClone(catalog)
duplicateSceneCatalog.scenes.push(structuredClone(duplicateSceneCatalog.scenes[0]))
assert.equal(api.isInstancedSceneCatalog(duplicateSceneCatalog), false)

const impossibleReadyCapability = structuredClone(catalog)
impossibleReadyCapability.scenes[0].export_capability.export_modes = []
assert.equal(api.isInstancedSceneCatalog(impossibleReadyCapability), false)

const unsafePreviewUrls = [
  'https://example.invalid/scene.png',
  '/api/v2/../v2/scene.png',
  '/api/v2/%2e%2e/v2/scene.png',
  '/api/v2/scene%2fsecret.png',
  '/api/v2/scene%5csecret.png',
  '/api/v2/scene%0a.png',
  '/api/v2/scene\u0001.png',
]
for (const url of unsafePreviewUrls) assert.equal(api.isSameOriginApiAssetUrl(url), false, url)

const mismatchedPreview = structuredClone(preview)
mismatchedPreview.asset.url = unsafePreviewUrls[2]
assert.equal(api.isInstancedScenePreview(mismatchedPreview), false)

const staleDetail = structuredClone(detail)
staleDetail.catalog_id = 'catalog-from-another-snapshot'
assert.equal(api.isInstancedSceneDetailForCatalog(staleDetail, 'scene-001', catalog.catalog_id, catalog.first_run_identity), false)

const stalePreview = structuredClone(preview)
stalePreview.first_run_identity = 'another-preparation'
assert.equal(api.isInstancedScenePreviewForCatalog(stalePreview, 'scene-001', catalog.catalog_id, catalog.first_run_identity), false)

const changedCanonicalRequest = structuredClone(validExportValidation)
changedCanonicalRequest.canonical_request.output.label = 'different-output'
assert.equal(api.isInstancedSceneExportValidationForRequest(changedCanonicalRequest, request), false)

const changedCanonicalMode = structuredClone(validExportValidation)
changedCanonicalMode.canonical_request.export_mode = 'data_package'
assert.equal(api.isInstancedSceneExportValidationForRequest(changedCanonicalMode, request), false)

const changedCanonicalGroups = structuredClone(validExportValidation)
changedCanonicalGroups.canonical_request.export_groups.pop()
assert.equal(api.isInstancedSceneExportValidationForRequest(changedCanonicalGroups, request), false)

const normalizedPathRequest = structuredClone(validExportValidation)
normalizedPathRequest.canonical_request.source.game_root = 'f:/Games/Endfield/'
assert.equal(api.isInstancedSceneExportValidationForRequest(normalizedPathRequest, request), true)

const changedRecoverySource = structuredClone(request)
changedRecoverySource.source.cache_root = 'F:\\DifferentCache'
assert.equal(api.hasSameInstancedSceneRequestSemantics(changedRecoverySource, request), false)

const invalidComponentCount = structuredClone(detail)
invalidComponentCount.component_counts.unknown = -1
assert.equal(api.isInstancedSceneDetail(invalidComponentCount), false)

const officeDetail = {
  ...structuredClone(detail),
  scene_id: 'indie_dg005',
  limitations: ['办公室现有验收仅覆盖室内范围；导出按完整物理场景进行。'],
}
assert.equal(api.isInstancedSceneDetail(officeDetail), true)
officeDetail.limitations = [null]
assert.equal(api.isInstancedSceneDetail(officeDetail), false)

const readyPreviewWithoutScope = structuredClone(preview)
readyPreviewWithoutScope.scope = null
assert.equal(api.isInstancedScenePreview(readyPreviewWithoutScope), false)

const boundedPreviewWithoutReason = structuredClone(preview)
boundedPreviewWithoutReason.scope = 'bounded_region'
boundedPreviewWithoutReason.reason = '   '
assert.equal(api.isInstancedScenePreview(boundedPreviewWithoutReason), false)

const missingPreviewWithScope = {
  format: 'InstancedScenePreview/1',
  scene_id: 'scene-001',
  catalog_id: catalog.catalog_id,
  first_run_identity: catalog.first_run_identity,
  status: 'missing',
  scope: 'bounded_region',
  reason: 'No preview asset is available.',
  asset: null,
}
assert.equal(api.isInstancedScenePreview(missingPreviewWithScope), false)

const unsafeExportFile = structuredClone(completedBlendJob)
unsafeExportFile.result.files[0].relative_path = '../scene.blend'
assert.equal(api.isInstancedSceneJob(unsafeExportFile), false)

const missingCompletedResult = structuredClone(completedBlendJob)
missingCompletedResult.result = null
assert.equal(api.isInstancedSceneJob(missingCompletedResult), false)

const completedBlendWithoutSceneArtifact = structuredClone(completedBlendJob)
completedBlendWithoutSceneArtifact.result.files = completedBlendWithoutSceneArtifact.result.files.filter((file) => file.role !== 'scene')
assert.equal(api.isInstancedSceneJob(completedBlendWithoutSceneArtifact), false)

const outOfRangeProgress = structuredClone(completedBlendJob)
outOfRangeProgress.progress = 1.01
assert.equal(api.isInstancedSceneJob(outOfRangeProgress), false)

const partialJobWithoutEvidence = structuredClone(completedBlendJob)
partialJobWithoutEvidence.state = 'partial'
partialJobWithoutEvidence.phase = 'partial'
partialJobWithoutEvidence.result.status = 'partial'
assert.equal(api.isInstancedSceneJob(partialJobWithoutEvidence), false)

const partialJobWithPending = structuredClone(partialJobWithoutEvidence)
partialJobWithPending.result.pending = [{ group_id: 'building', code: 'unsupported_component', message: 'Component requires an unsupported shader.' }]
partialJobWithPending.result.component_summary.building.pending = 1
assert.equal(api.isInstancedSceneJob(partialJobWithPending), true)

const packageRequest = structuredClone(request)
packageRequest.export_mode = 'data_package'
packageRequest.source.blender_exe = ''
assert.equal(api.isInstancedSceneExportRequest(packageRequest), true)

const blendRequestWithoutBlender = structuredClone(request)
blendRequestWithoutBlender.source.blender_exe = ''
assert.equal(api.isInstancedSceneExportRequest(blendRequestWithoutBlender), false)

const packageJob = structuredClone(completedBlendJob)
packageJob.request = packageRequest
packageJob.result.files = [
  { relative_path: 'manifest.json', role: 'manifest', bytes: 256, sha256: 'd'.repeat(64) },
  { relative_path: 'audit.json', role: 'audit', bytes: 256, sha256: 'e'.repeat(64) },
]
assert.equal(api.isInstancedSceneJob(packageJob), true)

const events = {
  format: 'InstancedSceneJobEvents/1',
  job_id: request.request_id,
  events: [
    { sequence: 1, timestamp: '2026-10-08T00:00:00.000Z', state: 'queued', phase: 'queued', progress: 0, message: 'Queued.', cancel_requested: false },
    { sequence: 2, timestamp: '2026-10-08T00:00:01.000Z', state: 'building', phase: 'building', progress: 0.4, message: 'Building scene.', cancel_requested: false },
  ],
  next_after: 2,
}
assert.equal(api.isInstancedSceneJobEventsResponse(events), true)
assert.equal(api.isInstancedSceneJobEventsForRequest(events, request.request_id, 0), true)
assert.equal(api.isInstancedSceneJobEventsForRequest(events, '5ef47291-493f-4907-b9b9-41f942ac30f6', 0), false)
assert.equal(api.isInstancedSceneJobEventsForRequest(events, request.request_id, 1), false)
const eventsWithoutIdentity = structuredClone(events)
delete eventsWithoutIdentity.job_id
assert.equal(api.isInstancedSceneJobEventsResponse(eventsWithoutIdentity), false)

const recoveryOptions = {
  firstRunReady: true,
  firstRunBusy: false,
  firstRunIdentity: 'prepared-runtime-identity',
  source: { game_root: 'F:/game', cache_root: 'F:/cache', blender_exe: 'F:/Blender/blender.exe' },
  developmentPreview: null,
}
assert.equal(sceneHook.isRecoveryContextReady({ ...recoveryOptions, firstRunReady: false }, request), false)
assert.equal(sceneHook.isRecoveryContextReady({ ...recoveryOptions, firstRunBusy: true }, request), false)
assert.equal(sceneHook.isRecoveryContextReady({ ...recoveryOptions, firstRunIdentity: null }, request), false)
assert.equal(sceneHook.isRecoveryContextReady({ ...recoveryOptions, source: { ...recoveryOptions.source, cache_root: '' } }, request), false)
assert.equal(sceneHook.isRecoveryContextReady({ ...recoveryOptions, developmentPreview: { format: 'not-a-real-preview' } }, request), false)
assert.equal(sceneHook.isRecoveryContextReady({ ...recoveryOptions, source: { ...recoveryOptions.source, blender_exe: '' } }, request), false)
assert.equal(sceneHook.isRecoveryContextReady(recoveryOptions, request), true)
assert.equal(sceneHook.isRecoveryContextReady(
  { ...recoveryOptions, source: { ...recoveryOptions.source, blender_exe: '' } },
  { ...request, export_mode: 'data_package' },
), true)

assert.deepEqual(api.parseInstancedSceneCancelAcknowledgement(202, '', request.request_id), {
  format: 'InstancedSceneJobCancelAck/1',
  job_id: request.request_id,
  accepted: true,
  state: 'accepted',
})
assert.throws(() => api.parseInstancedSceneCancelAcknowledgement(200, '', request.request_id), (error) =>
  error.name === 'BackendRequestError' && error.kind === 'invalid_json')
assert.throws(() => api.parseInstancedSceneCancelAcknowledgement(500, 'backend unavailable', request.request_id), {
  name: 'BackendRequestError',
})
assert.throws(() => api.parseInstancedSceneCancelAcknowledgement(202, '{', request.request_id), {
  name: 'BackendRequestError',
})

process.stdout.write('Instanced scene proposed contract validation passed.\n')
