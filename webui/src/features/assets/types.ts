export type AssetKind = 'weapon' | 'illustration' | 'display_prop'
export type WeaponVariant = 'base' | 'refined_candidate'
export type AssetProviderStatus = 'ready' | 'missing' | 'ambiguous'
export type WeaponEntry = {
  weapon_id: string
  name: string | null
  name_status: 'resolved' | 'missing_item' | 'missing_text'
  rarity: number
  weapon_type: number
  model_status: 'resolved' | 'missing_resource' | 'ambiguous_resource' | 'missing_provider' | 'ambiguous_provider'
  variants: { item_id: string; variant: WeaponVariant; resource_path: string; semantic_status: 'source_base' | 'unverified_candidate' }[]
}
export type WeaponCatalog = {
  kind: 'weapon'
  format: 'EndfieldWeaponCatalog/1'
  catalog_id: string
  entries: WeaponEntry[]
  summary: {
    weapon_count: number; resolved_names: number; resolved_models: number; missing_models: number
    ambiguous_models: number; refined_candidate_weapons: number
    duplicate_name_groups: [string, string[]][]; rarity_counts: [number, number][]; weapon_type_counts: [number, number][]
  }
}
export type IllustrationEntry = {
  illustration_id: string; character_id: string | null; name: string | null
  resource_path: string; provider_status: AssetProviderStatus
}
export type IllustrationCatalog = {
  kind: 'illustration'; format: 'EndfieldIllustrationCatalog/1'; catalog_id: string; entries: IllustrationEntry[]
}
export type DisplayPropBinding = {
  character_id: string; character_name: string | null; prefab_path: string
  mount_field: 'mountPoint' | 'boneName'; mount_point: string; item_index: number
  evidence: 'CharacterDisplayConfig.decoItemConfig.decoItemData'
}
export type UnresolvedDisplayPropBinding = Omit<DisplayPropBinding, 'evidence'> & {
  character_name_id: string | null; resource_paths: string[]
  status: 'missing_resource' | 'ambiguous_resource'
}
export type DisplayPropEntry = {
  prop_id: string; resource_path: string; provider_status: AssetProviderStatus
  geometry_status: 'not_verified'; bindings: DisplayPropBinding[]
}
export type DisplayPropCatalog = {
  kind: 'display_prop'; format: 'EndfieldDisplayPropCatalog/1'; catalog_id: string
  entries: DisplayPropEntry[]; unresolved_bindings: UnresolvedDisplayPropBinding[]
}
export type AssetCatalogByKind = { weapon: WeaponCatalog; illustration: IllustrationCatalog; display_prop: DisplayPropCatalog }
export type AssetCatalog = AssetCatalogByKind[AssetKind]
export type AssetExportSelection = {
  catalog_id: string; item_ids: string[]
} & ({ kind: 'weapon'; variant: WeaponVariant } | { kind: 'illustration' | 'display_prop' })
export type AssetExportRequest = AssetExportSelection & { format: 'AssetExportRequest/1'; request_id: string }
export type AssetJobState = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled'
export type AssetArtifact = { path: string; bytes: number; sha256: string }
export type IllustrationArtifact = {
  illustration_id: string; character_id: string | null; name: string | null; resource_path: string
  source: string; path_id: string; relative_path: string; width: number; height: number
  mode: string; alpha_extrema: [number, number]; bytes: number; sha256: string
}
type ModelResult = {
  status: 'completed' | 'partial'; blend: AssetArtifact
  preparation_receipt: string; build_receipt: string; reopen_audit: string; pending: string[]; limitations: string[]
}
export type AssetWeaponResult = ModelResult & {
  format: 'EndfieldWeaponExport/1'; weapon_id: string; name: string | null; variant: WeaponVariant
  variant_semantics: 'source_base' | 'unverified_candidate'; catalog_identity: string; source_resource: string
}
export type AssetDisplayPropResult = ModelResult & {
  format: 'EndfieldDisplayPropExport/1'; catalog_id: string; prop_id: string; resource_path: string
}
export type AssetModelResult = AssetWeaponResult | AssetDisplayPropResult
export type AssetIllustrationResult = {
  format: 'EndfieldIllustrationExport/1'; status: 'completed'; catalog_id: string
  files: IllustrationArtifact[]; elapsed_seconds: number
}
export type AssetExportResult = {
  format: 'AssetExportResult/1'; kind: AssetKind; status: 'completed' | 'partial'; catalog_id: string
  output_directory: string; request: AssetExportRequest; items: (AssetModelResult | AssetIllustrationResult)[]
}
export type AssetJob = {
  format: 'AssetJob/1'; job_id: string; kind: AssetKind; state: AssetJobState; phase: string
  progress: number; message: string; request: AssetExportRequest; result: AssetExportResult | null
  error: { message: string; diagnostic_id: string } | null
}
export type TerminalAssetJob = AssetJob & { state: 'completed' | 'partial' | 'failed' | 'cancelled' }
export type AssetRequestValidation = {
  format: 'AssetRequestValidation/1'; status: 'valid'; canonical_request: AssetExportRequest; issues: []
}
export type AssetCancelAcknowledgement = {
  format: 'AssetJobCancelAck/1'; job_id: string; accepted: true; state: 'accepted' | 'already_terminal'
}
