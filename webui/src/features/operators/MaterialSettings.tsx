import { WEATHER_EFFECTS, WEATHER_LABELS } from './materials'
import type { CharacterMaterialOptions } from './types'

type Props = {
  value: CharacterMaterialOptions
  disabled: boolean
  restriction: string | null
  onModeChange: (mode: CharacterMaterialOptions['mode']) => void
  onSimplifiedChange: (value: CharacterMaterialOptions) => void
}

export function MaterialSettings({ value, disabled, restriction, onModeChange, onSimplifiedChange }: Props) {
  const locked = disabled || value.mode === 'original' || restriction !== null
  return <>
    <label className="field-label" htmlFor="operator-material-mode">材质方案</label>
    <select id="operator-material-mode" data-testid="operator-material-mode" value={value.mode} disabled={disabled} onChange={event => onModeChange(event.currentTarget.value as CharacterMaterialOptions['mode'])}>
      <option value="original">原始材质</option><option value="simplified" disabled={restriction !== null}>精简材质</option>
    </select>
    {restriction && <p className="help" data-testid="operator-material-restriction">{restriction}</p>}
    <p className="help">{value.mode === 'original' ? '保留原始材质方案，使用完整光照与全部天气效果。' : '减少材质复杂度；光照、天气精度与包含的效果可分别选择。外观可能与原始材质不同。'}</p>
    <div className="material-quality-fields">
      <label className="field-label" htmlFor="operator-material-lighting">光照
        <select id="operator-material-lighting" data-testid="operator-material-lighting" value={value.lighting} disabled={locked} onChange={event => onSimplifiedChange({ ...value, lighting: event.currentTarget.value as CharacterMaterialOptions['lighting'] })}>
          <option value="full">完整光照</option><option value="simple">简易光照</option>
        </select>
      </label>
      <label className="field-label" htmlFor="operator-weather-quality">天气精度
        <select id="operator-weather-quality" data-testid="operator-weather-quality" value={value.weather.quality} disabled={locked} onChange={event => onSimplifiedChange({ ...value, weather: { ...value.weather, quality: event.currentTarget.value as CharacterMaterialOptions['weather']['quality'] } })}>
          <option value="full">完整天气</option><option value="simple">简易天气</option>
        </select>
      </label>
    </div>
    <div className="material-effects" role="group" aria-label="包含的天气效果">
      {WEATHER_EFFECTS.map(effect => <label className="checkbox-option" key={effect}>
        <input type="checkbox" data-testid={`operator-weather-${effect}`} checked={value.weather.effects.includes(effect)} disabled={locked} onChange={event => onSimplifiedChange({ ...value, weather: { ...value.weather, effects: WEATHER_EFFECTS.filter(item => item === effect ? event.currentTarget.checked : value.weather.effects.includes(item)) } })} />{WEATHER_LABELS[effect]}
      </label>)}
    </div>
    <p className="help">{value.mode === 'simplified' && value.weather.effects.length === 0 ? '本次不包含天气效果。' : '勾选项决定导出包含哪些天气效果，强度在 Blender 中调整。'}</p>
    <p className="help">骨骼、表情、天气及画面后处理的交互控制在 Blender 内使用角色工具插件；当前页面仅配置导出。</p>
  </>
}
