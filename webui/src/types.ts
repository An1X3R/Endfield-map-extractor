import type { BoundsXZ, MapId } from './lib/mapChunking'

export type { MapId } from './lib/mapChunking'

export type LayerId = 'terrain' | 'instances' | 'water' | 'effects'

export type LayerState = Record<LayerId, boolean>

export type ScanState = 'idle' | 'scanning' | 'identified'

export type FirstRunState =
  | 'idle'
  | 'queued'
  | 'running'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | 'completed'

export type FirstRunRequest = {
  format: 'EndfieldWebUIFirstRunRequest/1'
  game_root: string
  export_root: string
  cache_root?: string
  run_name: string
}

export type FirstRunCanonicalRequest = Omit<FirstRunRequest, 'cache_root'> & {
  cache_root: string
}

export type FirstRunError = {
  code: string
  message: string
  retryable: boolean
  details?: Record<string, unknown>
}

export type FirstRunCapabilities = {
  map_dataset: string
  asset_resolution: Record<string, string>
  blender_build: string
  profile_scan: string
}

export type FirstRunStatus = {
  format: 'EndfieldWebUIFirstRunStatus/1'
  state: FirstRunState
  phase: string
  progress: number
  progress_scope: 'phase'
  phase_index: number | null
  phase_count: number
  message: string
  ready: boolean
  cancel_requested: boolean
  resume_available: boolean
  actions: {
    can_validate: boolean
    can_start: boolean
    can_resume: boolean
    can_cancel: boolean
  }
  defaults: Partial<FirstRunCanonicalRequest>
  request?: FirstRunCanonicalRequest
  request_identity?: { run_name: string; sha256: string }
  capabilities: FirstRunCapabilities
  result?: Record<string, unknown>
  error?: FirstRunError | null
  disposition?: 'already_ready'
}

export type FirstRunValidation = {
  format: 'EndfieldWebUIFirstRunValidation/1'
  status: 'valid' | 'incomplete'
  game_read_only: true
  checks: Record<string, boolean>
  missing: string[]
  canonical: FirstRunCanonicalRequest
  space: Record<'cache' | 'export', {
    status: 'baseline_estimate' | 'unknown'
    path: string
    required_bytes: number
    free_bytes: number | null
    warning: boolean
  }>
  space_warning: boolean
  prerequisites: {
    git_available: boolean
    dotnet_available: boolean
    network_required_for_initial_helper_checkout: boolean
    helper_commit_is_pinned: boolean
  }
}

export type FirstRunEvent = {
  sequence: number
  source_sequence: number | null
  phase: string
  progress: number
  progress_scope: 'phase'
  phase_index: number | null
  phase_count: number
  message?: string
}

export type JobPhase =
  | 'idle'
  | 'queued'
  | 'preparing'
  | 'extracting'
  | 'building'
  | 'auditing'
  | 'completed'
  | 'cancelled'

export type LogTone = 'neutral' | 'accent' | 'success' | 'warning'

export type LogEntry = {
  id: number
  time: string
  code: string
  message: string
  tone: LogTone
}

export type MapDefinition = {
  id: MapId
  shortLabel: string
  title: string
  chineseName: string
  code: string
  worldBounds: BoundsXZ
  grid: number
  direction: string
  tagline: string
  description: string
  warning: string
  warningTone: 'candidate' | 'stable'
  coordinates: string
}
