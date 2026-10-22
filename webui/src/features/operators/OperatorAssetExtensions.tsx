import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { CSSProperties } from 'react'
import type { CharacterSummary } from './types'
import { OperatorCompletion } from './OperatorCompletion'
import type { OperatorReport } from './OperatorCompletion'
import { getOperatorPortrait } from './portraits'
import { useAssetCatalog } from '../assets/useAssetCatalog'
import { useAssetJob } from '../assets/useAssetJob'
import { isTerminalAssetJob } from '../assets/api'
import type { AssetDisplayPropResult, AssetIllustrationResult, AssetJob, AssetModelResult, DisplayPropCatalog, DisplayPropEntry, IllustrationCatalog, TerminalAssetJob, UnresolvedDisplayPropBinding } from '../assets/types'
import './operatorAssets.css'

type Props = {
  active: boolean
  blocked: boolean
  entity: CharacterSummary | null
  entityLabel: string
  taskReturnRevision: number
  ready: boolean
  modelBusy: boolean
  reduced: boolean
  resetRevision: number
  onBusyChange: (busy: boolean) => void
  onTaskChange: (text: string | null) => void
  onSelectionLockChange: (locked: boolean) => void
  onSelectCharacter: (characterId: string) => boolean
  onPropsOpen: () => void
  onCopy: (text: string, confirmation: string) => void
}

type Anchor = { left: number; top: number; width: number; viewportLeft: number; panelTop: number; panelHeight: number }

const TERMINAL_STATES = new Set(['completed', 'partial', 'failed', 'cancelled'])

function isTerminal(state: string): boolean {
  return TERMINAL_STATES.has(state)
}

function isIllustrationResult(result: AssetModelResult | AssetIllustrationResult): result is AssetIllustrationResult {
  return result.format === 'EndfieldIllustrationExport/1'
}

function isDisplayPropResult(result: AssetModelResult | AssetIllustrationResult): result is AssetDisplayPropResult {
  return result.format === 'EndfieldDisplayPropExport/1'
}

function createIllustrationReport(
  job: TerminalAssetJob,
  entityLabel: string,
  characterId: string,
): OperatorReport {
  const exported = job.result?.items.filter(isIllustrationResult).flatMap(result => result.files) ?? []
  const exportedIds = new Set(exported.map(file => file.illustration_id))
  const pending = job.state === 'partial'
    ? job.request.item_ids.filter(id => !exportedIds.has(id)).map(id => ({ code: 'ILLUSTRATION_RESULT_MISSING', message: `${id}：后端未返回文件明细或逐项失败原因。` }))
    : []
  const error = job.error ? `${job.error.message} · ${job.error.diagnostic_id}` : null
  return {
    assetType: 'illustration',
    illustrationIds: job.request.item_ids,
    portrait: getOperatorPortrait(characterId),
    id: job.job_id,
    state: job.state,
    entity: entityLabel,
    output: job.result?.output_directory ?? null,
    files: exported.map(file => ({ relative_path: file.relative_path, role: 'illustration', bytes: file.bytes, sha256: file.sha256 })),
    pending,
    error,
    development: false,
  }
}

function propBindingsFor(entry: DisplayPropEntry, characterId: string) {
  return entry.bindings.filter(binding => binding.character_id === characterId)
}

function resourceLabel(resourcePath: string): string {
  const segments = resourcePath.split(/[\\/]/).filter(Boolean)
  return segments[segments.length - 1] ?? resourcePath
}

function sameIdentifiers(left: readonly string[], right: readonly string[]): boolean {
  const sortedRight = [...right].sort()
  return left.length === sortedRight.length && [...left].sort().every((value, index) => value === sortedRight[index])
}

function commonCharacterIds(groups: string[][]): string[] {
  if (groups.length === 0 || groups.some(group => group.length === 0)) return []
  return [...new Set(groups[0])].filter(characterId => groups.every(group => group.includes(characterId)))
}

function assetJobOwnerCharacterId(job: AssetJob | null, illustrationCatalog: IllustrationCatalog | null, propsCatalog: DisplayPropCatalog | null): string | null {
  if (!job || job.request.item_ids.length === 0) return null
  if (job.kind === 'illustration' && illustrationCatalog) {
    const associations = job.request.item_ids.map(itemId => illustrationCatalog.entries
      .filter(entry => entry.illustration_id === itemId)
      .flatMap(entry => entry.character_id === null ? [] : [entry.character_id]))
    const owners = commonCharacterIds(associations)
    return owners.length === 1 ? owners[0] : null
  }
  if (job.kind === 'display_prop' && propsCatalog) {
    const associations = job.request.item_ids.map(itemId => propsCatalog.entries
      .filter(entry => entry.prop_id === itemId)
      .flatMap(entry => entry.bindings.map(binding => binding.character_id)))
    const owners = commonCharacterIds(associations)
    return owners.length === 1 ? owners[0] : null
  }
  return null
}

function illustrationStateLabel(loading: boolean, error: string | null, count: number, busy: boolean, jobKind: string | undefined, jobProgress: number, ready: boolean, hasCharacter: boolean): string {
  if (!hasCharacter) return '该实体无角色立绘'
  if (busy && jobKind === 'illustration') return `正在提取立绘 · ${Math.round(jobProgress * 100)}%`
  if (loading) return '读取立绘目录…'
  if (error) return '立绘目录错误'
  if (!ready) return '需完成环境准备'
  if (count === 0) return '无关联立绘'
  return '立绘提取'
}

function restrictedStatus(value: string): string {
  if (value === 'missing') return 'PROVIDER_MISSING · 源资产缺失'
  if (value === 'ambiguous') return 'PROVIDER_AMBIGUOUS · 源资产存在歧义'
  if (value === 'missing_resource') return 'MISSING_RESOURCE · 源资产缺失'
  if (value === 'ambiguous_resource') return 'AMBIGUOUS_BINDING · 资源关联未确认'
  return value
}

function IllustrationIcon() {
  return <svg className="operator-illustration-icon" width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 7V2h5m10 0h5v5m0 10v5h-5M7 22H2v-5" fill="none" stroke="currentColor" strokeWidth="1.5" /><circle cx="12" cy="8" r="3" fill="white" /><path d="m6 19 1-5 5-2.4 5 2.4 1 5z" fill="white" /></svg>
}

function PropRecord({ entry, characterId, selected, disabled, onChange }: {
  entry: DisplayPropEntry
  characterId: string
  selected: boolean
  disabled: boolean
  onChange: (propId: string, checked: boolean) => void
}) {
  const bindings = propBindingsFor(entry, characterId)
  return <label className="operator-prop-record" data-testid="operator-prop-record">
    <span className="operator-prop-record-title"><input type="checkbox" checked={selected} disabled={disabled} onChange={event => onChange(entry.prop_id, event.currentTarget.checked)} /><b title={resourceLabel(entry.resource_path)}>{resourceLabel(entry.resource_path)}</b><small>{entry.provider_status === 'ready' ? 'BOUND' : restrictedStatus(entry.provider_status)}</small></span>
    <code>{entry.prop_id}</code>
    {bindings.map((binding, index) => <div className="operator-prop-evidence" key={`${binding.prefab_path}-${binding.item_index}-${index}`}>
      <span>{binding.mount_field === 'boneName' ? 'BONE' : 'MOUNT'}: {binding.mount_point}</span>
      <span>{binding.evidence}</span>
      <code>{binding.prefab_path} · item {binding.item_index}</code>
    </div>)}
    <p>独立静态 .blend · 不含自动骨骼装配与动画</p>
  </label>
}

function UnresolvedPropRecord({ binding }: { binding: UnresolvedDisplayPropBinding }) {
  return <article className="operator-prop-record is-restricted" data-testid="operator-prop-restricted">
    <span className="operator-prop-record-title"><b>{binding.character_name ?? binding.character_name_id ?? binding.character_id}</b><small>{restrictedStatus(binding.status)}</small></span>
    <span className="operator-prop-evidence"><span>{binding.mount_field === 'boneName' ? 'BONE' : 'MOUNT'}: {binding.mount_point}</span><span>Item {binding.item_index} · unresolved source binding</span><code>{binding.prefab_path}</code></span>
    <p>不可选择 · {binding.resource_paths.length ? binding.resource_paths.join(' · ') : '源资源路径未返回'}</p>
  </article>
}

export function OperatorAssetExtensions({ active, blocked, entity, entityLabel, taskReturnRevision, ready, modelBusy, reduced, resetRevision, onBusyChange, onTaskChange, onSelectionLockChange, onSelectCharacter, onPropsOpen, onCopy }: Props) {
  const [propsOpen, setPropsOpen] = useState(false)
  const [selectedPropIds, setSelectedPropIds] = useState<string[]>([])
  const [propSelectionOwnerId, setPropSelectionOwnerId] = useState<string | null>(null)
  const [anchor, setAnchor] = useState<Anchor>({ left: 0, top: 0, width: 290, viewportLeft: 0, panelTop: 0, panelHeight: 520 })
  const [dock, setDock] = useState<HTMLElement | null>(null)
  const [completionOpen, setCompletionOpen] = useState(false)
  const [illustrationReport, setIllustrationReport] = useState<OperatorReport | null>(null)
  const [illustrationOwner, setIllustrationOwner] = useState<{ selectionId: string; characterId: string; label: string } | null>(null)
  const [propsOwnerSelectionId, setPropsOwnerSelectionId] = useState<string | null>(null)
  const [taskReturnActive, setTaskReturnActive] = useState(false)
  const [recoveredOwnerCharacterId, setRecoveredOwnerCharacterId] = useState<string | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const illustrationButton = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLElement>(null)
  const host = useRef<HTMLDivElement>(null)
  const shownReport = useRef<string | null>(null)
  const lastTaskReturnRevision = useRef(taskReturnRevision)
  const characterId = entity?.entry_kind === 'playable' ? entity.character_id : null
  const illustrationCatalog = useAssetCatalog('illustration', active)
  const propsCatalog = useAssetCatalog('display_prop', active)
  const assetJob = useAssetJob(['illustration', 'display_prop'])
  const localBusy = assetJob.busy
  const disabledByTask = modelBusy || localBusy || blocked
  const matchedIllustrations = useMemo(() => characterId && illustrationCatalog.catalog
    ? illustrationCatalog.catalog.entries.filter(entry => entry.character_id === characterId)
    : [], [characterId, illustrationCatalog.catalog])
  const matchedProps = useMemo(() => characterId && propsCatalog.catalog
    ? propsCatalog.catalog.entries.filter(entry => entry.bindings.some(binding => binding.character_id === characterId))
    : [], [characterId, propsCatalog.catalog])
  const selectableProps = useMemo(() => matchedProps.filter(entry => entry.provider_status === 'ready'), [matchedProps])
  const restrictedProps = useMemo(() => matchedProps.filter(entry => entry.provider_status !== 'ready'), [matchedProps])
  const unresolvedProps = useMemo(() => characterId && propsCatalog.catalog
    ? propsCatalog.catalog.unresolved_bindings.filter(binding => binding.character_id === characterId)
    : [], [characterId, propsCatalog.catalog])
  const activeJob = assetJob.job
  const recoveredJobOwnerCharacterId = assetJobOwnerCharacterId(activeJob, illustrationCatalog.catalog, propsCatalog.catalog)
  const jobKind = activeJob?.kind
  const progress = Math.round((activeJob?.progress ?? 0) * 100)
  const illustrationCatalogError = illustrationCatalog.error
  const illustrationsDisabled = !characterId || illustrationCatalog.loading || Boolean(illustrationCatalogError) || !illustrationCatalog.catalog || !ready || matchedIllustrations.length === 0 || disabledByTask
  const illustrationLabel = illustrationStateLabel(illustrationCatalog.loading, illustrationCatalogError, matchedIllustrations.length, localBusy, jobKind, activeJob?.progress ?? 0, ready, Boolean(characterId))
  const propCount = matchedProps.length + unresolvedProps.length
  const propsCatalogError = propsCatalog.error
  const currentPropIds = propSelectionOwnerId === entity?.selection_id ? selectedPropIds : []
  const propSelectionDisabled = disabledByTask || !ready || propsCatalog.loading || Boolean(propsCatalog.error) || !propsCatalog.catalog
  const propExportDisabled = propSelectionDisabled || currentPropIds.length === 0 || currentPropIds.some(id => !selectableProps.some(entry => entry.prop_id === id))
  const propResults = activeJob?.kind === 'display_prop' && activeJob.result?.items
    ? activeJob.result.items.filter(isDisplayPropResult)
    : []
  const propReceiptVisible = activeJob?.kind === 'display_prop' && isTerminal(activeJob.state) && propsOwnerSelectionId === entity?.selection_id
  const illustrationJobTerminal = activeJob?.kind === 'illustration' && isTerminal(activeJob.state)
  const requestedIllustrationsMatchSelection = activeJob?.kind === 'illustration' && characterId !== null && sameIdentifiers(activeJob.request.item_ids, matchedIllustrations.map(entry => entry.illustration_id))
  const illustrationError = assetJob.error ?? (activeJob?.kind === 'illustration' ? activeJob.error?.message ?? null : null)
  const propsJobError = assetJob.error ?? (activeJob?.kind === 'display_prop' ? activeJob.error?.message ?? null : null)
  const dockMessage = blocked ? '其他工作区有任务运行，角色操作暂不可用。'
    : modelBusy ? '角色准备或模型导出任务正在运行。'
      : assetJob.pending ? '正在验证资产请求…'
        : localBusy ? `${activeJob?.phase ?? '资产任务'} · ${activeJob?.message ?? '后台状态待确认'}${activeJob ? ` · ${progress}%` : ''}`
          : illustrationCatalogError ? `立绘目录读取失败：${illustrationCatalogError}`
            : propsCatalogError ? `展示道具目录读取失败：${propsCatalogError}`
              : ''
  const taskText = localBusy
    ? `TASK: ${activeJob?.state.toUpperCase() ?? (assetJob.pending ? 'VALIDATING' : 'CONNECTING')} ${activeJob?.kind === 'illustration' ? '角色立绘提取' : activeJob?.kind === 'display_prop' ? '展示道具导出' : '角色资产导出'}${activeJob ? ` ${progress}% · ${activeJob.message}` : ''}`
    : null

  useLayoutEffect(() => {
    const shell = host.current?.parentElement
    if (!shell) return
    const refresh = (): void => {
      const row = shell.querySelector<HTMLElement>('.entity-row.selected')
      const directory = shell.querySelector<HTMLElement>('.directory')
      const content = shell.querySelector<HTMLElement>('.directory-content')
      const base = shell.getBoundingClientRect()
      if (row && directory && content) {
        const bounds = row.getBoundingClientRect()
        const directoryBounds = directory.getBoundingClientRect()
        const contentBounds = content.getBoundingClientRect()
        const top = Math.max(contentBounds.top - base.top, Math.min(bounds.top - base.top, contentBounds.bottom - base.top - 64))
        const visibleTop = Math.max(8, base.top + 8)
        const visibleBottom = Math.min(window.innerHeight - 8, base.bottom - 8)
        const panelHeight = Math.min(520, Math.max(1, visibleBottom - visibleTop))
        const desiredPanelTop = base.top + top
        const panelViewportTop = Math.max(visibleTop, Math.min(desiredPanelTop, visibleBottom - panelHeight))
        setAnchor({
          left: directoryBounds.right - base.left,
          top,
          width: directoryBounds.width * .75,
          viewportLeft: directoryBounds.right,
          panelTop: panelViewportTop - desiredPanelTop,
          panelHeight,
        })
      }
      setDock(shell.querySelector<HTMLElement>('.action-dock'))
    }
    refresh()
    const observer = new ResizeObserver(refresh)
    observer.observe(shell)
    shell.addEventListener('scroll', refresh, true)
    window.addEventListener('resize', refresh)
    window.addEventListener('scroll', refresh, true)
    return () => {
      observer.disconnect()
      shell.removeEventListener('scroll', refresh, true)
      window.removeEventListener('resize', refresh)
      window.removeEventListener('scroll', refresh, true)
    }
  }, [active, entity?.selection_id])

  useLayoutEffect(() => {
    onBusyChange(localBusy)
    return () => onBusyChange(false)
  }, [localBusy, onBusyChange])

  useLayoutEffect(() => {
    onTaskChange(taskText)
  }, [onTaskChange, taskText])

  useLayoutEffect(() => () => onTaskChange(null), [onTaskChange])

  useLayoutEffect(() => {
    onSelectionLockChange(localBusy)
    return () => onSelectionLockChange(false)
  }, [localBusy, onSelectionLockChange])

  useEffect(() => {
    setSelectedPropIds([])
    setPropSelectionOwnerId(null)
    setPropsOpen(false)
    setIllustrationReport(null)
    setIllustrationOwner(null)
    setPropsOwnerSelectionId(null)
    setCompletionOpen(false)
    setRecoveredOwnerCharacterId(current => current === characterId ? current : null)
  }, [characterId, entity?.selection_id, resetRevision])

  useEffect(() => {
    if (!active || taskReturnRevision <= lastTaskReturnRevision.current) return
    setTaskReturnActive(true)
    if (!activeJob) {
      const currentCharacterId = illustrationOwner?.characterId ?? (propsOwnerSelectionId === entity?.selection_id ? characterId : null)
      if (!currentCharacterId) return
      if (!onSelectCharacter(currentCharacterId)) return
      lastTaskReturnRevision.current = taskReturnRevision
      if (propsOwnerSelectionId === entity?.selection_id) setPropsOpen(true)
      return
    }
    const catalogLoading = activeJob.kind === 'illustration' ? illustrationCatalog.loading : activeJob.kind === 'display_prop' ? propsCatalog.loading : false
    const catalogError = activeJob.kind === 'illustration' ? illustrationCatalog.error : activeJob.kind === 'display_prop' ? propsCatalog.error : null
    const catalogReady = activeJob.kind === 'illustration' ? illustrationCatalog.catalog : activeJob.kind === 'display_prop' ? propsCatalog.catalog : null
    if (catalogLoading || catalogError || !catalogReady || catalogReady.catalog_id !== activeJob.request.catalog_id) return
    if (!recoveredJobOwnerCharacterId) {
      lastTaskReturnRevision.current = taskReturnRevision
      return
    }
    if (recoveredOwnerCharacterId === recoveredJobOwnerCharacterId && characterId === recoveredJobOwnerCharacterId && entity) {
      lastTaskReturnRevision.current = taskReturnRevision
      setRecoveredOwnerCharacterId(null)
      if (activeJob.kind === 'display_prop') {
        setPropsOwnerSelectionId(entity.selection_id)
        setPropsOpen(true)
      }
      return
    }
    if (recoveredOwnerCharacterId !== recoveredJobOwnerCharacterId) setRecoveredOwnerCharacterId(recoveredJobOwnerCharacterId)
    if (characterId !== recoveredJobOwnerCharacterId && !onSelectCharacter(recoveredJobOwnerCharacterId)) return
  }, [active, activeJob, characterId, entity, entity?.selection_id, illustrationCatalog.catalog, illustrationCatalog.error, illustrationCatalog.loading, illustrationOwner, onSelectCharacter, propsCatalog.catalog, propsCatalog.error, propsCatalog.loading, propsOwnerSelectionId, recoveredJobOwnerCharacterId, recoveredOwnerCharacterId, taskReturnRevision])

  useEffect(() => { setSelectedPropIds([]) }, [propsCatalog.catalog?.catalog_id])

  useEffect(() => {
    if (!active || !illustrationJobTerminal || !activeJob || activeJob.kind !== 'illustration' || !isTerminalAssetJob(activeJob) || !characterId) return
    const ownerMatches = illustrationOwner !== null && illustrationOwner.selectionId === entity?.selection_id && illustrationOwner.characterId === characterId
    if (!ownerMatches && !requestedIllustrationsMatchSelection) return
    const report = createIllustrationReport(activeJob, ownerMatches && illustrationOwner ? illustrationOwner.label : entityLabel, characterId)
    setIllustrationReport(report)
    if ((report.state === 'completed' || report.state === 'partial') && shownReport.current !== report.id) {
      shownReport.current = report.id
      setCompletionOpen(true)
    }
  }, [active, activeJob, characterId, entity?.selection_id, entityLabel, illustrationJobTerminal, illustrationOwner, requestedIllustrationsMatchSelection])

  useEffect(() => {
    if (active) return
    setPropsOpen(false)
    setCompletionOpen(false)
  }, [active])

  useLayoutEffect(() => {
    if (!active || !taskReturnActive) return
    const receipt = propReceiptVisible ? panel.current?.querySelector<HTMLElement>('[data-testid="operator-prop-receipt"]') ?? null : null
    const illustrationReceipt = activeJob?.kind === 'illustration' && illustrationJobTerminal
      ? document.querySelector<HTMLElement>('[data-testid="operator-illustration-result"]')
      : null
    const propAction = activeJob?.kind === 'display_prop' ? panel.current?.querySelector<HTMLElement>('[data-testid="operator-props-cancel"]') ?? null : null
    const action = receipt ?? illustrationReceipt ?? propAction ?? document.querySelector<HTMLElement>('[data-testid="operator-asset-cancel"]') ?? document.querySelector<HTMLElement>('[data-testid="operator-illustration-export"]')
    action?.scrollIntoView({ block: 'nearest' })
    const waitingForPropReceipt = activeJob?.kind === 'display_prop' && isTerminal(activeJob.state) && !receipt && recoveredJobOwnerCharacterId !== null
    const waitingForIllustrationReport = activeJob?.kind === 'illustration' && illustrationJobTerminal && !illustrationReport && (recoveredJobOwnerCharacterId !== null || illustrationOwner !== null || requestedIllustrationsMatchSelection)
    const waitingForOwnerSelection = taskReturnRevision > lastTaskReturnRevision.current || recoveredOwnerCharacterId !== null
    if (!waitingForPropReceipt && !waitingForIllustrationReport && !waitingForOwnerSelection && (!localBusy || receipt || illustrationReceipt)) setTaskReturnActive(false)
  }, [active, activeJob, illustrationJobTerminal, illustrationOwner, illustrationReport, localBusy, propReceiptVisible, propsOpen, recoveredJobOwnerCharacterId, recoveredOwnerCharacterId, requestedIllustrationsMatchSelection, taskReturnActive, taskReturnRevision])

  useEffect(() => {
    if (!propsOpen) return
    panel.current?.focus()
    const escape = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      setPropsOpen(false)
      trigger.current?.focus({ preventScroll: true })
    }
    document.addEventListener('keydown', escape, true)
    return () => document.removeEventListener('keydown', escape, true)
  }, [propsOpen])

  const toggleProp = (propId: string, checked: boolean): void => {
    setPropSelectionOwnerId(entity?.selection_id ?? null)
    setSelectedPropIds(current => checked
      ? current.includes(propId) ? current : [...current, propId]
      : current.filter(value => value !== propId))
  }

  const startIllustration = (): void => {
    if (illustrationsDisabled || blocked || !characterId || !illustrationCatalog.catalog || !entity) return
    setIllustrationOwner({ selectionId: entity.selection_id, characterId, label: entityLabel })
    setIllustrationReport(null)
    setCompletionOpen(false)
    void assetJob.start({ kind: 'illustration', catalog_id: illustrationCatalog.catalog.catalog_id, item_ids: matchedIllustrations.map(entry => entry.illustration_id) })
  }

  const startProps = (): void => {
    if (propExportDisabled || blocked || !characterId || !propsCatalog.catalog || currentPropIds.length !== selectedPropIds.length) return
    setPropsOwnerSelectionId(entity?.selection_id ?? null)
    void assetJob.start({ kind: 'display_prop', catalog_id: propsCatalog.catalog.catalog_id, item_ids: currentPropIds })
  }

  const cancelAssetJob = (): void => { void assetJob.cancel() }
  const closeProps = (): void => { setPropsOpen(false); trigger.current?.focus({ preventScroll: true }) }
  const propsStyle: CSSProperties = { left: anchor.left, top: anchor.top, '--operator-props-width': `${anchor.width}px`, '--operator-props-viewport-left': `${anchor.viewportLeft}px`, '--operator-props-panel-top': `${anchor.panelTop}px`, '--operator-props-panel-height': `${anchor.panelHeight}px` } as CSSProperties

  return <>
    <div ref={host} className="operator-assets-overlay" aria-hidden={!active}>
      {active && characterId && entity && <div className={`operator-props-anchor ${propsOpen ? 'is-open' : ''}`} style={propsStyle}>
        <button ref={trigger} type="button" data-testid="operator-props-toggle" className={`operator-props-tab ${propCount === 0 ? 'is-empty' : ''}`} aria-expanded={propsOpen} aria-controls="operator-props-panel" disabled={disabledByTask} onClick={() => {
          if (!propsOpen) onPropsOpen()
          setPropsOpen(value => !value)
        }}><b>{propsOpen ? '◂' : '▸'}</b><small>道具 {propCount}</small></button>
        {propsOpen && <aside ref={panel} tabIndex={-1} className="operator-props-panel" id="operator-props-panel" data-testid="operator-props-panel" aria-label="关联展示道具">
          <header><small>ASSOCIATED PROPS</small><button type="button" data-testid="operator-props-close" aria-label="收起关联展示道具" onClick={closeProps}>◂</button><h3>关联展示道具</h3><p>{entityLabel}<br /><code>{characterId}</code></p><span className="operator-props-count">已关联 {propCount} 项展示道具</span></header>
          <div className="operator-props-scroll">
            {propsCatalog.loading && <p role="status">正在读取真实绑定目录…</p>}
            {!propsCatalog.loading && propsCatalogError && <div className="operator-assets-error" role="alert"><b>展示道具目录读取失败</b><p>{propsCatalogError}</p><button type="button" disabled={disabledByTask} onClick={propsCatalog.reload}>重新读取目录 ↻</button></div>}
            {!propsCatalog.loading && !propsCatalogError && !propsCatalog.catalog && <p role="status">等待展示道具目录载入…</p>}
            {!propsCatalog.loading && !propsCatalogError && propsCatalog.catalog && !propCount && <p className="operator-props-empty">NO ASSOCIATED PROPS<br />当前实体无已登记的关联展示道具</p>}
            {selectableProps.map(entry => <PropRecord key={entry.prop_id} entry={entry} characterId={characterId} selected={currentPropIds.includes(entry.prop_id)} disabled={propSelectionDisabled} onChange={toggleProp} />)}
            {(restrictedProps.length > 0 || unresolvedProps.length > 0) && <details className="operator-props-restricted" open>
              <summary>未解析 / 资源受限项 · {restrictedProps.length + unresolvedProps.length}</summary>
              {restrictedProps.map(entry => <article className="operator-prop-record is-restricted" key={entry.prop_id} data-testid="operator-prop-restricted"><span className="operator-prop-record-title"><b title={resourceLabel(entry.resource_path)}>{resourceLabel(entry.resource_path)}</b><small>{restrictedStatus(entry.provider_status)}</small></span><code>{entry.prop_id}</code><p>不可选择 · {entry.resource_path}<br />关联依据：{propBindingsFor(entry, characterId).map(binding => `${binding.mount_field}: ${binding.mount_point} · ${binding.prefab_path}`).join(' · ')}</p></article>)}
              {unresolvedProps.map((binding, index) => <UnresolvedPropRecord key={`${binding.character_id}-${binding.prefab_path}-${binding.mount_field}-${binding.item_index}-${index}`} binding={binding} />)}
            </details>}
            {propReceiptVisible && activeJob && <section className={`operator-prop-receipt ${activeJob.state}`} data-testid="operator-prop-receipt" role="status">
              <strong>{activeJob.state === 'completed' ? '道具导出完成' : activeJob.state === 'partial' ? '部分完成 · 待处理' : activeJob.state === 'cancelled' ? '任务已取消' : '任务失败'}</strong>
              <small>OUTPUT // STATIC DISPLAY PROP .BLEND</small>
              <p>当前关联角色：{entityLabel} · {characterId}</p>
              {propResults.map(result => <div className="operator-prop-receipt-item" key={result.prop_id}>
                <b>{result.prop_id}</b><code>{result.blend.path}</code><span>{result.blend.bytes.toLocaleString()} B · SHA-256 {result.blend.sha256}</span>
                <p><b>PREPARATION RECEIPT</b> · {result.preparation_receipt}</p>
                <p><b>BUILD RECEIPT</b> · {result.build_receipt}</p>
                <p><b>REOPEN AUDIT</b> · {result.reopen_audit}</p>
                {result.pending.map((item, index) => <p key={`${result.prop_id}-${index}`}>{item}</p>)}
                {result.limitations.map((item, index) => <p key={`${result.prop_id}-limitation-${index}`}>{item}</p>)}
              </div>)}
              {activeJob.result && <p>输出目录：{activeJob.result.output_directory}</p>}
              {(activeJob.error || propsJobError) && <p className="operator-prop-error">{activeJob.error?.message ?? propsJobError}{activeJob.error?.diagnostic_id ? ` · ${activeJob.error.diagnostic_id}` : ''}</p>}
              {activeJob.state === 'cancelled' && <p>后端已确认任务取消。</p>}
            </section>}
          </div>
          <footer><small>已选择 {currentPropIds.length} 项 · 默认不选</small><button type="button" className="operator-props-export" data-testid="operator-props-export" disabled={propExportDisabled} onClick={startProps}>{localBusy && jobKind === 'display_prop' ? `正在构建道具 .blend · ${progress}%` : `提取选中道具 .blend (${currentPropIds.length}) →`}</button>{localBusy && jobKind === 'display_prop' && <div className="operator-assets-progress"><i style={{ width: `${progress}%` }} /></div>}{localBusy && jobKind === 'display_prop' && <button type="button" className="operator-assets-cancel" data-testid="operator-props-cancel" disabled={assetJob.pending || assetJob.cancelPending || assetJob.cancelRequested} onClick={cancelAssetJob}>{assetJob.cancelRequested ? '正在取消…' : assetJob.pending ? '校验中…' : '取消任务'}</button>}</footer>
        </aside>}
      </div>}
    </div>
    {dock && active && createPortal(<>
      <div className="operator-asset-action-status"><p>{dockMessage || '配置就绪 · 提交后由后端验证'}</p><button ref={illustrationButton} type="button" data-testid="operator-illustration-export" title="提取当前角色全部关联原尺寸立绘 PNG" disabled={illustrationsDisabled} className={`operator-illustration-export ${localBusy && jobKind === 'illustration' ? 'running' : ''}`} style={{ '--operator-asset-progress': `${progress}%` } as CSSProperties} onClick={startIllustration}><IllustrationIcon /><span>{illustrationLabel}</span><b>→</b></button></div>
      {illustrationCatalogError && <div className="operator-asset-inline-error" role="alert"><span>{illustrationCatalogError}</span><button type="button" disabled={disabledByTask} onClick={illustrationCatalog.reload}>重新读取 ↻</button></div>}
      {assetJob.error && !localBusy && <div className="operator-asset-inline-error" role="alert"><span>{assetJob.error}</span></div>}
      {illustrationJobTerminal && illustrationReport && (illustrationOwner?.selectionId === entity?.selection_id || requestedIllustrationsMatchSelection) && <p className="operator-asset-inline-result" data-testid="operator-illustration-result" role="status">{illustrationReport.state === 'completed' ? `已导出 ${illustrationReport.files.length} 张立绘 PNG` : illustrationReport.state === 'partial' ? `部分立绘已导出 · ${illustrationReport.files.length} 张` : illustrationReport.state === 'failed' ? `立绘提取失败：${illustrationReport.error ?? assetJob.error ?? '后端任务失败'}` : '立绘提取任务已取消'}</p>}
      {localBusy && <div className="operator-asset-job-actions"><span>{assetJob.pending ? '正在验证请求…' : `${activeJob?.phase ?? '资产任务'} · ${progress}%`}</span><button type="button" data-testid="operator-asset-cancel" disabled={assetJob.pending || assetJob.cancelPending || assetJob.cancelRequested} onClick={cancelAssetJob}>{assetJob.cancelRequested ? '正在取消…' : assetJob.pending ? '校验中…' : '取消任务'}</button></div>}
      {assetJob.error && localBusy && <div className="operator-asset-inline-error" role="alert"><span>{assetJob.error}</span><button type="button" onClick={assetJob.reconnect}>重连任务 ↻</button></div>}
    </>, dock)}
    {active && illustrationReport && (illustrationOwner?.selectionId === entity?.selection_id || requestedIllustrationsMatchSelection) && <OperatorCompletion report={illustrationReport} open={completionOpen} reduced={reduced} onClose={() => { setCompletionOpen(false); illustrationButton.current?.focus({ preventScroll: true }) }} onCopy={onCopy} />}
  </>
}
