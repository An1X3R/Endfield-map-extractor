import type { BackendRequestErrorKind } from '../../lib/backendClient'

export type CharacterDiagnostic = {
  kind: BackendRequestErrorKind | 'application'
  method: string | null
  path: string | null
  status: number | null
  responseBody: string | null
  detail: string
}

export type CharacterEntryKind = 'playable' | 'independent_npc'
export type CharacterAssetCategory = 'playable' | 'npc' | 'animal' | 'creature' | 'object'
export type CharacterJobKind = 'preparation' | 'export'
export type CharacterNameStatus = 'resolved' | 'missing' | 'ambiguous'
export type CharacterNpcSourceKind = 'prefab' | 'config_assembled' | 'unresolved'

export type CharacterCapabilities = {
  bundle_prepare: boolean
  blend_export: boolean
  animation: boolean
  raw_expressions: boolean
  basic_mmd: boolean
}

export type CharacterVariant = {
  variant_id: string
  label: string
  variant: string
  geometry_status: 'not_verified'
  part_names?: string[]
  animation_binding_status?: string
}

export type CharacterNameSource = {
  table: 'NpcTemplateGroupTable' | 'NpcTable' | 'EnemyTemplateDisplayInfoTable' | 'EnemyDisplayInfoTable'
  row_key: string
  field: string
  text_id: string
}

export type PlayableCharacterSummary = {
  selection_id: string
  character_id: string
  name: string
  english_name: string | null
  entry_kind: 'playable'
  asset_category: CharacterAssetCategory
  variants: CharacterVariant[]
  capabilities: CharacterCapabilities
  optional_tools_reason: string | null
  unavailable_reason: string | null
}

export type IndependentNpcSummary = {
  selection_id: string
  npc_id: string
  name: string | null
  english_name: null
  entry_kind: 'independent_npc'
  asset_category: CharacterAssetCategory
  name_status: CharacterNameStatus
  source_kind: CharacterNpcSourceKind
  name_source: CharacterNameSource | null
  variants: CharacterVariant[]
  capabilities: CharacterCapabilities
  expression_unavailable_reason: string
  cpu_animation_template: string | null
  animation_scope: string
  animation_catalog_available: boolean
  provider_found: boolean
  optional_tools_reason: string | null
  unavailable_reason: string | null
  assembly_scope: string | null
}

export type CharacterSummary = PlayableCharacterSummary | IndependentNpcSummary

export type CharacterCatalog = {
  format: 'CharacterCatalog/1'
  catalog_id: string
  source_identity: string | null
  availability: {
    ready: boolean
    reason: string | null
  }
  characters: CharacterSummary[]
  profiles: Array<{
    profile_id: string
    name: string
    engines: string[]
  }>
  limitations?: string[]
}

export type CharacterDetail = {
  format: 'CharacterDetail/1'
  catalog_id: string
} & CharacterSummary

export type CharacterReplay = {
  replay_id: string
  label: string
  boundary: string
}

export type CharacterAnimation = {
  action_id: string
  name: string
  binding_status: string
  state_name?: string
  ordinary_replays?: CharacterReplay[]
  native_expression_replays?: CharacterReplay[]
  finalik_replays?: CharacterReplay[]
}

export type CharacterUnsupportedAction = {
  state_name: string
  clip_name: string
  binding_status: string
  reason: string
}

export type CharacterAnimationCoverage = {
  scope: string
  available_action_count: number
  unsupported_action_count: number
  source_state_count?: number
  source_reference_count?: number
  runtime_state_machine_reconstructed?: false
}

export type CharacterCpuActionReference = {
  reference_id: string
  state_name: string
  clip_cab: string
  clip_path_id: string
  logical_provider: string
  execution_available: false
  request_available: boolean
}

export type CharacterCpuActionReferences = {
  format: 'EndfieldNpcCpuActionReferences/1'
  status: 'not_configured' | 'not_present'
  execution_available: false
  actions: []
  unavailable_reason: string
  request_available?: never
} | {
  format: 'EndfieldNpcCpuActionReferences/1'
  status: 'exact_avatar_base_clip_references'
  execution_available: false
  actions: CharacterCpuActionReference[]
  source_identity: string
  selection_id: string
  template_id: string
  part_name: string
  cpu_animation_template: string
  template_namespace: 'Humanoid' | 'Generic'
  cpu_source: {
    cab: string
    path_id: string
    sha256: string
  }
  avatar: {
    cab: string
    path_id: string
    binding_status: string
  }
  request_available: boolean
  request_unavailable_reason: string | null
  limitations: string[]
}

export type CharacterAnimationCatalog = {
  format: 'CharacterAnimationCatalog/1'
  source_identity: string
  selection_id: string
  variant_id: string
  offset: number
  limit: number
  total: number
  animations: CharacterAnimation[]
  limitations: string[]
  binding_status?: string
  unavailable_reason?: string | null
  source_cpu_action_references?: CharacterCpuActionReferences
  unsupported_actions?: CharacterUnsupportedAction[]
  coverage_counts?: CharacterAnimationCoverage
}

export type CharacterPreparationSource = {
  game_root: string
  blender_exe: string
  cache_root: string
  output_root: string
}

export type CharacterPreparationRequest = {
  format: 'CharacterPreparationRequest/1'
  request_id: string
  source: CharacterPreparationSource
}

export type CharacterActionAnimationSelection = {
  action_id: string
  maximum_stored_frames: number
  replay_id?: string
  finalik_replay_id?: string
}

export type CharacterReferenceAnimationSelection = {
  reference_id: string
  maximum_stored_frames: number
}

export type CharacterWeatherEffect = 'wet' | 'snow' | 'rain'
export type CharacterMaterialOptions = {
  mode: 'original' | 'simplified'
  lighting: 'full' | 'simple'
  weather: {
    quality: 'full' | 'simple'
    effects: CharacterWeatherEffect[]
  }
}

export type CharacterExportOptions = {
  appearance?: CharacterAppearanceOptions
  materials?: CharacterMaterialOptions
  raw_expressions?: boolean
  basic_mmd?: boolean
  native_expression_replay_id?: string
  animation?: CharacterActionAnimationSelection | CharacterReferenceAnimationSelection
}

export type CharacterExportRequest = {
  format: 'CharacterExportRequest/1'
  request_id: string
  catalog_id: string
  source_identity: string
  selection_id: string
  variant_id: string
  export_profile_id: 'hgrp-preview'
  output_label: string
  options?: CharacterExportOptions
}

export type CharacterPublicPreparationRequest = {
  format: 'CharacterPreparationRequest/1'
  request_id: string
}

export type CharacterPublicExportRequest = Omit<CharacterExportRequest, 'options'> & {
  options?: CharacterExportOptions
}

export type CharacterPublicRequest = CharacterPublicPreparationRequest | CharacterPublicExportRequest

export type CharacterValidationIssue = {
  code: string
  message: string
  field: string | null
}

export type CharacterRequestValidation = {
  format: 'CharacterRequestValidation/1'
  status: 'valid'
  kind: CharacterJobKind
  canonical_request: CharacterPublicRequest
  issues: CharacterValidationIssue[]
}

export type CharacterDeliveryFile = {
  relative_path: string
  role: 'character' | 'animation' | 'expressions' | 'texture' | 'preview' | 'report'
  bytes: number
  sha256: string
}

export type CharacterJobResult = {
  appearance_options?: CharacterAppearanceOptions
  appearance_report?: CharacterAppearanceReport
  material_options?: CharacterMaterialOptions
  format: 'CharacterExportResult/1' | 'CharacterPreparationResult/1'
  status: 'completed' | 'partial'
  output_directory: string
  files: CharacterDeliveryFile[]
  pending: Array<{
    code: string
    message: string
  }>
}

export type CharacterAppearanceOptions = {
  potential: 'base' | 'maximum'
  fur: 'existing' | 'source_shell' | 'charcoal_embers'
}

export type CharacterAppearanceReport = {
  format: 'EndfieldCharacterAppearance/1'
  status: 'completed' | 'partial'
  potential: {
    requested: CharacterAppearanceOptions['potential']
    status: 'applied' | 'not_applicable' | 'partial'
    scope?: string
    pending?: string[]
  }
  fur: {
    requested: CharacterAppearanceOptions['fur']
    status: 'applied' | 'not_applicable' | 'partial' | 'unchanged'
    pending?: string[]
    verification?: {
      source_shell_count?: number
      artist_applied_count?: number
      artist_skipped_count?: number
      skipped_hidden_source_count?: number
    }
  }
}

export type CharacterJobState = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled'
export type CharacterJobPhase = 'queued' | 'preparing' | 'extracting' | 'building' | 'auditing' | 'completed' | 'partial' | 'failed' | 'cancelled'

export type CharacterJobError = {
  code: string
  message: string
  stage: string
  retryable: boolean
  diagnostic_id: string
}

export type CharacterJob = {
  format: 'CharacterJob/1'
  job_id: string
  kind: CharacterJobKind
  state: CharacterJobState
  phase: CharacterJobPhase
  progress: number
  message: string
  cancel_requested: boolean
  request: CharacterPublicRequest
  result: CharacterJobResult | null
  error: CharacterJobError | null
}

export type CharacterJobEvent = {
  sequence: number
  timestamp: string
  state: CharacterJobState
  phase: CharacterJobPhase
  progress: number
  message: string
  cancel_requested: boolean
}

export type CharacterJobEventsResponse = {
  format: 'CharacterJobEvents/1'
  job_id: string
  events: CharacterJobEvent[]
  next_after: number
  has_more: boolean
}

export type CharacterCancelAcknowledgement = {
  format: 'CharacterJobCancelAck/1'
  job_id: string
  accepted: true
  state: 'accepted' | 'already_terminal'
}
