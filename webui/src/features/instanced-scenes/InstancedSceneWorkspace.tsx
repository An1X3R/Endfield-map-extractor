import { useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { useDraggablePanel } from '../../hooks/useDraggablePanel'
import { InstancedSceneResult } from './InstancedSceneResult'
import type { InstancedSceneResultView } from './InstancedSceneResult'
import { useInstancedScenes } from './useInstancedScenes'
import type {
  InstancedSceneExportGroup,
  InstancedSceneExportMode,
  InstancedSceneForm,
  InstancedSceneJob,
  InstancedSceneSourcePaths,
  InstancedSceneSummary,
} from './types'
import './instanced-scenes.css'

type Props = {
  active: boolean
  firstRunReady: boolean
  firstRunBusy: boolean
  mapJobBusy: boolean
  source: InstancedSceneSourcePaths
  outputRoot: string
  preparationIdentity: string | null
  onRequestPreparation: () => void
  onBusyChange: (busy: boolean) => void
  onCatalogCountChange: (count: number | null) => void
  developmentPreview: import('./types').InstancedSceneDevelopmentPreview | null
}

const EXPORT_GROUPS: ReadonlyArray<{ id: InstancedSceneExportGroup; label: string }> = [
  { id: 'building', label: '建筑' },
  { id: 'prop', label: '道具' },
  { id: 'lighting', label: '光源' },
  { id: 'particle', label: '粒子' },
  { id: 'vegetation', label: '植被' },
  { id: 'terrain', label: '地形' },
  { id: 'road', label: '道路' },
  { id: 'water', label: '水体' },
  { id: 'effects', label: '特效' },
  { id: 'unknown', label: '未知 / 其它' },
]

const FORM_LABELS: Record<InstancedSceneForm, string> = {
  maze: '协议空间 / 迷宫',
  water: '探索 / 水体机关',
  tower: '结构 / 高塔',
  office: '设施 / 办公室',
  ship: '设施 / 舰船',
  blueprint: '结构 / 蓝图',
}

const EXPORT_MODE_LABELS: Record<InstancedSceneExportMode, string> = {
  blend: 'Blender 场景（含审计报告）',
  data_package: '仅审计数据包',
}

const isTerminalJob = (job: InstancedSceneJob): boolean =>
  job.state === 'completed' || job.state === 'partial' || job.state === 'failed' || job.state === 'cancelled'

const formatDimension = (value: number): string => {
  if (!Number.isFinite(value)) throw new RangeError(`Invalid scene dimension: ${value}`)
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
}

const catalogStateText = (connection: string): string => {
  if (connection === 'loading') return '正在读取独立场景目录…'
  if (connection === 'ready') return '目录连接已建立。场景可浏览，导出状态以能力声明为准。'
  if (connection === 'disconnected') return '本地服务连接中断；可重连后继续读取目录。'
  if (connection === 'unavailable') return '独立场景接口当前不可用；不会用演示数据替代正式目录。'
  return '等待独立场景接口接通。'
}

const sceneDescription = (scene: InstancedSceneSummary, groupName: string): string =>
  `${groupName} · ${FORM_LABELS[scene.form]}`

export function InstancedSceneWorkspace({
  active,
  firstRunReady,
  firstRunBusy,
  mapJobBusy,
  source,
  outputRoot,
  preparationIdentity,
  onRequestPreparation,
  onBusyChange,
  onCatalogCountChange,
  developmentPreview,
}: Props) {
  const workspaceRef = useRef<HTMLElement | null>(null)
  const [openGroups, setOpenGroups] = useState<ReadonlySet<string>>(() => new Set())
  const [exportMode, setExportMode] = useState<InstancedSceneExportMode>('blend')
  const [selectedGroups, setSelectedGroups] = useState<ReadonlySet<InstancedSceneExportGroup>>(
    () => new Set(EXPORT_GROUPS.map((group) => group.id)),
  )
  const [outputLabel, setOutputLabel] = useState('scene-export')
  const [resultView, setResultView] = useState<InstancedSceneResultView | null>(null)
  const [showResult, setShowResult] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const shownJobId = useRef<string | null>(null)
  const restoredLabelFor = useRef<string | null>(null)
  const shownDevelopmentPreview = useRef<Props['developmentPreview']>(null)

  const scenes = useInstancedScenes({
    active,
    firstRunReady,
    firstRunIdentity: preparationIdentity,
    source,
    firstRunBusy,
    mapJobBusy,
    developmentPreview,
    onBusyChange,
    onCatalogCountChange,
  })
  const selectedScene = scenes.catalog?.scenes.find((scene) => scene.scene_id === scenes.selectedSceneId) ?? null
  const selectedDetail = scenes.detail?.scene_id === scenes.selectedSceneId ? scenes.detail : null
  const selectedPreview = scenes.preview?.scene_id === scenes.selectedSceneId ? scenes.preview : null
  const selectedGroup = scenes.catalog?.groups.find((group) => group.group_id === selectedScene?.group_id)
  const availableModes = useMemo(() => {
    if (!scenes.catalog || !selectedScene || !selectedDetail) return []
    return scenes.catalog.capabilities.export_modes.filter((mode) =>
      selectedScene.export_capability.export_modes.includes(mode) &&
      selectedDetail.export_capability.export_modes.includes(mode),
    )
  }, [scenes.catalog, selectedDetail, selectedScene])
  const activeJob = scenes.job !== null && !isTerminalJob(scenes.job)
  const panelDisabled = !active || scenes.operationPending || activeJob
  const draggable = useDraggablePanel(workspaceRef, panelDisabled)
  const normalizedLabel = outputLabel.trim()
  const labelIsValid = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(normalizedLabel)
  const sourcesReady = Boolean(source.game_root.trim() && source.cache_root.trim() &&
    (exportMode !== 'blend' || source.blender_exe.trim()))
  const identityMatches = scenes.catalog?.capabilities.requires_first_run !== true ||
    scenes.catalog.first_run_identity === preparationIdentity
  const selectedGroupIds = EXPORT_GROUPS.filter((group) => selectedGroups.has(group.id)).map((group) => group.id)
  const selectedModeSupported = availableModes.includes(exportMode)
  const requestReady = scenes.canStartExport && active && firstRunReady && identityMatches && sourcesReady &&
    outputRoot.trim().length > 0 && labelIsValid && selectedScene !== null && selectedGroupIds.length > 0 &&
    selectedDetail?.export_capability.ready === true && !scenes.detailLoading && selectedModeSupported &&
    developmentPreview === null

  useEffect(() => {
    if (!active || !developmentPreview || scenes.catalog !== developmentPreview.catalog ||
      shownDevelopmentPreview.current === developmentPreview) return
    const scene = developmentPreview.catalog.scenes.find((entry) => entry.scene_id === scenes.selectedSceneId) ??
      developmentPreview.catalog.scenes[0]
    if (!scene) throw new Error('The development scene catalog is empty.')
    shownDevelopmentPreview.current = developmentPreview
    setOpenGroups((current) => new Set([...current, scene.group_id]))
    void scenes.selectScene(scene.scene_id).catch((error: unknown) => {
      setActionError(error instanceof Error ? error.message : String(error))
    })
    setResultView({ kind: 'development', sceneId: scene.scene_id, sceneName: scene.name, exportMode, exportGroups: selectedGroupIds })
    setShowResult(true)
  }, [active, developmentPreview, scenes.catalog, scenes.selectedSceneId, scenes.selectScene, exportMode, selectedGroupIds])

  useEffect(() => {
    const request = scenes.operationRequest ?? scenes.job?.request ?? null
    if (!request || restoredLabelFor.current === request.request_id) return
    setOutputLabel(request.output.label)
    setSelectedGroups(new Set(request.export_groups))
    setExportMode(request.export_mode)
    restoredLabelFor.current = request.request_id
  }, [scenes.job, scenes.operationRequest])

  useEffect(() => {
    if (availableModes.length > 0 && !availableModes.includes(exportMode)) setExportMode(availableModes[0])
  }, [availableModes, exportMode])

  useEffect(() => {
    const job = scenes.job
    if (!active || !job || !isTerminalJob(job) || shownJobId.current === job.job_id) return
    const sceneName = scenes.catalog?.scenes.find((scene) => scene.scene_id === job.request.scene_id)?.name ?? job.request.scene_id
    shownJobId.current = job.job_id
    setResultView({ kind: 'job', job, sceneName })
    setShowResult(true)
  }, [active, scenes.catalog, scenes.job])

  useEffect(() => {
    const job = scenes.job
    if (!job || !showResult || resultView?.kind !== 'job' || resultView.job.job_id !== job.job_id) return
    const sceneName = scenes.catalog?.scenes.find((scene) => scene.scene_id === job.request.scene_id)?.name ?? job.request.scene_id
    if (resultView.job === job && resultView.sceneName === sceneName) return
    setResultView({ ...resultView, job, sceneName })
  }, [resultView, scenes.catalog, scenes.job, showResult])

  const toggleGroup = (groupId: string): void => {
    setOpenGroups((current) => {
      const next = new Set(current)
      if (next.has(groupId)) next.delete(groupId)
      else next.add(groupId)
      return next
    })
  }

  const toggleExportGroup = (groupId: InstancedSceneExportGroup): void => {
    setSelectedGroups((current) => {
      const next = new Set(current)
      if (next.has(groupId)) next.delete(groupId)
      else next.add(groupId)
      return next
    })
  }

  const selectScene = (sceneId: string): void => {
    const scene = scenes.catalog?.scenes.find((entry) => entry.scene_id === sceneId)
    if (!scene) throw new Error(`Scene ${sceneId} is not present in the active catalog.`)
    if (!scene.export_capability.export_modes.includes(exportMode)) {
      const nextMode = scenes.catalog?.capabilities.export_modes.find((mode) => scene.export_capability.export_modes.includes(mode))
      if (nextMode) setExportMode(nextMode)
    }
    setActionError(null)
    void scenes.selectScene(sceneId)
  }

  const handleExport = (): void => {
    if (!requestReady || !scenes.catalog || !selectedScene) {
      throw new Error('Instanced-scene export is unavailable until every readiness and output setting is valid.')
    }
    setActionError(null)
    void scenes.startExport({
      format: 'InstancedSceneExportRequest/1',
      request_id: crypto.randomUUID(),
      scene_id: selectedScene.scene_id,
      catalog_id: scenes.catalog.catalog_id,
      first_run_identity: preparationIdentity,
      export_mode: exportMode,
      export_groups: selectedGroupIds,
      effects_mode: 'full_system_by_anchor',
      source,
      output: { root: outputRoot.trim(), label: normalizedLabel },
    }).catch((error: unknown) => {
      setActionError(error instanceof Error ? error.message : `独立场景任务提交失败：${String(error)}`)
    })
  }

  const openDevelopmentResult = (): void => {
    if (!import.meta.env.DEV) throw new Error('Development result preview is available only in development builds.')
    if (!selectedScene) throw new Error('Select an instanced scene before opening its development result preview.')
    setResultView({
      kind: 'development',
      sceneId: selectedScene.scene_id,
      sceneName: selectedScene.name,
      exportMode,
      exportGroups: selectedGroupIds,
    })
    setShowResult(true)
  }

  const runApiAction = (action: () => Promise<void>): void => {
    setActionError(null)
    void action().catch((error: unknown) => {
      setActionError(error instanceof Error ? error.message : `独立场景接口操作失败：${String(error)}`)
    })
  }

  const exportMessage = (): { tone: 'error' | 'warning' | 'info'; text: string } => {
    if (developmentPreview !== null) return { tone: 'warning', text: '当前目录仅用于界面状态展示；真实任务没有提交。' }
    if (actionError) return { tone: 'error', text: '独立场景请求未完成；请展开左侧技术诊断查看详情。' }
    if (!scenes.catalog) {
      if (scenes.connection === 'unavailable' || scenes.error) {
        return { tone: 'error', text: '独立场景服务暂不可用；请展开左侧技术诊断查看请求详情。' }
      }
      return { tone: 'warning', text: catalogStateText(scenes.connection) }
    }
    if (!scenes.catalog.availability.ready) return { tone: 'warning', text: '独立场景目录当前不可用。' }
    if (!selectedScene) return { tone: 'info', text: '选择一个场景后检查导出能力。' }
    if (scenes.catalog.capabilities.requires_first_run && !identityMatches) {
      return { tone: 'warning', text: '目录与当前首次准备身份不匹配；请重新读取目录。' }
    }
    if (!firstRunReady) return { tone: 'warning', text: '需要先完成地图首次准备。' }
    if (firstRunBusy || mapJobBusy) return { tone: 'warning', text: '首次准备或地图任务运行中，暂不可导出。' }
    if (!selectedScene.export_capability.ready) {
      return { tone: 'warning', text: selectedScene.export_capability.reason || '此场景暂不可导出。' }
    }
    if (scenes.detailLoading) return { tone: 'warning', text: '正在读取场景详情与导出能力…' }
    if (!selectedDetail) {
      return scenes.error
        ? { tone: 'error', text: '场景详情暂不可用；请展开左侧技术诊断查看请求详情。' }
        : { tone: 'warning', text: '场景详情尚未读取，暂不允许提交导出。' }
    }
    if (!selectedDetail.export_capability.ready) {
      return { tone: 'warning', text: selectedDetail.export_capability.reason || '场景详情声明当前不可导出。' }
    }
    if (availableModes.length === 0) return { tone: 'warning', text: '目录没有声明此场景可用的导出格式。' }
    if (!selectedModeSupported) return { tone: 'warning', text: '当前输出格式不适用于所选场景。' }
    if (selectedGroupIds.length === 0) return { tone: 'warning', text: '至少选择一个导出分组。' }
    if (!sourcesReady) return { tone: 'warning', text: exportMode === 'blend' ? '游戏目录、缓存目录或 Blender 路径未配置完整。' : '游戏目录或缓存目录未配置完整。' }
    if (!outputRoot.trim()) return { tone: 'warning', text: '尚未配置输出目录。' }
    if (!labelIsValid) return { tone: 'warning', text: '输出名称需为 1–80 位字母、数字、点、下划线或连字符。' }
    if (scenes.operationPending || activeJob) return { tone: 'warning', text: '当前场景任务尚未结束。' }
    if (scenes.connection !== 'ready') return { tone: 'warning', text: '独立场景服务连接状态尚未就绪。' }
    return { tone: 'info', text: '任务将按所选物理场景提交，并返回可核验的输出清单。' }
  }

  const message = exportMessage()
  const catalogGroups = scenes.catalog?.groups ?? []
  const scenesByGroup = catalogGroups.map((group) => ({
    ...group,
    scenes: scenes.catalog?.scenes.filter((scene) => scene.group_id === group.group_id) ?? [],
  }))
  const sceneOrdinal = selectedScene && scenes.catalog
    ? scenes.catalog.scenes.findIndex((scene) => scene.scene_id === selectedScene.scene_id) + 1
    : 0
  const sceneCount = scenes.catalog?.scenes.length ?? 0
  const catalogReady = scenes.catalog?.availability.ready === true
  const sceneCountLabel = catalogReady ? String(sceneCount).padStart(2, '0') : '—'
  const sceneOrdinalLabel = catalogReady ? String(sceneOrdinal).padStart(2, '0') : '—'
  const isCleanDevelopmentCatalog = developmentPreview !== null && scenes.catalog !== null &&
    scenes.error === null && scenes.diagnostic === null
  const indexStatusText = scenes.catalog
    ? isCleanDevelopmentCatalog
      ? 'DEV · 样例场景目录'
      : scenes.error ?? scenes.diagnostic?.detail ?? (scenes.catalog.availability.ready
        ? `目录已读取 · ${sceneCount} 个独立场景。`
        : scenes.catalog.availability.reason || '场景目录当前不可用。')
    : scenes.connection === 'unavailable' || scenes.error || scenes.diagnostic
      ? '独立场景服务暂不可用；可展开技术诊断查看请求详情。'
      : catalogStateText(scenes.connection)
  const indexStatusTone = scenes.error !== null || scenes.diagnostic !== null
    ? 'error'
    : scenes.catalog?.availability.ready === false && !isCleanDevelopmentCatalog
      ? 'warning'
      : 'info'
  const stageCode = selectedScene
    ? `SEC_${selectedScene.group_id.toUpperCase().replace(/[^A-Z0-9]+/g, '_')} // ${String(sceneOrdinal).padStart(2, '0')}`
    : 'SEC_DISCRETE // 00'
  const previewAvailable = selectedPreview?.status === 'ready' && selectedPreview.asset !== null
  const previewScopeNotice = selectedPreview?.scope === 'bounded_region'
    ? `${selectedPreview.reason || '当前图片只覆盖场景的一部分'} 此预览边界不改变物理场景整体导出范围。`
    : null
  const sceneTitle = selectedScene?.name ?? '选择一个独立场景。'
  const jobProgress = scenes.job ? Math.max(0, Math.min(1, scenes.job.progress)) : 0

  return (
    <section
      ref={workspaceRef}
      className="isc-workspace"
      aria-label="独立场景工作区"
      aria-hidden={!active}
      hidden={!active}
      inert={!active}
      data-testid="instanced-scene-workspace"
    >
      <div className="isc-floor" aria-hidden="true" />
      <div className="isc-horizon" aria-hidden="true"><span>SURFACE_DATUM // 0.000</span></div>
      <header className="isc-heading">
        <span className="isc-kicker">03 / DISCRETE SCENE INDEX</span>
        <h1>INSTANCED<br /><em>SCENES</em></h1>
        <span className="isc-heading-cn">独立场景</span>
        <p>按物理场景归属索引，查看目录信息、预览与导出能力。</p>
      </header>

      <div className="isc-layout">
        <aside className="isc-index" aria-label="场景目录">
          <div className="isc-index-head">
            <span>SCENE INDEX / CATALOG</span>
            <b>{catalogReady ? String(sceneCount).padStart(2, '0') : '—'}</b>
          </div>
          <p className="isc-index-note">同一场景可关联多个玩法，目录按物理场景去重。</p>
          <p className={`isc-index-state${isCleanDevelopmentCatalog ? ' is-dev-summary' : ''}`} data-tone={indexStatusTone} role="status">
            {indexStatusText}
          </p>
          {scenes.diagnostic && (
            <details className="isc-diagnostic">
              <summary>展开技术诊断</summary>
              <dl>
                <div><dt>类型</dt><dd>{scenes.diagnostic.kind}</dd></div>
                {scenes.diagnostic.method && scenes.diagnostic.path && <div><dt>请求</dt><dd>{scenes.diagnostic.method} {scenes.diagnostic.path}</dd></div>}
                {scenes.diagnostic.status !== null && <div><dt>状态码</dt><dd>{scenes.diagnostic.status}</dd></div>}
                <div><dt>详情</dt><dd>{scenes.diagnostic.detail}</dd></div>
                {scenes.diagnostic.responseBody && <div className="isc-diagnostic-response"><dt>响应</dt><dd><pre>{scenes.diagnostic.responseBody}</pre></dd></div>}
              </dl>
            </details>
          )}
          <div className="isc-groups" aria-label="场景分组">
            {scenesByGroup.map((group) => {
              const expanded = openGroups.has(group.group_id)
              return (
                <section className="isc-group" key={group.group_id}>
                  <button
                    className="isc-group-toggle"
                    type="button"
                    aria-expanded={expanded}
                    aria-controls={`isc-group-items-${group.group_id}`}
                    onClick={() => toggleGroup(group.group_id)}
                    data-testid={`instanced-scene-group-${group.group_id}`}
                  >
                    <span>{group.name}</span>
                    <small className="isc-group-count">{catalogReady ? String(group.scenes.length).padStart(2, '0') : '—'}</small>
                    <span className="isc-group-arrow" aria-hidden="true">›</span>
                  </button>
                  <div
                    id={`isc-group-items-${group.group_id}`}
                    className="isc-group-items"
                    data-expanded={expanded}
                    aria-hidden={!expanded}
                    inert={!expanded}
                  >
                    <div className="isc-group-items-inner">
                      {group.scenes.map((scene, index) => (
                        <button
                          key={scene.scene_id}
                          className="isc-scene-entry"
                          type="button"
                          aria-current={scene.scene_id === scenes.selectedSceneId}
                          style={{ '--isc-stagger': `${index * 18}ms` } as CSSProperties}
                          onClick={() => selectScene(scene.scene_id)}
                          data-testid={`instanced-scene-entry-${scene.scene_id}`}
                        >
                          <b>{String(index + 1).padStart(2, '0')}</b>
                          <span>{scene.name}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                </section>
              )
            })}
            {scenes.catalog && catalogGroups.length === 0 && (
              <p className="isc-index-state" data-tone="warning">目录已连接，但尚未提供场景分组。</p>
            )}
          </div>
          <div className="isc-index-foot">DIRECTORY / SOURCE DECLARED BY API</div>
        </aside>

        <section className="isc-stage" aria-labelledby="isc-stage-title">
          <div className="isc-stage-top">
            <span>{selectedScene?.scene_id.toUpperCase() ?? 'NO SCENE SELECTED'}</span>
            <span>{sceneOrdinalLabel} / {sceneCountLabel}</span>
          </div>
          <span className="isc-stage-watermark" aria-hidden="true">{stageCode}</span>
          <div className="isc-stage-title">
            <small>{selectedScene ? (selectedDetail?.source_label ?? selectedGroup?.name ?? selectedScene.group_id).toUpperCase() : '等待选取 / AWAITING INDEX'}</small>
            <h2 id="isc-stage-title">{selectedScene ? <>{sceneTitle}<span>。</span></> : <>选择一个<br />独立场景<span>。</span></>}</h2>
            <p>{selectedScene
              ? sceneDescription(selectedScene, selectedGroup?.name ?? selectedScene.group_id)
              : '从左侧展开系列，选取要查看的场景。'}</p>
            {selectedScene && (
              <div className="isc-stage-meta">
                <span>{selectedScene.scene_id}</span>
                <span>{FORM_LABELS[selectedScene.form]}</span>
                {selectedDetail?.dimensions
                  ? <span>尺寸 {formatDimension(selectedDetail.dimensions.x)} × {formatDimension(selectedDetail.dimensions.y)} × {formatDimension(selectedDetail.dimensions.z)}</span>
                  : <span>{developmentPreview ? '真实尺寸待接口提供' : '场景尺寸待目录接口提供'}</span>}
              </div>
            )}
          </div>
          <div className={`isc-art${selectedScene ? '' : ' is-empty'}${previewAvailable ? ' has-preview' : ''}`} data-form={selectedScene?.form ?? 'blueprint'} aria-hidden="true">
            {previewAvailable && selectedPreview?.asset && (
              <img className="isc-art-preview" src={selectedPreview.asset.url} alt="" />
            )}
            <div className="isc-art-ornament">
              <div className="isc-art-orbit" />
              <div className="isc-art-orbit isc-art-orbit--inner" />
              <div className="isc-art-plinth"><i /><i /><i /><i /></div>
              <div className="isc-art-column isc-art-column--a" />
              <div className="isc-art-column isc-art-column--b" />
              <div className="isc-art-column isc-art-column--c" />
              <div className="isc-art-shard" />
              <div className="isc-art-shard isc-art-shard--b" />
              <span className="isc-art-cross isc-art-cross--a">+</span>
              <span className="isc-art-cross isc-art-cross--b">+</span>
              <div className="isc-art-scan" />
            </div>
            <span className="isc-art-caption">{previewAvailable
              ? selectedPreview?.scope === 'bounded_region' ? '局部预览 / BOUNDED REGION' : '全场景预览 / WHOLE SCENE'
              : '轴测蓝图 / 尺寸待确认'}</span>
          </div>
          {selectedPreview && (!previewAvailable || previewScopeNotice !== null) && (
            <span className="isc-preview-status" data-tone={selectedPreview.status === 'unavailable' ? 'error' : 'warning'}>
              {previewScopeNotice ?? (selectedPreview.reason || '此场景暂时没有可用预览图。')}
            </span>
          )}
          <div className="isc-stage-bottom">
            <span>{selectedScene ? `场景索引已选 · ${sceneOrdinalLabel}/${sceneCountLabel}` : '目录已就绪，等待选择'}</span>
            <span>{selectedPreview?.status === 'ready' && selectedPreview.asset
              ? selectedPreview.scope === 'bounded_region' ? '局部图片 / 不限制导出范围' : '全场景图片 / SAME-ORIGIN'
              : '结构预览 / 等待场景规格'}</span>
          </div>
        </section>

        <aside ref={draggable.panelRef} className={`isc-settings${draggable.isDragging ? ' is-dragging' : ''}`} aria-label="独立场景提取设置">
          <header
            ref={draggable.headerRef}
            className={`isc-settings-head${draggable.isDragging ? ' is-dragging' : ''}`}
            tabIndex={0}
            aria-label="拖动面板，方向键移动，Home 归位"
            onPointerDown={draggable.onPointerDown}
            onPointerMove={draggable.onPointerMove}
            onPointerUp={draggable.onPointerUp}
            onPointerCancel={draggable.onPointerCancel}
            onLostPointerCapture={draggable.onLostPointerCapture}
            onKeyDown={draggable.onKeyDown}
          >
            <span>EXTRACTION SETTINGS <b>⠿ 拖动</b></span>
            <h2>提取设置</h2>
            <button type="button" onClick={draggable.reset} aria-label="设置面板归位">归位 ↖</button>
          </header>
          <div className="isc-settings-body">
            <p className="isc-settings-note">输出设置 · 当前修改只作用于独立场景任务。</p>
            <label className="isc-setting-field">
              输出格式
              <select
                value={exportMode}
                onChange={(event) => {
                  const value = event.currentTarget.value
                  if (value !== 'blend' && value !== 'data_package') throw new Error(`Unsupported instanced-scene export mode: ${value}`)
                  setExportMode(value)
                }}
                data-testid="instanced-scene-export-mode"
                disabled={scenes.operationPending || activeJob || availableModes.length === 0}
              >
                {availableModes.length > 0
                  ? availableModes.map((mode) => <option value={mode} key={mode}>{EXPORT_MODE_LABELS[mode]}</option>)
                  : <option value={exportMode} disabled>等待场景能力确认</option>}
              </select>
            </label>
            <label className="isc-setting-field">
              输出名称
              <input
                value={outputLabel}
                onChange={(event) => setOutputLabel(event.currentTarget.value)}
                maxLength={80}
                spellCheck={false}
                autoComplete="off"
                aria-invalid={!labelIsValid}
                data-testid="instanced-scene-output-label"
                disabled={scenes.operationPending || activeJob}
              />
            </label>
            <div className="isc-output-root" title={outputRoot || '尚未配置输出目录'}>
              <strong>输出目录 / OUTPUT ROOT</strong>
              {outputRoot.trim() || '尚未配置；请先设置正式输出路径。'}
            </div>
            <div className="isc-output-root" title={source.game_root || '尚未配置游戏目录'}>
              <strong>首次准备来源 / SOURCE</strong>
              {firstRunReady ? '路径与首次准备身份匹配。' : '等待游戏目录、缓存和依赖检查通过。'}
            </div>
            <fieldset disabled={scenes.operationPending || activeJob}>
              <legend>导出分组 <span>已选择 {selectedGroupIds.length} 项</span></legend>
              <div className="isc-option-grid">
                {EXPORT_GROUPS.map((group) => (
                  <label key={group.id}>
                    <input
                      type="checkbox"
                      checked={selectedGroups.has(group.id)}
                      onChange={() => toggleExportGroup(group.id)}
                      data-testid={`instanced-scene-export-group-${group.id}`}
                    />
                    {group.label}
                  </label>
                ))}
              </div>
            </fieldset>
            {selectedDetail && (
              <section className="isc-component-catalog" aria-label="场景构件统计与目录限制">
                <h3>构件统计 <small>未测量显示 —</small></h3>
                <dl className="isc-component-counts" data-testid="instanced-scene-component-counts">
                  {EXPORT_GROUPS.map((group) => (
                    <div key={group.id}>
                      <dt>{group.label}</dt>
                      <dd data-testid={`instanced-scene-component-count-${group.id}`}>
                        {selectedDetail.component_counts[group.id]?.toLocaleString() ?? '—'}
                      </dd>
                    </div>
                  ))}
                </dl>
                {selectedDetail.limitations.length > 0 && (
                  <ul className="isc-detail-limitations" aria-label="目录限制">
                    {selectedDetail.limitations.map((limitation, index) => <li key={`${index}:${limitation}`}>{limitation}</li>)}
                  </ul>
                )}
              </section>
            )}
            <p className="isc-settings-note">
              {selectedDetail
                ? '目录统计可能未测量；最终构件数量与待处理项以任务报告为准。'
                : '场景详情尚未读取；目录分组可先浏览。'}
            </p>
          </div>
        </aside>

        <section className="isc-action" aria-label="导出预览与任务">
          <div className="isc-action-top"><span>INSTANCED SCENE EXPORT</span><span className="isc-badge">{developmentPreview ? 'DEV 样例' : '真实任务'}</span></div>
          <div className="isc-action-selection">
            <strong>{selectedScene?.name ?? '尚未选择场景'}</strong>
            <small>{selectedScene?.scene_id.toUpperCase() ?? 'SCENE ID / —'}</small>
          </div>
          {developmentPreview ? (
            <button type="button" onClick={openDevelopmentResult} disabled={!selectedScene} data-testid="instanced-scene-preview-action">
              <span><strong>查看结果状态样例</strong><small>开发预览 · 不提交真实导出</small></span>
              <span aria-hidden="true">↗</span>
            </button>
          ) : !firstRunReady ? (
            <button type="button" onClick={onRequestPreparation} disabled={firstRunBusy || mapJobBusy} data-testid="instanced-scene-first-run">
              <span><strong>{firstRunBusy || mapJobBusy ? '首次准备进行中' : '准备正式导出'}</strong><small>跳转至现有地图首次准备流程</small></span>
              <span aria-hidden="true">↗</span>
            </button>
          ) : (
            <button type="button" onClick={handleExport} disabled={!requestReady} data-testid="instanced-scene-start-export">
              <span>
                <strong>{activeJob ? '任务正在运行' : exportMode === 'blend' ? '构建 Blender 场景' : '导出审计数据包'}</strong>
                <small>{requestReady ? '提交真实任务并返回结果清单' : '导出门禁未满足'}</small>
              </span>
              <span aria-hidden="true">↗</span>
            </button>
          )}
          <p className="isc-action-message" data-tone={message.tone}>{message.text}</p>
          {import.meta.env.DEV && (
            <button className="isc-dev-action" type="button" onClick={openDevelopmentResult} disabled={!selectedScene} data-testid="instanced-scene-dev-result">
              打开结果状态样例 ↗
            </button>
          )}
          <div className="isc-runtime-row">
            <button type="button" onClick={() => runApiAction(scenes.refreshCatalog)} disabled={scenes.connection === 'loading'} data-testid="instanced-scene-refresh">刷新目录</button>
            <button
              type="button"
              onClick={() => runApiAction(scenes.activeJobId ? scenes.reconnect : scenes.refreshCatalog)}
              disabled={scenes.connection === 'loading'}
              data-testid="instanced-scene-reconnect"
            >
              {scenes.activeJobId ? '重连任务' : '重连接口'}
            </button>
          </div>
        </section>

        <section className="isc-job-panel" aria-label="独立场景任务状态" hidden={!scenes.job && scenes.connection !== 'disconnected' && !scenes.error}>
          {scenes.job ? (
            <>
              <div className="isc-job-heading"><span>SCENE JOB / {scenes.job.job_id}</span><strong>{scenes.job.phase.toUpperCase()} · {Math.round(jobProgress * 100)}%</strong></div>
              <p className="isc-job-message" role="status">{scenes.job.message}</p>
              <progress max={1} value={jobProgress} aria-label="场景任务进度" />
              {scenes.job.error && <p className="isc-job-error">{scenes.job.error.stage ? `${scenes.job.error.stage} · ` : ''}{scenes.job.error.code}: {scenes.job.error.message}</p>}
              <ol className="isc-job-events">
                {scenes.events.map((event) => <li key={`${scenes.job?.job_id ?? 'scene'}:${event.sequence}`}>{event.message}</li>)}
              </ol>
              {scenes.canCancel && (
                <button type="button" onClick={() => runApiAction(scenes.cancelExport)} disabled={scenes.cancelRequested} data-testid="instanced-scene-cancel">
                  {scenes.cancelRequested ? '正在请求取消…' : '取消任务'}
                </button>
              )}
            </>
          ) : (
            <>
              <div className="isc-job-heading"><span>SCENE SERVICE / CONNECTION</span><strong>OFFLINE</strong></div>
              <p className="isc-job-message" role="status">
                {scenes.recoveryNeedsDiscard
                  ? scenes.error ?? '旧任务属于不同的首次准备身份或源路径，已停止重连。'
                  : scenes.diagnostic || scenes.error
                    ? '独立场景服务暂不可用；请展开左侧技术诊断查看详情。'
                    : catalogStateText(scenes.connection)}
              </p>
              <button
                type="button"
                onClick={() => runApiAction(scenes.activeJobId ? scenes.reconnect : scenes.refreshCatalog)}
                data-testid="instanced-scene-reconnect-panel"
              >
                {scenes.activeJobId ? '重连场景任务' : '重连并刷新目录'}
              </button>
              {scenes.recoveryNeedsDiscard && (
                <>
                  <p className="isc-job-message">仅移除浏览器恢复记录；不会取消服务端任务或删除已生成输出。</p>
                  <button
                    type="button"
                    onClick={scenes.discardStaleRecovery}
                    data-testid="instanced-scene-discard-stale-recovery"
                  >
                    丢弃本地恢复记录
                  </button>
                </>
              )}
            </>
          )}
        </section>
      </div>

      <div className="isc-bottom-rail">
        <span role="status">{selectedScene ? `已选 ${selectedScene.name} · ${selectedScene.scene_id}` : '独立场景目录已就绪，等待选择'}</span>
        <span>DISCRETE SCENE INDEX / 03</span>
      </div>

      {active && showResult && resultView && <InstancedSceneResult result={resultView} onClose={() => setShowResult(false)} />}
    </section>
  )
}
