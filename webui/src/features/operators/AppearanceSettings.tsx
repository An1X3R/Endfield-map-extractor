import { FUR_OPTIONS } from './appearance'
import type { CharacterAppearanceOptions } from './types'

type Props = {
  enabled: boolean
  value: CharacterAppearanceOptions
  disabled: boolean
  restriction: string | null
  onEnabledChange: (value: boolean) => void
  onChange: (value: CharacterAppearanceOptions) => void
}

export function AppearanceSettings({ enabled, value, disabled, restriction, onEnabledChange, onChange }: Props) {
  const locked = disabled || !enabled || restriction !== null
  return <>
    <label className="checkbox-option"><input data-testid="operator-appearance-enabled" type="checkbox" checked={enabled} disabled={disabled || !enabled && restriction !== null} onChange={event => onEnabledChange(event.currentTarget.checked)} />启用外观设置</label>
    {restriction && <p className={enabled ? 'validation-error' : 'help'} data-testid="operator-appearance-restriction">{restriction}</p>}
    {!enabled && <p className="help">关闭时沿用现有导出行为，不添加外观设置。</p>}
    <label className="checkbox-option"><input data-testid="operator-appearance-maximum" type="checkbox" checked={value.potential === 'maximum'} disabled={locked} onChange={event => onChange({ ...value, potential: event.currentTarget.checked ? 'maximum' : 'base' })} />满潜外观初始开启</label>
    <p className="help">启用外观设置后装入源外观开关；未勾选满潜时初始关闭，可在 Blender 中调整。是否存在可用变化，以导出结果为准。</p>
    <div className="field-label">绒面处理</div>
    <div className="appearance-choices" role="group" aria-label="绒面处理">
      {FUR_OPTIONS.map(option => <button key={option.value} type="button" data-testid={`operator-fur-${option.value}`} aria-pressed={value.fur === option.value} disabled={locked} onClick={() => onChange({ ...value, fur: option.value })}><b>{option.label}</b><small>{option.description}</small></button>)}
    </div>
    <p className="help">炭绒暗燃是兼容源材质上的近似表现；不匹配的材质保留源表现并记录跳过，不代表所有角色都适用。</p>
    <p className="help">满潜粒子目前为固定展示快照，不包含完整原生动态过程。</p>
  </>
}
