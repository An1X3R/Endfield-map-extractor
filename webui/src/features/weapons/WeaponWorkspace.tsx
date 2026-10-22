import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useReducedMotion } from 'motion/react'
import { useDraggablePanel } from '../../hooks/useDraggablePanel'
import { useAssetCatalog } from '../assets/useAssetCatalog'
import { useAssetJob } from '../assets/useAssetJob'
import type { AssetExportSelection, AssetWeaponResult, WeaponEntry, WeaponVariant } from '../assets/types'
import { WeaponCompletion } from './WeaponCompletion'
import type { WeaponCompletionReport } from './WeaponCompletion'
import { matchesWeaponQuery } from './search'
import './weapons.css'

type Props = {
  active: boolean
  blocked: boolean
  resetRevision: number
  taskReturnRevision: number
  previewRevision: number
  onCatalogCountChange: (count: number | null) => void
  onTaskChange: (text: string | null) => void
  onBusyChange: (busy: boolean) => void
  onNotify: (message: string) => void
}

type VariantRecord = WeaponEntry['variants'][number]

const VARIANT_LABELS: Record<WeaponVariant, string> = {
  base: '基础外观',
  refined_candidate: '精炼候选外观',
}

const MODEL_STATUS_LABELS: Record<WeaponEntry['model_status'], string> = {
  resolved: '源资源已解析',
  missing_resource: '源资源缺失',
  ambiguous_resource: '源资源有歧义',
  missing_provider: '资源提供方缺失',
  ambiguous_provider: '资源提供方有歧义',
}

const NAME_STATUS_LABELS: Record<WeaponEntry['name_status'], string> = {
  resolved: '名称已解析',
  missing_item: 'ItemTable 条目缺失',
  missing_text: '中文文本缺失',
}

const variantFor = (entry: WeaponEntry, itemId: string | null): VariantRecord | null =>
  itemId === null ? null : entry.variants.find(variant => variant.item_id === itemId) ?? null

const initialVariant = (entry: WeaponEntry): VariantRecord | null =>
  entry.variants.find(variant => variant.variant === 'base') ?? entry.variants[0] ?? null

const isWeaponResult = (item: { format: string }): item is AssetWeaponResult =>
  item.format === 'EndfieldWeaponExport/1'

const reportFor = (
  job: NonNullable<ReturnType<typeof useAssetJob>['job']>,
  entries: readonly WeaponEntry[],
): WeaponCompletionReport | null => {
  if (job.kind !== 'weapon' || job.request.kind !== 'weapon' ||
    (job.state !== 'completed' && job.state !== 'partial' && job.state !== 'failed' && job.state !== 'cancelled')) return null

  const itemId = job.request.item_ids[0]
  const resultItem = job.result?.items.find(isWeaponResult) ?? null
  const entry = entries.find(candidate => candidate.weapon_id === resultItem?.weapon_id ||
    candidate.variants.some(variant => variant.item_id === itemId)) ?? null
  const variantRecord = entry?.variants.find(variant => variant.item_id === itemId) ?? null
  const variant = resultItem?.variant ?? job.request.variant
  const successful = job.state === 'completed' || job.state === 'partial'
  const artifacts = resultItem ? [
    { kind: 'blend' as const, path: resultItem.blend.path },
    { kind: 'preparation_receipt' as const, path: resultItem.preparation_receipt },
    { kind: 'build_receipt' as const, path: resultItem.build_receipt },
    { kind: 'reopen_audit' as const, path: resultItem.reopen_audit },
  ] : []

  return {
    state: job.state,
    weaponId: resultItem?.weapon_id ?? entry?.weapon_id ?? null,
    name: resultItem?.name ?? entry?.name ?? entry?.weapon_id ?? itemId,
    itemId,
    variant,
    rarity: entry?.rarity ?? null,
    variantSemantics: resultItem?.variant_semantics ?? variantRecord?.semantic_status ?? null,
    sourceResource: resultItem?.source_resource ?? variantRecord?.resource_path ?? null,
    outputDirectory: job.result?.output_directory ?? null,
    reopenAudit: resultItem?.reopen_audit ? 'passed' : successful ? 'receipt_missing' : 'not_generated',
    artifacts,
    pending: resultItem?.pending ?? [],
    limitations: resultItem?.limitations ?? [],
    error: job.error ? `${job.error.message} · diagnostic_id=${job.error.diagnostic_id}` : null,
    development: false,
  }
}

function BlockTitle({ number, title, code }: { number: string; title: string; code: string }) {
  return <div className="block-title"><span>{number}</span><h3>{title}<small>{code}</small></h3></div>
}

export function WeaponWorkspace({ active, blocked, resetRevision, taskReturnRevision, previewRevision, onCatalogCountChange, onTaskChange, onBusyChange, onNotify }: Props) {
  const catalogApi = useAssetCatalog('weapon', active)
  const jobApi = useAssetJob(['weapon'])
  const [weaponId, setWeaponId] = useState<string | null>(null)
  const [itemId, setItemId] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<number | null>(null)
  const [rarity, setRarity] = useState<number | null>(null)
  const [directoryExpanded, setDirectoryExpanded] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [completionOpen, setCompletionOpen] = useState(false)
  const [developmentReport, setDevelopmentReport] = useState<WeaponCompletionReport | null>(null)
  const preferredReducedMotion = useReducedMotion()
  const reduced = Boolean(preferredReducedMotion)
  const shell = useRef<HTMLDivElement>(null)
  const search = useRef<HTMLInputElement>(null)
  const drawerButton = useRef<HTMLButtonElement>(null)
  const exportButton = useRef<HTMLButtonElement>(null)
  const lastPreviewRevision = useRef(previewRevision)
  const lastTaskRevision = useRef(taskReturnRevision)
  const lastResetRevision = useRef(resetRevision)
  const lastBusySelectionJobId = useRef<string | null>(null)
  const shownJobId = useRef<string | null>(null)
  const drag = useDraggablePanel(shell, false)

  const catalog = catalogApi.catalog
  const entries = catalog?.entries ?? []
  const selected = entries.find(entry => entry.weapon_id === weaponId) ?? null
  const selectedVariant = selected ? variantFor(selected, itemId) : null
  const types = useMemo(() => [...new Set(entries.map(entry => entry.weapon_type))].sort((left, right) => left - right), [entries])
  const rarities = useMemo(() => [...new Set(entries.map(entry => entry.rarity))].sort((left, right) => left - right), [entries])
  const filtered = useMemo(() => entries.filter(entry =>
    (category === null || entry.weapon_type === category) &&
    (rarity === null || entry.rarity === rarity) && matchesWeaponQuery(entry, query)), [entries, category, rarity, query])
  const report = useMemo(() => jobApi.job ? reportFor(jobApi.job, entries) : null, [jobApi.job, entries])
  const activeWeaponJob = jobApi.job?.kind === 'weapon' ? jobApi.job : null
  const taskText = jobApi.busy
    ? `TASK: ${activeWeaponJob?.state.toUpperCase() ?? (jobApi.pending ? 'VALIDATING' : 'CONNECTING')} ${activeWeaponJob ? `${Math.round(activeWeaponJob.progress * 100)}% · ${activeWeaponJob.message}` : 'WEAPON ASSET EXPORT'}`
    : null
  const busy = jobApi.busy
  const selection: AssetExportSelection | null = catalog && selected && selectedVariant ? {
    kind: 'weapon',
    catalog_id: catalog.catalog_id,
    item_ids: [selectedVariant.item_id],
    variant: selectedVariant.variant,
  } : null
  const canExport = active && selection !== null && selected?.model_status === 'resolved' &&
    !blocked && !busy && !catalogApi.loading && !catalogApi.error
  const selectedIndex = selected ? entries.findIndex(entry => entry.weapon_id === selected.weapon_id) + 1 : -1
  const selectedName = selected?.name ?? selected?.weapon_id ?? '等待指定武器档案'

  useEffect(() => { onCatalogCountChange(catalog?.entries.length ?? null) }, [catalog, onCatalogCountChange])
  useLayoutEffect(() => { onBusyChange(busy) }, [busy, onBusyChange])
  useLayoutEffect(() => () => onBusyChange(false), [onBusyChange])
  useLayoutEffect(() => { onTaskChange(taskText) }, [taskText, onTaskChange])
  useLayoutEffect(() => () => onTaskChange(null), [onTaskChange])
  useEffect(() => {
    if (jobApi.error) onNotify(jobApi.error)
  }, [jobApi.error, onNotify])

  useEffect(() => {
    if (!catalog) return
    const exists = catalog.entries.some(entry => entry.weapon_id === weaponId)
    if (exists) {
      const currentEntry = catalog.entries.find(entry => entry.weapon_id === weaponId)
      if (currentEntry && variantFor(currentEntry, itemId)) return
      setItemId(currentEntry ? initialVariant(currentEntry)?.item_id ?? null : null)
      return
    }
    const first = catalog.entries[0] ?? null
    setWeaponId(first?.weapon_id ?? null)
    setItemId(first ? initialVariant(first)?.item_id ?? null : null)
  }, [catalog, weaponId, itemId])

  useEffect(() => {
    if (resetRevision <= lastResetRevision.current) return
    lastResetRevision.current = resetRevision
    const first = catalog?.entries[0] ?? null
    setWeaponId(first?.weapon_id ?? null)
    setItemId(first ? initialVariant(first)?.item_id ?? null : null)
    setQuery('')
    setCategory(null)
    setRarity(null)
    setDirectoryExpanded(false)
    setDrawerOpen(false)
    setCompletionOpen(false)
    setDevelopmentReport(null)
    drag.reset()
  }, [resetRevision, catalog, drag.reset])

  useEffect(() => {
    if (!drawerOpen && !directoryExpanded) return
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape') return
      setDrawerOpen(false)
      setDirectoryExpanded(false)
      drawerButton.current?.focus()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [drawerOpen, directoryExpanded])

  useEffect(() => {
    if (!active || !report || !jobApi.job || shownJobId.current === jobApi.job.job_id) return
    shownJobId.current = jobApi.job.job_id
    setDevelopmentReport(null)
    setCompletionOpen(true)
  }, [active, report, jobApi.job])

  useEffect(() => {
    const returnRequested = active && taskReturnRevision > lastTaskRevision.current
    const restoreBusyJob = jobApi.busy && activeWeaponJob !== null &&
      lastBusySelectionJobId.current !== activeWeaponJob.job_id
    if ((!returnRequested && !restoreBusyJob) || !activeWeaponJob || !catalog || activeWeaponJob.request.kind !== 'weapon') return

    const request = activeWeaponJob.request
    const requestedItemId = request.item_ids[0]
    const matchingEntry = catalog.catalog_id === request.catalog_id
      ? entries.find(entry => entry.variants.some(variant =>
        variant.item_id === requestedItemId && variant.variant === request.variant)) ?? null
      : null
    if (matchingEntry) {
      setWeaponId(matchingEntry.weapon_id)
      setItemId(requestedItemId)
    }

    if (restoreBusyJob) lastBusySelectionJobId.current = activeWeaponJob.job_id
    if (returnRequested) {
      lastTaskRevision.current = taskReturnRevision
      if (report) setCompletionOpen(true)
    }
  }, [active, taskReturnRevision, activeWeaponJob, catalog, entries, jobApi.busy, report])

  useEffect(() => {
    if (!active || previewRevision <= lastPreviewRevision.current) return
    lastPreviewRevision.current = previewRevision
    if (!import.meta.env.DEV) return
    if (!selected || !selectedVariant) {
      onNotify('武器目录没有可用于开发预览的真实条目。')
      return
    }
    setDevelopmentReport({
      state: 'completed',
      weaponId: selected.weapon_id,
      name: selected.name ?? selected.weapon_id,
      itemId: selectedVariant.item_id,
      variant: selectedVariant.variant,
      rarity: selected.rarity,
      variantSemantics: selectedVariant.semantic_status,
      sourceResource: selectedVariant.resource_path,
      outputDirectory: null,
      reopenAudit: 'not_generated',
      artifacts: [],
      pending: [],
      limitations: ['开发预览不会提交任务或生成文件。'],
      error: null,
      development: true,
    })
    setCompletionOpen(true)
  }, [active, previewRevision, selected, selectedVariant, onNotify])

  const selectWeapon = (entry: WeaponEntry): void => {
    const nextVariant = initialVariant(entry)
    setWeaponId(entry.weapon_id)
    setItemId(nextVariant?.item_id ?? null)
    setDevelopmentReport(null)
  }

  const selectVariant = (variant: VariantRecord): void => {
    setItemId(variant.item_id)
    setDevelopmentReport(null)
  }

  const startExport = async (): Promise<void> => {
    if (!selection || blocked || jobApi.busy) return
    try {
      await jobApi.start(selection)
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error))
    }
  }

  const cancelExport = async (): Promise<void> => {
    try {
      await jobApi.cancel()
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error))
    }
  }

  const terminalReport = developmentReport ?? report
  const modelStatus = selected ? MODEL_STATUS_LABELS[selected.model_status] : '未选择武器'
  const readiness = catalogApi.loading ? 'CATALOG_LOADING' : catalogApi.error ? 'CATALOG_ERROR' : catalog ? 'CATALOG_READY' : 'CATALOG_STANDBY'
  const actionText = jobApi.busy
    ? activeWeaponJob?.message ?? (jobApi.pending ? '正在校验并提交武器目录请求…' : '正在连接资产任务…')
    : blocked ? '全局已有资产任务运行中，当前不能提交。'
      : catalogApi.loading ? '正在读取武器目录，暂不能提交。'
        : catalogApi.error ? '目录读取失败，重新读取后再提交。'
          : !selected ? '请先从真实资产目录选择武器。'
            : !selectedVariant ? '当前武器没有可提交的 base 或 refined_candidate item_id。'
              : selected.model_status !== 'resolved' ? `${modelStatus} · 当前不能提交。`
                : `${modelStatus} · 后端会校验源目录、Blender 与输出条件。`

  return (
    <div className="operator-workspace-shell weapon-workspace-shell weapon-workspace" data-testid="weapon-workspace" hidden={!active} inert={!active}>
      <div className="datum-floor" aria-hidden="true"><i/><span>STATIC ASSET / ENGINEERING REGISTRY</span></div>
      <div className="workspace-line"><span>WEAPON ARCHIVE <i>/</i> 武器工程档案</span><span>STATIC .BLEND + VERIFIED PACKED MATERIALS</span><button ref={drawerButton} type="button" data-testid="weapon-drawer-open" className="weapon-drawer-open text-button" onClick={() => setDrawerOpen(true)}>提取设置 ↗</button></div>
      <div className={`workspace workspace-${directoryExpanded ? 'directory-expanded' : 'compact'}`} ref={shell}>
        <button type="button" className="settings-drawer-backdrop" data-testid="weapon-drawer-backdrop" aria-label="关闭提取设置" hidden={!drawerOpen} onClick={() => { setDrawerOpen(false); drawerButton.current?.focus() }}/>
        <aside className="directory" aria-label="武器目录">
          <div className="section-title"><div><small>INDEX / {catalog ? String(entries.length).padStart(2, '0') : '—'}</small><h2>武器目录</h2></div><span>{catalog ? entries.length : '—'}<small>WEAPONS</small></span></div>
          <button type="button" className="catalog-expand-toggle" data-testid="weapon-directory-expand" aria-expanded={directoryExpanded} onClick={() => setDirectoryExpanded(value => !value)}>{directoryExpanded ? '收起目录 ↙' : '展开目录 ↗'}<small>COMPACT INDEX</small></button>
          <label className="search-field"><span aria-hidden="true">⌕</span><input ref={search} type="search" data-testid="weapon-search" placeholder="名称、拼音首字母、ID、源路径" aria-label="检索武器名称、拼音首字母、ID 与源路径" autoComplete="off" value={query} onChange={event => setQuery(event.currentTarget.value)}/><kbd>/</kbd></label>
          <div className="weapon-filters">
            <div role="group" aria-label="按原始武器类型筛选"><button type="button" data-testid="weapon-type-all" aria-pressed={category === null} onClick={() => setCategory(null)}>全部</button>{types.map(value => <button type="button" data-testid={`weapon-type-${value}`} aria-pressed={category === value} key={value} onClick={() => setCategory(value)}>TYPE {value}</button>)}</div>
            <label>原始稀有度 <select data-testid="weapon-rarity" value={rarity === null ? 'all' : String(rarity)} onChange={event => setRarity(event.currentTarget.value === 'all' ? null : Number(event.currentTarget.value))}><option value="all">全部</option>{rarities.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
          </div>
          <div className="directory-content" aria-live="polite">
            {catalogApi.loading && <p className="no-results" role="status">正在读取真实武器目录…</p>}
            {!catalogApi.loading && catalogApi.error && <div className="weapon-catalog-error" role="alert"><b>WEAPON_CATALOG_UNAVAILABLE</b><p>{catalogApi.error}</p><button type="button" onClick={catalogApi.reload}>重新读取目录</button></div>}
            {!catalogApi.loading && !catalogApi.error && catalog && entries.length === 0 && <p className="no-results">真实目录没有武器资源。</p>}
            {!catalogApi.loading && !catalogApi.error && catalog && entries.length > 0 && <div className="entity-tiles">
              {filtered.map(entry => <button type="button" className={`entity-row weapon-row ${entry.weapon_id === weaponId ? 'selected' : ''}`} data-testid={`weapon-row-${entry.weapon_id}`} key={entry.weapon_id} disabled={busy} aria-pressed={entry.weapon_id === weaponId} onClick={() => selectWeapon(entry)}>
                <span className="weapon-index">{String(entries.indexOf(entry) + 1).padStart(2, '0')}</span>
                <span className="entity-copy"><b>{entry.name ?? entry.weapon_id}</b><small>{entry.weapon_id}</small><small>TYPE RAW {entry.weapon_type} / RARITY RAW {entry.rarity}</small></span>
                <span className={`entity-state ${entry.model_status === 'resolved' ? '' : 'weapon-state-missing'}`}>{entry.weapon_id === weaponId ? 'SELECTED' : entry.model_status === 'resolved' ? 'DISCOVERED' : 'SOURCE ISSUE'}</span>
              </button>)}
              {filtered.length === 0 && <p className="no-results">没有匹配的武器记录。</p>}
            </div>}
          </div>
          <div className="directory-footnote"><span>LOCAL SOURCE CATALOG</span><span>RAW RARITY / NO REMAPPING</span></div>
          <div className={`readiness weapon-readiness ${catalogApi.error ? 'not-ready' : ''}`}>
            <div className="readiness-header">目录与源资源状态 <span className="readiness-state">{readiness}</span></div>
            <div className="readiness-grid"><span>CATALOG <b>{catalog ? '真实数据' : catalogApi.loading ? '读取中' : '未连接'}</b></span><span>SELECTION <b>{selectedVariant ? selectedVariant.variant : '必选'}</b></span><span>MODEL <b>{selected?.model_status ?? '—'}</b></span><span>EXPORT <b>后端验证</b></span></div>
            <button type="button" data-testid="weapon-environment" onClick={() => { setDrawerOpen(true); requestAnimationFrame(() => drag.panelRef.current?.focus()) }}>查看提交与源状态 →</button>
          </div>
        </aside>

        <section className="archive weapon-archive" hidden={directoryExpanded} aria-live="polite">
          <div className="archive-top"><span>ARCHIVE // {selected?.weapon_id ?? (catalogApi.error ? 'CATALOG_ERROR' : catalogApi.loading ? 'CATALOG_LOADING' : 'STANDBY')}</span><span>{readiness} <i/></span></div>
          {selected ? <>
            <div className="archive-heading"><div><small>WEAPON ARCHIVE // TYPE RAW {selected.weapon_type}</small><h1>{selected.weapon_id.toUpperCase()}</h1><h2>{selected.name ?? '源中文名称缺失'}</h2></div><span className="archive-number">{String(selectedIndex).padStart(2, '0')}</span></div>
            <div className="raw-rarity"><span>RARITY / RAW</span><strong>{selected.rarity}</strong><span className="raw-rarity-marks" aria-label={`原始稀有度 ${selected.rarity}`} aria-hidden="true">{Array.from({ length: selected.rarity }, (_, index) => <i key={index}/>)}</span><small>原样展示后端字段</small></div>
            <div className="weapon-nameplate"><span>// ENDFIELD WEAPON SPECIFICATION</span><div><small>WEAPON_ID</small><strong>{selected.weapon_id}</strong></div><div><small>ACTIVE_ITEM_ID</small><strong className="weapon-item-id" data-testid="weapon-active-item">{selectedVariant?.item_id ?? '请选择可用变体'}</strong></div><div className="weapon-plate-footer"><span>TYPE RAW {selected.weapon_type}</span><span>{selectedVariant?.variant.toUpperCase() ?? 'VARIANT REQUIRED'}</span><span>{selected.model_status.toUpperCase()}</span></div></div>
            <div className="weapon-spec-grid">
              <section><small>01 / VARIANT REGISTRY</small><h3>变体资源对照</h3>{selected.variants.map(variant => <button type="button" data-testid={`weapon-matrix-${variant.variant}`} aria-pressed={selectedVariant?.item_id === variant.item_id} key={variant.item_id} disabled={busy} onClick={() => selectVariant(variant)}><b>{variant.variant === 'base' ? 'BASE / 基础外观' : 'REFINED_CANDIDATE / 精炼候选'}</b><code>{variant.item_id}</code><span>{variant.semantic_status === 'source_base' ? '源基础外观' : '未验证候选资源'}</span><small>{variant.resource_path}</small></button>)}{selected.variants.length === 0 && <p>目录未提供可提交的变体资源。</p>}<p>候选外观只反映源资源存在，不代表游戏内已解锁。</p></section>
              <section><small>02 / PIPELINE SPECIFICATION</small><h3>管线与输出规格</h3><dl><dt>FORMAT</dt><dd>Static Blender .blend</dd><dt>MATERIALS</dt><dd>Packed and reopen-audited</dd><dt>MODEL SOURCE</dt><dd>{selected.model_status}</dd><dt>ANIMATION</dt><dd>静态资产，不包含动作帧</dd></dl><p>名称状态：{NAME_STATUS_LABELS[selected.name_status]}。源路径与回执以实际任务报告为准。</p></section>
            </div>
          </> : <div className="weapon-empty-stage"><small>{catalogApi.loading ? 'CATALOG / LOADING' : catalogApi.error ? 'CATALOG / UNAVAILABLE' : catalog?.entries.length === 0 ? 'NO WEAPON ASSET' : 'WEAPON / STANDBY'}</small><h3>{catalogApi.loading ? '正在读取武器档案' : catalogApi.error ? '武器目录暂不可用' : catalog?.entries.length === 0 ? '当前目录没有可用武器' : '等待指定武器档案'}</h3><p>{catalogApi.loading ? '目录载入后将显示真实 weapon_id、item_id 与源字段。' : catalogApi.error ? '修复资产服务连接后可重新读取真实目录。' : catalog?.entries.length === 0 ? '当前游戏源没有返回武器条目。' : '请从左侧目录选择一项真实武器资源。'}</p></div>}
          <div className="archive-bottom"><span>WEAPON WORKSPACE / {catalog?.format ?? 'EndfieldWeaponCatalog/1'}</span><span>STATIC WEAPON PIPELINE</span></div>
        </section>

        <aside ref={drag.panelRef} tabIndex={-1} className={`settings ${drawerOpen ? 'drawer-visible' : ''} ${drag.isDragging ? 'dragging' : ''}`} data-testid="weapon-settings">
          <header ref={drag.headerRef} className="settings-header" tabIndex={0} data-testid="weapon-drag-handle" aria-label="提取设置，方向键移动，Home 复位" onPointerDown={drag.onPointerDown} onPointerMove={drag.onPointerMove} onPointerUp={drag.onPointerUp} onPointerCancel={drag.onPointerCancel} onLostPointerCapture={drag.onLostPointerCapture} onKeyDown={drag.onKeyDown}><div><small>EXPORT CONFIGURATION</small><h2>提取设置</h2></div><div><button type="button" data-testid="weapon-panel-reset" className="text-button" onClick={drag.reset}>复位</button><button type="button" data-testid="weapon-drawer-close" className="weapon-drawer-close icon-button" aria-label="关闭设置" onClick={() => { setDrawerOpen(false); drawerButton.current?.focus() }}>×</button></div></header>
          <div className="settings-body">
            <section className="config-block"><BlockTitle number="01" title="外观变体选择" code="REQUIRED / SOURCE ITEM_ID"/>{selected?.variants.map(variant => <label className={`variant-option ${selectedVariant?.item_id === variant.item_id ? 'selected' : ''}`} key={variant.item_id}><input type="radio" name="weapon-variant" data-testid={`weapon-variant-${variant.variant}`} checked={selectedVariant?.item_id === variant.item_id} disabled={busy} onChange={() => selectVariant(variant)}/><span>{VARIANT_LABELS[variant.variant]}<small>{variant.variant.toUpperCase()}<br/>{variant.item_id}</small></span><b>{variant.semantic_status === 'source_base' ? 'SOURCE BASE' : 'UNVERIFIED'}</b></label>)}{selected && selected.variants.length === 0 && <p className="weapon-config-empty">该武器没有可选外观。</p>}<p className="help">选择要导出的外观；精炼候选尚未确认游戏内语义。</p></section>
            <section className="config-block"><BlockTitle number="02" title="资产包含内容" code="STATIC WEAPON PACKAGE"/><ul className="weapon-package-list"><li>独立静态网格模型 <b>.blend</b></li><li>打包材质与源纹理</li><li>重开检查回执</li></ul><p className="help">输出是否完整以真实任务回执为准。武器资产不包含动作帧预算。</p></section>
            <section className="config-block"><BlockTitle number="03" title="目录与环境校验" code="CATALOG / SOURCE / BLENDER"/><div className="weapon-source-state"><span>WEAPON_ID</span><strong>{selected?.weapon_id ?? '—'}</strong><span>MODEL STATUS</span><strong>{selected?.model_status ?? '—'}</strong><span>NAME STATUS</span><strong>{selected ? selected.name_status : '—'}</strong><span>VARIANT</span><strong>{selectedVariant?.variant ?? '必选'}</strong></div><p className="help">角色独立准备契约 · 无需先提取地图。提交前后端会重新验证源目录、Blender 与输出条件。</p>{catalogApi.error && <div className="weapon-operation-error" role="alert">{catalogApi.error}</div>}</section>
            {jobApi.error && <section className="weapon-operation-error" role="alert"><b>ASSET JOB ERROR</b><p>{jobApi.error}</p><button type="button" onClick={jobApi.reconnect}>重新连接任务</button></section>}
            {report && <section className={`result ${report.state}`} data-testid="weapon-result"><strong>{report.state.toUpperCase()}</strong><p>{report.itemId}</p>{report.error && <p>{report.error}</p>}<button type="button" data-testid="weapon-replay-result" onClick={() => setCompletionOpen(true)}>查看任务回执 / 重播 →</button></section>}
          </div>
          <div className="action-dock"><p>{actionText}</p><button ref={exportButton} type="button" data-testid="weapon-export" className={`export-button ${busy ? 'running' : ''}`} disabled={!canExport} onClick={() => { void startExport() }}><span>{busy ? jobApi.pending ? '正在校验并提交…' : '正在构建武器 .blend…' : '构建武器 .blend 资产包'}</span><b>{busy ? '…' : '→'}</b></button>{busy && activeWeaponJob && <button type="button" data-testid="weapon-cancel" className="text-button" disabled={jobApi.cancelPending} onClick={() => { void cancelExport() }}>{jobApi.cancelPending ? '正在请求取消…' : '取消任务 ×'}</button>}</div>
        </aside>
      </div>
      <div className="mobile-action"><button type="button" data-testid="weapon-mobile-settings" className="export-button" onClick={() => setDrawerOpen(true)}>提取设置 / 武器资产包 →</button></div>
      {terminalReport && <WeaponCompletion open={completionOpen && active} report={terminalReport} reduced={reduced} onClose={() => { setCompletionOpen(false); setDevelopmentReport(null); exportButton.current?.focus() }}/ >}
    </div>
  )
}
