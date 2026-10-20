import { useEffect, useRef, useState } from 'react'
import { EXPORT_GROUPS, LAYER_LABELS } from '../../data/maps'
import { useDraggablePanel } from '../../hooks/useDraggablePanel'
import type { ChunkMode } from '../../lib/mapChunking'
import type { ExportMode, LayerId } from '../../types'
import { AMBIENT_ART } from './artwork'
import screenArt from './screen.svg?raw'
import { PaintingMap } from './PaintingMap'
import { PaintingCompletion } from './PaintingCompletion'
import { INITIAL_PAINTING_SETTINGS } from './types'
import type { PaintingResult, PaintingSelection, PaintingSettings } from './types'
import type { PaintingRuntime } from './usePaintingRuntime'
import { mapPreparationTerminal } from './useMapPreparation'
import { describeSelection, snapPaintingSelection } from './levelMaps'
import { ExportGroupSelector } from '../../components/ExportGroupSelector'
import { InkSpirit } from './InkSpirit'
import type { CanvasMapSnapshot } from '../../lib/mapSnapshot'
import './si-painting.css'

type Props = {
  active: boolean
  previousMap: CanvasMapSnapshot | null
  runtime: PaintingRuntime
  onPrepare: () => void
  resetRevision: number
  previewRevision: number
  onBusyChange: (busy: boolean) => void
  onHandoffChange: (busy: boolean) => void
}

const INITIAL_SELECTION: PaintingSelection = { u: .4375, v: .4375, width: .125, height: .125 }

export function PaintingWorkspace({ active, previousMap, runtime, onPrepare, resetRevision, previewRevision, onBusyChange, onHandoffChange }: Props) {
  const workspaceRef = useRef<HTMLDivElement>(null)
  const [settings, setSettings] = useState<PaintingSettings>(INITIAL_PAINTING_SETTINGS)
  const [selection, setSelection] = useState<PaintingSelection>(INITIAL_SELECTION)
  const [collapsed, setCollapsed] = useState(false)
  const busy = runtime.busy
  const allGroupsSelected = settings.groups.length === EXPORT_GROUPS.length
  const [imageReady, setImageReady] = useState(false)
  const [imageError, setImageError] = useState<string | null>(null)
  const map = runtime.map
  const preparation = runtime.preparation
  const preview = import.meta.env.DEV && new URLSearchParams(window.location.search).get('painting-preview') === '1'
  const ready = Boolean(map && imageReady && runtime.sourceReady && !runtime.preview)
  const selectionLabel = map ? describeSelection(map, selection) : '地图坐标待准备'
  const onSelection = (value: PaintingSelection): void => setSelection(map ? snapPaintingSelection(map, value) : value)
  const [completion, setCompletion] = useState<{ result: PaintingResult | null } | null>(null)
  const previousPreview = useRef(previewRevision)
  const panel = useDraggablePanel(workspaceRef, busy)
  const closeCompletion = (): void => setCompletion(null)
  const patchSettings = (patch: Partial<PaintingSettings>): void => setSettings(current => ({ ...current, ...patch }))

  useEffect(() => { if (!active) setCompletion(null) }, [active])
  useEffect(() => { onBusyChange(busy) }, [busy, onBusyChange])
  useEffect(() => { if (runtime.result) setCompletion({ result: runtime.result }) }, [runtime.result])
  useEffect(() => {
    if (map) setSelection(snapPaintingSelection(map, INITIAL_SELECTION))
  }, [map])

  useEffect(() => {
    setSettings(INITIAL_PAINTING_SETTINGS)
    setSelection(map ? snapPaintingSelection(map, INITIAL_SELECTION) : INITIAL_SELECTION)
    setCollapsed(false)
    panel.reset()
  }, [resetRevision, panel.reset])
  useEffect(() => {
    if (previewRevision !== previousPreview.current) setCompletion({ result: null })
    previousPreview.current = previewRevision
  }, [previewRevision])

  return <div className="si-surface" data-theme="si" data-testid="si-workspace" hidden={!active} inert={!active}>
    <svg className="si-ambient" viewBox="0 0 1600 1000" preserveAspectRatio="xMidYMid slice" aria-hidden="true" dangerouslySetInnerHTML={{ __html: AMBIENT_ART }} />
    <div className="si-workspace" ref={workspaceRef}>
      <PaintingMap emptyLabel={runtime.loading ? '正在读取画境地图' : runtime.mapError ? '画境目录读取失败 · 请查看加载诊断' : '画境地图尚未准备'} onHandoffChange={onHandoffChange} previousMap={previousMap} active={active} overviewUrl={map ? `/api/v2${map.image.url}?v=${map.image.sha256}` : null} metadata={map} preview={preview} onImageReady={setImageReady} onImageError={setImageError} layers={settings.layers} selection={selection} onSelectionChange={onSelection} revision={resetRevision} disabled={busy} />
      <section className="si-region-title"><div className="si-hairline"><i /><i /></div><small>ORTHOGRAPHIC REGION SELECTOR / PAINTED REALM</small><div className="si-title-row"><h1>SI’S<br /><span>PAINTING</span></h1><span className="si-region-badge">祀的画</span></div><p><span>WULING / PAINTED REALM</span><i />ATLAS / 03</p><span className="si-title-ghost" aria-hidden="true">叁</span></section>
      <aside ref={panel.panelRef} className="si-settings" aria-label="祀的画提取设置">
        <header ref={panel.headerRef} className="si-settings-handle" tabIndex={0} aria-label="拖动设置面板，方向键移动，Home 归位" onPointerDown={panel.onPointerDown} onPointerMove={panel.onPointerMove} onPointerUp={panel.onPointerUp} onPointerCancel={panel.onPointerCancel} onLostPointerCapture={panel.onLostPointerCapture} onKeyDown={panel.onKeyDown} data-testid="si-settings-handle">
          <div><small>EXTRACTION SETTINGS</small><h2>提取设置 <span>⠿ 拖动</span></h2></div>
          <button type="button" data-testid="si-reset-panel" onClick={panel.reset} aria-label="归位设置面板">归位 ↖</button><button type="button" id="si-collapse" data-testid="si-collapse-panel" aria-expanded={!collapsed} aria-label="折叠提取设置" onClick={() => setCollapsed(value => !value)}>{collapsed ? '+' : '−'}</button>
        </header>
        <div className="si-settings-body" hidden={collapsed}>
          <div className="si-preparation-status" data-testid="si-map-status">
            <strong>{runtime.loading ? '正在读取画境目录' : runtime.mapError ? '画境目录读取失败' : imageError ? '地图图片加载失败' : map ? imageReady ? '画境地图已加载' : '正在加载地图图片' : '画境地图尚未准备'}</strong>
            {!runtime.sourceReady && <button type="button" data-testid="si-prepare-source" disabled={busy} onClick={onPrepare}>打开场景首次准备 ↗</button>}
            <button type="button" data-testid="si-refresh-map" disabled={busy || runtime.loading} onClick={runtime.reload}>刷新地图</button>
            {(runtime.mapError || imageError) && <details open data-testid="si-map-diagnostic"><summary>查看加载诊断</summary><p role="alert">{runtime.mapError ?? imageError}</p></details>}
            {runtime.mapMissing && !runtime.loading && runtime.sourceReady && <div data-testid="si-map-preparation">
              <p>从当前游戏来源发现地图，按需准备单张原图；已有同源地图将直接校验复用。</p>
              <label className="si-field">区域地图<select data-testid="si-prepare-map-level" value={preparation.selectedLevel} disabled={busy || preparation.loading} onChange={event => preparation.setSelectedLevel(event.target.value)}>
                {!preparation.catalog && <option value="">{preparation.loading ? '正在发现地图' : '地图目录未读取'}</option>}
                {preparation.catalog?.entries.map(entry => <option key={entry.levelId} value={entry.levelId} disabled={entry.needInverseXZ}>{entry.levelId} · {entry.tileCount} 块{preparation.catalog?.preparedLevelIds.includes(entry.levelId) ? ' · 已准备' : ''}{entry.needInverseXZ ? ' · 暂不支持该方向' : ''}</option>)}
              </select></label>
              <button type="button" data-testid="si-prepare-map" disabled={busy || preparation.loading || !preparation.selectedLevel || runtime.preview} onClick={() => void preparation.start()}>准备所选地图</button>
              <button type="button" data-testid="si-refresh-map-catalog" disabled={busy || preparation.loading} onClick={preparation.refresh}>刷新地图目录</button>
              {preparation.job && <div role="status" data-testid="si-map-preparation-status"><span>{preparation.job.message} · 当前阶段 {Math.round(preparation.job.progress * 100)}%</span><progress max="1" value={preparation.job.progress} />
                {!mapPreparationTerminal(preparation.job) && <button type="button" data-testid="si-cancel-map-preparation" disabled={preparation.cancelling || preparation.job.cancel_requested} onClick={() => void preparation.cancel()}>{preparation.cancelling || preparation.job.cancel_requested ? '正在取消' : '取消地图准备'}</button>}
                {preparation.job.state === 'failed' && <p role="alert">{preparation.job.error?.message}</p>}
              </div>}
              {preparation.error && <><p role="alert">{preparation.error}</p><button type="button" data-testid="si-reconnect-map-preparation" disabled={preparation.cancelling || preparation.submitting} onClick={preparation.job && !mapPreparationTerminal(preparation.job) ? preparation.reconnect : preparation.refresh}>重连地图状态</button></>}
              <small>祀的画需要当前来源提供对应地图；准备其他区域后仍可继续选择地图。</small>
            </div>}
            {preview && <small>开发预览模式不执行真实导出。{!map ? '正在显示构图占位。' : ''}</small>}
          </div>
          <fieldset disabled={busy}>
            <div className="si-section-caption"><b>{map ? '导出内容' : '预览与导出'}</b><span>LAYERS / 04</span></div>
            {map && <p className="si-layer-note">原图为合成地图；下列开关控制导出内容。</p>}
            <div className="si-layer-list">{(Object.keys(LAYER_LABELS) as LayerId[]).map(id => <label key={id}><input type="checkbox" data-testid={`si-layer-${id}`} checked={settings.layers[id]} onChange={event => patchSettings({ layers: { ...settings.layers, [id]: event.target.checked } })} /><span><b>{LAYER_LABELS[id][0]}</b><small>{LAYER_LABELS[id][1]} / {id.toUpperCase()}</small></span></label>)}</div>
            <label className="si-field">输出格式<select data-testid="si-output-format" value={settings.exportMode} onChange={event => patchSettings({ exportMode: event.target.value as ExportMode })}><option value="blend">Blender 场景 + 数据包（默认）</option><option value="data_package">仅审计数据包</option></select></label>
            <section className="si-export-groups" aria-label="导出分组">
              <div className="si-section-caption"><b>导出分组</b><span>{settings.groups.length} / {EXPORT_GROUPS.length} GROUPS</span></div>
              <p className="si-layer-note">全部或独立多选</p>
              <ExportGroupSelector groups={settings.groups} allSelected={allGroupsSelected} exportMode={settings.exportMode} disabled={busy} testIdPrefix="si" onSelectAll={() => patchSettings({ groups: EXPORT_GROUPS.map(([id]) => id) })} onToggle={id => patchSettings({ groups: allGroupsSelected ? [id] : settings.groups.includes(id) ? settings.groups.filter(group => group !== id) : [...settings.groups, id] })} />
            </section>
            <div className="si-section-caption"><b>区块与构建策略</b><span>CHUNK / BUILD</span></div>
            <label className="si-field">区块打包方式<select data-testid="si-pack-mode" value={settings.chunkMode} onChange={event => patchSettings({ chunkMode: event.target.value as ChunkMode })}><option value="cluster_4x4_sectors">4 × 4 sectors 稳定分组</option><option value="per_sector">每个 sector 独立导出</option><option value="merged_selection">按选区完整打包</option></select></label>
            <label className="si-field">水体模式 · {settings.exportMode === 'blend' ? '按源配置自动重建' : '仅记录导出参数'}<select data-testid="si-water-mode" value="stable_eevee" disabled><option value="stable_eevee">{settings.exportMode === 'blend' ? '自动重建 · Eevee / Cycles' : 'stable_eevee'}</option></select></label>
            <label className="si-field">特效选择策略<select data-testid="si-effects-mode" value="full_system_by_anchor" disabled><option value="full_system_by_anchor">按锚点保留完整系统</option></select></label>
            <p className="si-layer-note">粒子与特效为固定时刻的静态预览，仅支持部分组件；水体、灯光与材质效果仍有待处理项，以导出报告为准。</p>
            <label className="si-field">输出名称<input data-testid="si-output-name" value={settings.outputName} onChange={event => patchSettings({ outputName: event.target.value })} spellCheck={false} /></label>
          </fieldset>
          <div className="si-settings-note"><i />{ready ? '设置用于当前画境任务 · 部分内容仍待处理' : '请先准备场景与画境地图'}</div>
        </div>
      </aside>
      <aside className="si-cartouche"><div className="si-cartouche-top"><span>03</span><b>ATLAS<br />REGION STUDY</b><i>↗</i></div><div className="si-cartouche-content"><div><span className="si-tiny-bar" /><h2>祀的画</h2><p>画境全景<br />折卷入画</p></div><span className="si-screen-art" dangerouslySetInnerHTML={{ __html: screenArt }} /></div><div className="si-cartouche-foot">PAINTED SPACE <span>正交视图 / —</span></div></aside>
      <div className="si-annotation" aria-hidden="true"><span>画外有山，山中有画。</span><i>↗</i></div>
      <aside className="si-export-panel"><InkSpirit active={active && !busy && !completion} /><div className="si-export-heading"><span>REGION / SECTOR EXPORT</span><b>{busy ? 'RUNNING' : ready ? 'READY' : 'AWAITING DATA'}</b></div>
        <button type="button" id="si-export" data-testid="si-export" disabled={!ready || busy || !Object.values(settings.layers).some(Boolean) || !settings.outputName.trim() || settings.groups.length === 0 || selection.width === 0 || selection.height === 0} onClick={() => void runtime.submit(settings, selection)}><span><small>展开画卷 / OPEN THE SCROLL</small><strong>{busy ? '正在绘就山河' : ready ? '导出所选场景' : '等待画境准备'}</strong></span><b>↗</b></button>
        <div className="si-export-meta"><span>{selectionLabel}</span><span>{settings.chunkMode === 'cluster_4x4_sectors' ? '4 × 4 / GROUPED' : settings.chunkMode === 'per_sector' ? 'PER SECTOR' : 'MERGED'}</span><span>{settings.exportMode === 'blend' ? 'BLENDER' : 'DATA PACKAGE'}</span></div>
        <div className="si-pipeline"><i />PREPARE <i />EXTRACT <i />BUILD <i />AUDIT</div>
        {import.meta.env.DEV && <button type="button" className="si-preview-button" data-testid="si-completion-preview" onClick={() => setCompletion({ result: null })}>↗ 完成动画预览</button>}
        {busy && !preparation.busy && <div className="si-job-status" role="status"><span>{runtime.job ? `${runtime.job.phase} · ${Math.round(runtime.job.progress * 100)}%` : '正在校验导出请求'}</span><progress max="1" value={runtime.job?.progress ?? 0} /><button type="button" data-testid="si-cancel-job" disabled={!runtime.job || runtime.cancelling || runtime.job.cancel_requested} onClick={() => void runtime.cancel()}>{runtime.cancelling || runtime.job?.cancel_requested ? '正在取消' : '取消任务'}</button>{runtime.jobError && <button type="button" data-testid="si-reconnect-job" disabled={runtime.cancelling} onClick={runtime.reconnect}>重连状态</button>}</div>}
        {runtime.jobError && <p role="alert" className="si-export-error">{runtime.jobError}</p>}
      </aside>
      <div className="si-status-strip"><span>SELECTION <b>{selectionLabel}</b></span><span>画境 / 区域选择</span><span>PAINT / RESOLVE / COMPOSE</span></div>
    </div>
    {completion && <PaintingCompletion result={completion.result} onClose={closeCompletion} />}
  </div>
}
