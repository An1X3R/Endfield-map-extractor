import type { ExportMode } from '../../types'
import type { BackendRequestErrorKind } from '../../lib/backendClient'

export type InstancedSceneDiagnostic = {
  kind: BackendRequestErrorKind | 'application'
  method: string | null
  path: string | null
  status: number | null
  responseBody: string | null
  detail: string
}

export type InstancedSceneExportMode = ExportMode

export type InstancedSceneExportGroup =
  | 'building'
  | 'prop'
  | 'lighting'
  | 'particle'
  | 'vegetation'
  | 'terrain'
  | 'road'
  | 'water'
  | 'effects'
  | 'unknown'

export type InstancedSceneForm = 'maze' | 'water' | 'tower' | 'office' | 'ship' | 'blueprint'

export type InstancedSceneExportCapability = {
  ready: boolean
  reason: string
  export_modes: InstancedSceneExportMode[]
}

export type InstancedSceneSummary = {
  scene_id: string
  name: string
  group_id: string
  form: InstancedSceneForm
  export_capability: InstancedSceneExportCapability
}

export type InstancedSceneCatalog = {
  format: 'InstancedSceneCatalog/1'
  catalog_id: string
  first_run_identity: string | null
  availability: {
    ready: boolean
    reason: string
  }
  capabilities: {
    export_modes: InstancedSceneExportMode[]
    requires_first_run: boolean
  }
  groups: Array<{
    group_id: string
    name: string
  }>
  scenes: InstancedSceneSummary[]
}

export type InstancedSceneDimensions = {
  x: number
  y: number
  z: number
}

export type InstancedSceneDetail = {
  format: 'InstancedSceneDetail/1'
  scene_id: string
  catalog_id: string
  first_run_identity: string | null
  name: string
  group_id: string
  source_label: string
  dimensions: InstancedSceneDimensions | null
  component_counts: Record<InstancedSceneExportGroup, number | null>
  export_capability: InstancedSceneExportCapability
  limitations: string[]
}

export type InstancedScenePreview = {
  format: 'InstancedScenePreview/1'
  scene_id: string
  catalog_id: string
  first_run_identity: string | null
  status: 'ready' | 'missing' | 'unavailable'
  scope: 'whole_scene' | 'bounded_region' | null
  reason: string
  asset: null | {
    url: string
    mime: 'image/png' | 'image/jpeg'
    bytes: number
    sha256: string
    width: number
    height: number
  }
}

export type InstancedSceneDevelopmentPreview = {
  catalog: InstancedSceneCatalog
  details: InstancedSceneDetail[]
  previews: InstancedScenePreview[]
}

export type InstancedSceneSourcePaths = {
  game_root: string
  cache_root: string
  blender_exe: string
}

export type InstancedSceneExportRequest = {
  format: 'InstancedSceneExportRequest/1'
  request_id: string
  scene_id: string
  catalog_id: string
  first_run_identity: string | null
  export_mode: InstancedSceneExportMode
  export_groups: InstancedSceneExportGroup[]
  effects_mode: 'full_system_by_anchor'
  source: InstancedSceneSourcePaths
  output: {
    root: string
    label: string
  }
}

export type InstancedSceneValidationIssue = {
  code: string
  message: string
  field: string | null
}

export type InstancedSceneExportValidation = {
  format: 'InstancedSceneExportValidation/1'
  status: 'valid' | 'invalid'
  canonical_request: InstancedSceneExportRequest
  issues: InstancedSceneValidationIssue[]
  warnings: string[]
}

export type InstancedSceneJobState =
  | 'queued'
  | 'preparing'
  | 'extracting'
  | 'building'
  | 'auditing'
  | 'completed'
  | 'partial'
  | 'failed'
  | 'cancelled'

export type InstancedSceneJobPhase =
  | 'queued'
  | 'preparing'
  | 'extracting'
  | 'building'
  | 'auditing'
  | 'completed'
  | 'partial'
  | 'failed'
  | 'cancelled'

export type InstancedSceneExportFile = {
  relative_path: string
  role: 'scene' | 'audit' | 'plan' | 'manifest' | 'data_package'
  bytes: number
  sha256: string
}

export type InstancedSceneComponentSummary = Record<InstancedSceneExportGroup, {
  exported: number | null
  pending: number | null
  unsupported: number | null
}>

export type InstancedScenePendingItem = {
  group_id: InstancedSceneExportGroup
  code: string
  message: string
}

export type InstancedSceneExportResult = {
  status: 'completed' | 'partial'
  output_directory: string
  files: InstancedSceneExportFile[]
  component_summary: InstancedSceneComponentSummary
  pending: InstancedScenePendingItem[]
}

export type InstancedSceneJobError = {
  code: string
  message: string
  retryable: boolean
  stage: string | null
}

export type InstancedSceneJob = {
  format: 'InstancedSceneJob/1'
  job_id: string
  state: InstancedSceneJobState
  phase: InstancedSceneJobPhase
  progress: number
  message: string
  cancel_requested: boolean
  request: InstancedSceneExportRequest
  result: InstancedSceneExportResult | null
  error: InstancedSceneJobError | null
}

export type InstancedSceneJobEvent = {
  sequence: number
  timestamp: string
  state: InstancedSceneJobState
  phase: InstancedSceneJobPhase
  progress: number
  message: string
  cancel_requested: boolean
}

export type InstancedSceneJobEventsResponse = {
  format: 'InstancedSceneJobEvents/1'
  job_id: string
  events: InstancedSceneJobEvent[]
  next_after: number
}

export type InstancedSceneCancelAcknowledgement = {
  format: 'InstancedSceneJobCancelAck/1'
  job_id: string
  accepted: true
  state: 'accepted' | 'already_terminal'
}
