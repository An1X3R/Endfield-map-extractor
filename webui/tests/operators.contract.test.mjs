import assert from 'node:assert/strict'
import path from 'node:path'
import { registerHooks } from 'node:module'

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('.') && path.extname(specifier) === '') return nextResolve(`${specifier}.ts`, context)
    return nextResolve(specifier, context)
  },
})

const api = await import('../src/features/operators/api.ts')
const variant = { variant_id: 'variant-current', label: 'Source variant', variant: 'npc', geometry_status: 'not_verified' }
const npc = {
  selection_id: 'independent-npc-source', npc_id: 'source', name: null, english_name: null,
  entry_kind: 'independent_npc', asset_category: 'animal', name_status: 'missing', source_kind: 'prefab',
  name_source: null, variants: [variant],
  capabilities: { bundle_prepare: true, blend_export: true, animation: false, raw_expressions: false, basic_mmd: false },
  expression_unavailable_reason: 'No exact expression binding', cpu_animation_template: null,
  animation_scope: 'source_family_candidates_require_avatar_binding', animation_catalog_available: true,
  provider_found: true, optional_tools_reason: null, unavailable_reason: null, assembly_scope: 'one_exact_prefab_part',
}
const catalog = {
  format: 'CharacterCatalog/1', catalog_id: 'catalog-current', source_identity: 'source-current',
  availability: { ready: true, reason: null }, profiles: [{ profile_id: 'hgrp-preview', name: 'Source profile', engines: ['EEVEE'] }],
  characters: ['npc', 'animal', 'creature', 'object'].map(asset_category => ({ ...npc, asset_category })),
}
const parsedCatalog = api.parseCharacterCatalog(catalog)
assert.deepEqual(parsedCatalog.characters.map(item => item.asset_category), ['npc', 'animal', 'creature', 'object'])
assert.equal(parsedCatalog.characters[1].name_status, 'missing')
assert.equal(parsedCatalog.characters[1].name, null)
assert.equal(parsedCatalog.characters[1].animation_catalog_available, true)
assert.equal(parsedCatalog.characters[1].capabilities.animation, false)
assert.equal(api.parseCharacterCatalog({ ...catalog, characters: [{ ...npc, name_status: 'ambiguous' }] }).characters[0].name_status, 'ambiguous')
const { asset_category, ...uncategorized } = npc
assert.throws(() => api.parseCharacterCatalog({ ...catalog, characters: [uncategorized] }), /asset_category/)

const animationCatalog = {
  format: 'CharacterAnimationCatalog/1', source_identity: 'source-current', selection_id: npc.selection_id,
  variant_id: variant.variant_id, offset: 0, limit: 100, total: 1,
  animations: [{ action_id: 'action-current', name: 'Source clip', state_name: 'controller_base_clip_0', binding_status: 'exact_enemy_controller_clip_reference_requires_final_clip_binding' }],
  binding_status: 'source_enemy_controller_clip_references', unavailable_reason: 'Final Avatar binding remains unverified',
  unsupported_actions: [{ state_name: 'controller_base_clip_1', clip_name: 'Empty', binding_status: 'source_enemy_controller_empty_clip_placeholder', reason: 'Native clip contains no motion', extra_source_evidence: 'ignored' }],
  coverage_counts: { scope: 'exact_effective_controller_clip_references', available_action_count: 1, unsupported_action_count: 1, source_reference_count: 2, runtime_state_machine_reconstructed: false },
  limitations: ['Final saved Action validation remains mandatory'],
}
const parsedAnimations = api.parseCharacterAnimationCatalog(animationCatalog)
assert.equal(parsedAnimations.animations[0].binding_status, animationCatalog.animations[0].binding_status)
assert.equal(parsedAnimations.unsupported_actions[0].reason, 'Native clip contains no motion')
assert.equal(parsedAnimations.unavailable_reason, animationCatalog.unavailable_reason)
assert.deepEqual(parsedAnimations.coverage_counts, animationCatalog.coverage_counts)
assert.throws(() => api.parseCharacterAnimationCatalog({ ...animationCatalog, unsupported_actions: [{ ...animationCatalog.unsupported_actions[0], reason: null }] }), /reason/)
assert.throws(() => api.parseCharacterAnimationCatalog({ ...animationCatalog, coverage_counts: { ...animationCatalog.coverage_counts, unsupported_action_count: -1 } }), /unsupported_action_count/)
assert.throws(() => api.parseCharacterAnimationCatalog({ ...animationCatalog, coverage_counts: { ...animationCatalog.coverage_counts, runtime_state_machine_reconstructed: true } }), /runtime_state_machine_reconstructed/)

const request = {
  format: 'CharacterExportRequest/1', request_id: '11111111-1111-4111-8111-111111111111',
  catalog_id: catalog.catalog_id, source_identity: catalog.source_identity, selection_id: npc.selection_id,
  variant_id: variant.variant_id, export_profile_id: 'hgrp-preview', output_label: 'source-asset',
}
const referenceId = 'a'.repeat(64)
for (const animation of [{ action_id: 'action-current', maximum_stored_frames: 2 }, { reference_id: referenceId, maximum_stored_frames: 18000 }]) {
  assert.deepEqual(api.parseCharacterExportRequest({ ...request, options: { animation } }).options.animation, animation)
}
for (const animation of [
  { action_id: 'action-current', reference_id: referenceId, maximum_stored_frames: 300 },
  { maximum_stored_frames: 300 },
  { reference_id: referenceId, maximum_stored_frames: 1 },
  { reference_id: referenceId, maximum_stored_frames: 18001 },
]) assert.throws(() => api.parseCharacterExportRequest({ ...request, options: { animation } }), /animation/)

const { assertMaterialAcknowledgement, originalMaterials, simplifiedMaterialRestriction } = await import('../src/features/operators/materials.ts')
const original = originalMaterials()
assert.doesNotThrow(() => assertMaterialAcknowledgement(undefined, undefined))
assert.doesNotThrow(() => assertMaterialAcknowledgement(original, original))
assert.throws(() => assertMaterialAcknowledgement(original, undefined), /停止提交/)
assert.throws(() => assertMaterialAcknowledgement(original, { ...original, mode: 'simplified' }), /停止提交/)
assert.equal(api.parseCharacterExportRequest(request).options, undefined)
const withMaterials = materials => api.parseCharacterExportRequest({ ...request, options: { materials } }).options.materials
assert.deepEqual(withMaterials(original), original)
for (const lighting of ['simple', 'full']) {
  for (const quality of ['simple', 'full']) {
    for (let mask = 0; mask < 8; mask += 1) {
      const effects = ['wet', 'snow', 'rain'].filter((effect, index) => mask & (1 << index))
      const materials = { mode: 'simplified', lighting, weather: { quality, effects } }
      assert.deepEqual(withMaterials(materials), materials)
      if (lighting !== 'full' || quality !== 'full' || mask !== 7) {
        assert.throws(() => withMaterials({ ...materials, mode: 'original' }), /materials/)
      }
    }
  }
}
assert.deepEqual(withMaterials({ ...original, extra: true, weather: { ...original.weather, effects: ['rain', 'wet', 'snow'], extra: true } }), original)
for (const invalid of [null, {}, { mode: 'simplified' }, { ...original, lighting: 'fast' }, { ...original, weather: { quality: 'full' } }, { ...original, weather: { quality: 'full', effects: ['wet', 'wet', 'snow'] } }, { ...original, weather: { quality: 'full', effects: ['wet', 'snow', 'fog'] } }, { ...original, weather: { quality: 'high', effects: [] } }]) {
  assert.throws(() => withMaterials(invalid), /materials/)
}
assert.equal(simplifiedMaterialRestriction(npc), null)
assert.match(simplifiedMaterialRestriction({ ...npc, source_kind: 'config_assembled' }), /原始材质/)
assert.equal(api.parseCharacterCatalog({ ...catalog, characters: [{ ...npc, source_kind: 'config_assembled' }] }).characters[0].source_kind, 'config_assembled')
const result = { format: 'CharacterExportResult/1', status: 'completed', output_directory: 'exports/asset', files: [], pending: [] }
const job = { format: 'CharacterJob/1', job_id: request.request_id, kind: 'export', state: 'completed', phase: 'completed', progress: 1, message: 'Complete', cancel_requested: false, request: { ...request, options: { materials: original } }, result, error: null }
assert.equal(api.parseCharacterJob(job).result.material_options, undefined)
const actual = { mode: 'simplified', lighting: 'simple', weather: { quality: 'full', effects: [] } }
assert.deepEqual(api.parseCharacterJob({ ...job, result: { ...result, material_options: actual } }).result.material_options, actual)
assert.throws(() => api.parseCharacterJob({ ...job, result: { ...result, material_options: null } }), /result.material_options/)
const combined = { materials: actual, basic_mmd: true, raw_expressions: true, animation: { action_id: 'action-current', maximum_stored_frames: 300 } }
assert.deepEqual(api.parseCharacterExportRequest({ ...request, options: combined }).options, combined)

process.stdout.write('Operator category, capability, rejection, binding and request contracts passed.\n')
