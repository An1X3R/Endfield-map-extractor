import { Check } from 'lucide-react'
import { EXPORT_GROUPS } from '../data/maps'
import type { ExportMode } from '../types'

type ExportGroup = typeof EXPORT_GROUPS[number][0]
type Props = {
  groups: readonly string[]
  allSelected: boolean
  exportMode: ExportMode
  disabled: boolean
  testIdPrefix: string
  onSelectAll: () => void
  onToggle: (group: ExportGroup) => void
}

const GROUP_NOTES: Partial<Record<ExportGroup, string>> = {
  lighting: '原生灯光预览 · 亮度待校准',
  particle: '静态预览 · 部分组件支持',
  water: '双引擎重建 · 部分效果近似',
  effects: '静态预览 · 部分组件支持',
}

export function ExportGroupSelector({ groups, allSelected, exportMode, disabled, testIdPrefix, onSelectAll, onToggle }: Props) {
  return <>
    <button className={`export-all-toggle ${allSelected ? 'active' : ''}`} type="button" data-testid={`${testIdPrefix}-group-all`} aria-pressed={allSelected} disabled={disabled} onClick={onSelectAll}>
      <span>{allSelected && <Check size={9} />}</span><strong>全部分组</strong><small>默认 · 保留限制与待处理报告</small>
    </button>
    <div className="export-grid">
      {EXPORT_GROUPS.map(([id, label, code]) => {
        const active = !allSelected && groups.includes(id)
        const note = exportMode === 'blend' ? GROUP_NOTES[id] : undefined
        return <button key={id} type="button" data-testid={`${testIdPrefix}-group-${id}`} className={active ? 'active' : ''} aria-pressed={active} disabled={disabled} onClick={() => onToggle(id)}>
          <span>{active && <Check size={9} />}</span><strong>{label}</strong><small>{note ? `${code} · ${note}` : code}</small>
        </button>
      })}
    </div>
  </>
}
