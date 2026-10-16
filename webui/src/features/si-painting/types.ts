import type { ExportMode, LayerState } from '../../types'
import type { ChunkMode } from '../../lib/mapChunking'
import type { EXPORT_GROUPS } from '../../data/maps'

export type PaintingExportGroup = typeof EXPORT_GROUPS[number][0]
/** Normalized image coordinates; u points right and v points down, both in [0, 1]. */
export type PaintingSelection = { u: number; v: number; width: number; height: number }
export type PaintingSettings = {
  layers: LayerState
  groups: PaintingExportGroup[]
  chunkMode: ChunkMode
  exportMode: ExportMode
  outputName: string
}
export type PaintingOutcome = 'success' | 'partial' | 'failure' | 'cancel'
export type PaintingResult = {
  outcome: PaintingOutcome
  subtitle: string
  primary: string
  secondary: string
  terminal: string
  selectionLabel: string
}

export const INITIAL_PAINTING_SETTINGS: PaintingSettings = {
  layers: { terrain: true, instances: true, water: true, effects: true },
  groups: ['building', 'prop', 'lighting', 'particle', 'vegetation', 'terrain', 'road', 'water', 'effects', 'unknown'],
  chunkMode: 'cluster_4x4_sectors',
  exportMode: 'blend',
  outputName: 'si_painting_region',
}

export const PAINTING_OUTCOMES = {
  success: { title: '山河已入卷', kicker: '导出流程结束', symbol: '✓', label: '生成' },
  partial: { title: '画卷待续', kicker: '导出流程结束', symbol: '◒', label: '待处理' },
  failure: { title: '此卷未成', kicker: '导出未完成', symbol: '×', label: '需检查' },
  cancel: { title: '暂且搁笔', kicker: '任务已取消', symbol: '−', label: '状态' },
} satisfies Record<PaintingOutcome, { title: string; kicker: string; symbol: string; label: string }>
