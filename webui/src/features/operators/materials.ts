import type { CharacterMaterialOptions, CharacterSummary, CharacterWeatherEffect } from './types'

export const WEATHER_EFFECTS: readonly CharacterWeatherEffect[] = ['wet', 'snow', 'rain']
export const WEATHER_LABELS: Record<CharacterWeatherEffect, string> = { wet: '湿润', snow: '积雪', rain: '雨水' }

export function originalMaterials(): CharacterMaterialOptions {
  return { mode: 'original', lighting: 'full', weather: { quality: 'full', effects: [...WEATHER_EFFECTS] } }
}

export function simplifiedMaterialRestriction(entity: CharacterSummary | null): string | null {
  return entity?.entry_kind === 'independent_npc' && entity.source_kind === 'config_assembled'
    ? '当前组合式实体仅支持原始材质，请选择原始材质后导出。'
    : null
}

export function describeMaterials(options: CharacterMaterialOptions): string {
  const effects = options.weather.effects.map(effect => WEATHER_LABELS[effect]).join('、') || '不包含天气效果'
  return `${options.mode === 'original' ? '原始材质' : '精简材质'} · ${options.lighting === 'full' ? '完整' : '简易'}光照 · ${options.weather.quality === 'full' ? '完整' : '简易'}天气 · ${effects}`
}

export function assertMaterialAcknowledgement(requested: CharacterMaterialOptions | undefined, acknowledged: CharacterMaterialOptions | undefined): void {
  if (!requested) return
  if (!acknowledged || requested.mode !== acknowledged.mode || requested.lighting !== acknowledged.lighting ||
    requested.weather.quality !== acknowledged.weather.quality || requested.weather.effects.length !== acknowledged.weather.effects.length ||
    requested.weather.effects.some(effect => !acknowledged.weather.effects.includes(effect))) {
    throw new TypeError(`后端未确认本次材质选择，已停止提交；请检查前后端版本。请求：${JSON.stringify(requested)}；后端确认：${JSON.stringify(acknowledged) ?? '未返回'}。`)
  }
}
