import type { CharacterAnimation, CharacterCatalog, CharacterSummary } from './types'
import { getOperatorPortrait } from './portraits'
import type { OperatorReport } from './OperatorCompletion'

type PreviewEntry = { code: string; name: string; english: string | null; ready: boolean; variants: number; actions: string[] } & ({ npc: true; characterId: null } | { npc: false; characterId: string })
const ENTRIES: PreviewEntry[] = [
  { characterId: 'chr_0004_pelica', code: 'perlica', name: '佩丽卡', english: 'PERLICA', npc: false, ready: true, variants: 2, actions: ['Idle_Loop', 'Walk_Forward_Loop', 'Run_Forward_Loop', 'Attack_01'] },
  { characterId: 'chr_0005_chen', code: 'chenqianyu', name: '陈千语', english: 'CHEN QIANYU', npc: false, ready: true, variants: 1, actions: ['Idle_Loop', 'Walk_Forward_Loop', 'Attack_01'] },
  { characterId: 'chr_0006_wolfgd', code: 'wolfguard', name: '狼卫', english: 'WOLFGUARD', npc: false, ready: true, variants: 1, actions: ['Idle_Loop', 'Run_Forward_Loop', 'Attack_02'] },
  { characterId: 'chr_0003_endminf', code: 'endministrator', name: '管理员', english: 'ENDMINISTRATOR', npc: false, ready: true, variants: 2, actions: ['Idle_Loop', 'Walk_Forward_Loop', 'Attack_01'] },
  { characterId: 'chr_0012_avywen', code: 'avywenna', name: '艾维文娜', english: 'AVYWENNA', npc: false, ready: true, variants: 1, actions: ['Idle_Loop', 'Walk_Forward_Loop'] },
  { characterId: 'chr_0016_laevat', code: 'laevatain', name: '莱万汀', english: 'LAEVATAIN', npc: false, ready: true, variants: 1, actions: ['Idle_Loop', 'Attack_01'] },
  { characterId: 'chr_0009_azrila', code: 'ember', name: '余烬', english: 'EMBER', npc: false, ready: true, variants: 1, actions: ['Idle_Loop', 'Walk_Forward_Loop'] },
  { characterId: 'chr_0015_lifeng', code: 'lifeng', name: '黎风', english: 'LIFENG', npc: false, ready: true, variants: 1, actions: ['Idle_Loop', 'Walk_Forward_Loop', 'Attack_01'] },
  { characterId: 'chr_0002_endminm', code: 'chr_0002_endminm', name: '管理员', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0007_ikut', code: 'chr_0007_ikut', name: '弧光', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0011_seraph', code: 'chr_0011_seraph', name: '赛希', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0013_aglina', code: 'chr_0013_aglina', name: '洁尔佩塔', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0014_aurora', code: 'chr_0014_aurora', name: '昼雪', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0017_yvonne', code: 'chr_0017_yvonne', name: '伊冯', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0018_dapan', code: 'chr_0018_dapan', name: '大潘', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0019_karin', code: 'chr_0019_karin', name: '秋栗', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0020_meurs', code: 'chr_0020_meurs', name: '卡契尔', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0021_whiten', code: 'chr_0021_whiten', name: '埃特拉', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0022_bounda', code: 'chr_0022_bounda', name: '萤石', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0023_antal', code: 'chr_0023_antal', name: '安塔尔', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0024_deepfin', code: 'chr_0024_deepfin', name: '阿列什', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0025_ardelia', code: 'chr_0025_ardelia', name: '艾尔黛拉', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0026_lastrite', code: 'chr_0026_lastrite', name: '别礼', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0027_tangtang', code: 'chr_0027_tangtang', name: '汤汤', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0028_wulfa', code: 'chr_0028_wulfa', name: '洛茜', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0029_pograni', code: 'chr_0029_pograni', name: '骏卫', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0030_zhuangfy', code: 'chr_0030_zhuangfy', name: '庄方宜', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0031_mifu', code: 'chr_0031_mifu', name: '弭弗', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0032_lizhiyan', code: 'chr_0032_lizhiyan', name: '诀', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0033_camille', code: 'chr_0033_camille', name: '卡缪', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0034_typhoea', code: 'chr_0034_typhoea', name: '提弗洛斯', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: 'chr_0035_liino', code: 'chr_0035_liino', name: '梨诺', english: null, npc: false, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: null, code: 'observer', name: '协议观测员', english: null, npc: true, ready: true, variants: 1, actions: ['Idle_Loop'] },
  { characterId: null, code: 'mechanic', name: '基地整备技师', english: null, npc: true, ready: true, variants: 1, actions: [] },
  { characterId: null, code: 'target_a', name: '训练靶标 ALPHA', english: null, npc: true, ready: false, variants: 1, actions: [] },
  { characterId: null, code: 'field_archive', name: '基地观测与维护协议归档实体', english: null, npc: true, ready: true, variants: 1, actions: [] },
]

export type OperatorDevelopmentPreview = {
  catalog: CharacterCatalog
  animations: Record<string, CharacterAnimation[]>
  createReport: (entity: CharacterSummary, label: string, state: OperatorReport['state']) => OperatorReport
}

/** Explicit visual fixtures never call the extraction service. */
export function createOperatorDevelopmentPreview(): OperatorDevelopmentPreview {
  const characters: CharacterSummary[] = ENTRIES.map(entry => {
    const common = {
      selection_id: `${entry.npc ? 'npc' : 'chr'}_demo_${entry.code}`,
      name: entry.name,
      variants: Array.from({ length: entry.variants }, (_, index) => ({ variant_id: `demo_${entry.code}_${index}`, label: index ? '备用装束 · 演示' : entry.npc ? '基础模型' : '默认装束', variant: index ? 'ALT_02' : 'DEFAULT_01', geometry_status: 'not_verified' as const })),
      capabilities: { bundle_prepare: entry.ready, blend_export: entry.ready, animation: entry.actions.length > 0, raw_expressions: !entry.npc, basic_mmd: !entry.npc },
      optional_tools_reason: null,
      unavailable_reason: entry.ready ? null : 'DEMO_CACHE_MISSING · 演示缓存缺失，不能导出',
    }
    return entry.npc ? { ...common, entry_kind: 'independent_npc', npc_id: `npc_demo_${entry.code}`, english_name: null, name_status: 'resolved', source_kind: 'prefab', name_source: null, expression_unavailable_reason: '当前实体无可用表情模板（开发样例）', cpu_animation_template: null, animation_scope: 'development_preview', animation_catalog_available: entry.actions.length > 0, provider_found: entry.ready, assembly_scope: null } : { ...common, entry_kind: 'playable', character_id: entry.characterId, english_name: entry.english }
  })
  const animations = Object.fromEntries(characters.flatMap((entity, index) => entity.variants.map(variant => [variant.variant_id, ENTRIES[index].actions.map(name => ({ action_id: `demo_${variant.variant_id}_${name}`, name, binding_status: 'development_preview' }))])))
  return {
    catalog: { format: 'CharacterCatalog/1', catalog_id: 'development_preview', source_identity: 'development_preview', availability: { ready: true, reason: null }, characters, profiles: [{ profile_id: 'hgrp-preview', name: 'HGRP 可编辑材质', engines: ['EEVEE', 'CYCLES'] }], limitations: ['开发样例，不读取游戏，不执行提取。'] },
    animations,
    createReport: (entity, label, state) => ({
      portrait: getOperatorPortrait(entity.entry_kind === 'playable' ? entity.character_id : null),
      id: crypto.randomUUID(), state, entity: `${entity.english_name ?? entity.selection_id} / ${entity.name}`, development: true,
      output: state === 'completed' || state === 'partial' ? `DEVELOPMENT_PREVIEW\\${label}\\${entity.selection_id}.blend` : null,
      files: state === 'completed' || state === 'partial' ? [{ relative_path: 'character.blend', role: 'character', bytes: 0, sha256: 'development_preview' }] : [],
      pending: state === 'partial' ? [{ code: 'DEMO_EXPRESSION_MISSING', message: '模拟表情模板缺失' }, { code: 'DEMO_ACTION_LIMIT', message: '模拟动作验收未完成' }] : [],
      error: state === 'failed' ? 'DEMO_CACHE_READ_FAILED · 开发样例缓存读取失败，没有生成文件。' : null,
    }),
  }
}
