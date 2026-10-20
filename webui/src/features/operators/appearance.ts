import type { CharacterAppearanceOptions, CharacterAppearanceReport, CharacterMaterialOptions, CharacterSummary } from './types'

export const FUR_OPTIONS: readonly { value: CharacterAppearanceOptions['fur']; label: string; description: string }[] = [
  { value: 'existing', label: '保持现有', description: '沿用现有毛发处理' },
  { value: 'source_shell', label: '源绒面', description: '使用源绒面结构' },
  { value: 'charcoal_embers', label: '炭绒暗燃', description: '对兼容源材质应用近似表现' },
]

export function appearanceRestriction(entity: CharacterSummary | null, materials: CharacterMaterialOptions): string | null {
  if (!entity) return '选择普通角色后可配置外观。'
  if (entity.entry_kind !== 'playable') return '当前外观设置仅支持普通角色，NPC、动物及其他独立实体暂不支持；请关闭外观设置后导出。'
  if (materials.mode !== 'original') return '外观设置需要原始材质；请改回原始材质，或关闭外观设置后导出。'
  return null
}

export function assertAppearanceAcknowledgement(requested: CharacterAppearanceOptions | undefined, acknowledged: CharacterAppearanceOptions | undefined): void {
  if (requested?.potential !== acknowledged?.potential || requested?.fur !== acknowledged?.fur) {
    throw new TypeError(`后端未确认本次外观选择，已停止提交；请检查前后端版本。请求：${JSON.stringify(requested) ?? '未启用'}；后端确认：${JSON.stringify(acknowledged) ?? '未返回'}。`)
  }
}

export function describeAppearance(report: CharacterAppearanceReport): string {
  const potential = report.potential.status === 'not_applicable' ? '无源配置变化（不适用）'
    : report.potential.status === 'partial' ? '部分应用'
      : report.potential.requested === 'base' ? '开关已装入，初始关闭' : '已应用，初始开启'
  const fur = { applied: '已应用', partial: '部分应用', not_applicable: '不适用，保留源表现', unchanged: '未改动' }[report.fur.status]
  const furName = FUR_OPTIONS.find(option => option.value === report.fur.requested)!.label
  return `${report.potential.requested === 'maximum' ? '满潜外观' : '基础外观'}：${potential}；${furName}：${fur}`
}
