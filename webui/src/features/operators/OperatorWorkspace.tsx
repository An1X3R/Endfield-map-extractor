import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { useReducedMotion } from 'motion/react'
import { useDraggablePanel } from '../../hooks/useDraggablePanel'
import { isJsonObject, postJson } from '../../lib/backendClient'
import { useOperators } from './useOperators'
import { MaterialSettings } from './MaterialSettings'
import { describeMaterials, originalMaterials, simplifiedMaterialRestriction } from './materials'
import type { CharacterMaterialOptions } from './types'
import { ActionCatalog } from './ActionCatalog'
import type { ActionCatalogOption } from './ActionCatalog'
import { OperatorCompletion } from './OperatorCompletion'
import type { OperatorReport } from './OperatorCompletion'
import type { CharacterAssetCategory, CharacterExportRequest, CharacterPreparationSource, CharacterSummary } from './types'
import type { OperatorDevelopmentPreview } from './developmentPreview'
import { revealOperators } from './motion'
import { getOperatorPortrait } from './portraits'
import ditherDefinitions from './dither.svg?raw'
import { navigateCatalog, readCatalogAnchor, restoreCatalogAnchor } from './catalogNavigation'
import type { CatalogAnchor } from './catalogNavigation'
import './operators.css'

type Props = {
  active: boolean
  developmentPreview: OperatorDevelopmentPreview | null
  resetRevision: number
  taskReturnRevision: number
  onCatalogCountChange: (count: number | null) => void
  onTaskChange: (text: string | null) => void
  onBusyChange: (busy: boolean) => void
  onNotify: (message: string) => void
}

const EMPTY_SOURCE: CharacterPreparationSource = { game_root: '', blender_exe: '', cache_root: '', output_root: '' }
const PATHS: Array<{ key: keyof CharacterPreparationSource; label: string; picker: 'game' | 'blender' | 'cache' | 'output' }> = [
  { key: 'game_root', label: 'GAME PATH', picker: 'game' },
  { key: 'blender_exe', label: 'BLENDER PATH', picker: 'blender' },
  { key: 'cache_root', label: 'CACHE PATH', picker: 'cache' },
  { key: 'output_root', label: 'OUTPUT PATH', picker: 'output' },
]
const GROUPS = [{ kind: 'playable', label: '可操控干员', code: 'PLAYABLE' }, { kind: 'independent_npc', label: 'NPC / 动物 / 生物', code: 'ENTITY' }] as const
type NpcCategoryFilter = 'all' | Exclude<CharacterAssetCategory, 'playable'>
const NPC_CATEGORY_FILTERS: readonly { value: NpcCategoryFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'npc', label: 'NPC' },
  { value: 'animal', label: '动物' },
  { value: 'creature', label: '生物 / 敌人' },
  { value: 'object', label: '其他' },
]
const ASSET_CATEGORY_LABELS: Record<CharacterAssetCategory, string> = {
  playable: 'PLAYABLE OPERATOR',
  npc: 'NPC ENTITY',
  animal: 'ANIMAL ENTITY',
  creature: 'CREATURE ENTITY',
  object: 'OBJECT ENTITY',
}
const CHINESE_COLLATOR = new Intl.Collator('zh-Hans-CN-u-co-pinyin')
const INITIAL_ANCHORS = Array.from('阿八嚓搭蛾发旮哈击喀垃妈拿噢啪期然撒塌挖夕压匝')
const INITIAL_LETTERS = 'abcdefghjklmnopqrstwxyz'

function initials(name: string): string {
  return Array.from(name).map(character => {
    if (/[a-z0-9]/i.test(character)) return character.toLowerCase()
    if (!/[\u3400-\u9fff]/.test(character)) return ''
    let index = INITIAL_ANCHORS.length - 1
    while (index > 0 && CHINESE_COLLATOR.compare(character, INITIAL_ANCHORS[index]) < 0) index -= 1
    return INITIAL_LETTERS[index]
  }).join('')
}

function entityCode(entity: CharacterSummary): string {
  return entity.entry_kind === 'playable' ? entity.character_id : entity.npc_id
}

function entityName(entity: CharacterSummary): string {
  if (entity.entry_kind === 'independent_npc' && entity.name_status !== 'resolved') {
    return `${entityCode(entity)} · ${entity.name_status === 'missing' ? '源中文名缺失' : '源中文名有歧义'}`
  }
  return entity.name || entityCode(entity)
}

function highlight(value: string, query: string): ReactNode {
  const index = value.toLowerCase().indexOf(query.toLowerCase())
  return !query || index < 0 ? value : <>{value.slice(0, index)}<mark>{value.slice(index, index + query.length)}</mark>{value.slice(index + query.length)}</>
}

function budgetPosition(value: number): number {
  const position = value <= 300 ? Math.log(value / 2) / Math.log(150) * 40 : value <= 1200 ? 40 + Math.log(value / 300) / Math.log(4) * 35 : 75 + Math.log(value / 1200) / Math.log(15) * 25
  return Number.isFinite(position) ? Math.max(0, Math.min(100, position)) : 0
}

function BlockTitle({ number, title, code }: { number: number; title: string; code: string }) {
  return <div className="block-title"><span>{String(number).padStart(2, '0')}</span><h3>{title}<small>{code}</small></h3></div>
}

function loadPreparationSource(): CharacterPreparationSource {
  const serialized = localStorage.getItem('endfield.character.paths.v1')
  if (!serialized) return EMPTY_SOURCE
  const value: object = JSON.parse(serialized)
  if (!isJsonObject(value) || PATHS.some(path => typeof value[path.key] !== 'string')) throw new TypeError('Saved character preparation paths do not match the four-path contract.')
  return { game_root: String(value.game_root), blender_exe: String(value.blender_exe), cache_root: String(value.cache_root), output_root: String(value.output_root) }
}

/** A persistent entity workspace; only explicit development fixtures bypass the API. */
export function OperatorWorkspace({ active, developmentPreview, resetRevision, taskReturnRevision, onCatalogCountChange, onTaskChange, onBusyChange, onNotify }: Props) {
  const preferredReduced = useReducedMotion()
  const [reduced, setReduced] = useState(false)
  const [selectionId, setSelectionId] = useState<string | null>(null)
  const [variantId, setVariantId] = useState<string | null>(null)
  const [actionId, setActionId] = useState('')
  const [meshOnly, setMeshOnly] = useState(false)
  const [budget, setBudget] = useState('300')
  const [mmd, setMmd] = useState(false)
  const [raw, setRaw] = useState(false)
  const [materialMode, setMaterialMode] = useState<CharacterMaterialOptions['mode']>('original')
  const [simplifiedMaterials, setSimplifiedMaterials] = useState<CharacterMaterialOptions>({ mode: 'simplified', lighting: 'simple', weather: { quality: 'simple', effects: ['wet', 'snow', 'rain'] } })
  const materials = materialMode === 'original' ? originalMaterials() : simplifiedMaterials
  const [label, setLabel] = useState('OP_EXP_01')
  const [query, setQuery] = useState('')
  const [directoryExpanded, setDirectoryExpanded] = useState(false)
  const [npcCategoryFilter, setNpcCategoryFilter] = useState<NpcCategoryFilter>('all')
  const [expanded, setExpanded] = useState<string[]>([])
  const [enteringGroup, setEnteringGroup] = useState<string | null>(null)
  const [rowLimit, setRowLimit] = useState<Record<string, number>>({ playable: 80, independent_npc: 80 })
  const [source, setSource] = useState(loadPreparationSource)
  const [drawer, setDrawer] = useState(false)
  const [previewReady, setPreviewReady] = useState(true)
  const [previewReport, setPreviewReport] = useState<OperatorReport | null>(null)
  const [previewOutcome, setPreviewOutcome] = useState<OperatorReport['state']>('completed')
  const [completionOpen, setCompletionOpen] = useState(false)
  const [pathBusy, setPathBusy] = useState<keyof CharacterPreparationSource | null>(null)
  const shell = useRef<HTMLDivElement>(null)
  const workspace = useRef<HTMLDivElement>(null)
  const directory = useRef<HTMLElement>(null)
  const directoryContent = useRef<HTMLDivElement>(null)
  const directoryToggle = useRef<HTMLButtonElement>(null)
  const directoryAnchor = useRef<CatalogAnchor | null>(null)
  const archive = useRef<HTMLElement>(null)
  const search = useRef<HTMLInputElement>(null)
  const environment = useRef<HTMLDialogElement>(null)
  const stopReveal = useRef<() => void>(() => {})
  const lastSelection = useRef(0)
  const shownReport = useRef<string | null>(null)
  const initializedSelection = useRef<string | null>(null)
  const revealedGroups = useRef(new Set<string>())
  const api = useOperators({ enabled: !developmentPreview, active: active && !developmentPreview, selectionId, variantId, preparationSource: source })
  const drag = useDraggablePanel(workspace, false)
  const catalog = developmentPreview?.catalog ?? api.catalog
  const entities = catalog?.characters ?? []
  const selected = developmentPreview ? entities.find(entity => entity.selection_id === selectionId) ?? null : api.detail?.selection_id === selectionId ? api.detail : api.selected
  const selectedPortrait = getOperatorPortrait(selected?.entry_kind === 'playable' ? selected.character_id : null)
  const variant = selected?.variants.find(item => item.variant_id === variantId) ?? null
  const actions = developmentPreview && variantId ? developmentPreview.animations[variantId] ?? [] : api.actions
  const cpuReferenceCatalog = !developmentPreview ? api.animationCatalog?.source_cpu_action_references : undefined
  const cpuReferences = cpuReferenceCatalog?.request_available ? cpuReferenceCatalog.actions.filter(item => item.request_available) : []
  const actionOptions = useMemo<ActionCatalogOption[]>(() => [
    ...actions.map(action => ({ value: action.action_id, label: action.name, group: 'action' as const, disabled: !selected?.capabilities.animation, detail: action.binding_status })),
    ...(cpuReferenceCatalog?.actions ?? []).map(reference => ({ value: `reference:${reference.reference_id}`, label: reference.state_name, group: 'reference' as const, disabled: !reference.request_available || !cpuReferenceCatalog?.request_available, detail: reference.request_available && cpuReferenceCatalog?.request_available ? '精确来源引用 · 本次导出待验收' : cpuReferenceCatalog?.status === 'exact_avatar_base_clip_references' ? cpuReferenceCatalog.request_unavailable_reason ?? '当前引用不可提交' : cpuReferenceCatalog?.unavailable_reason ?? '当前引用不可提交' })),
  ], [actions, selected?.capabilities.animation, cpuReferenceCatalog])
  const selectedAction = actions.find(action => action.action_id === actionId)
  const selectedReference = cpuReferences.find(reference => `reference:${reference.reference_id}` === actionId)
  const assetCategoryCount = new Set(entities.map(entity => entity.asset_category)).size
  const ready = Boolean(catalog?.availability.ready) && (!developmentPreview || previewReady)
  const busy = !developmentPreview && api.operationBusy
  const animationReduced = reduced || Boolean(preferredReduced)
  const numericBudget = Number(budget)
  const validBudget = meshOnly || budget.trim() !== '' && Number.isInteger(numericBudget) && numericBudget >= 2 && numericBudget <= 18000
  const validLabel = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(label)
  const matched = useMemo(() => {
    const term = query.trim().toLowerCase()
    return entities.filter(entity => [entityName(entity), entity.english_name ?? '', entityCode(entity), entity.selection_id, initials(entityName(entity))].some(value => value.toLowerCase().includes(term)))
  }, [entities, query])
  const report = useMemo<OperatorReport | null>(() => {
    if (developmentPreview) return previewReport
    const job = api.job
    if (!job || job.kind !== 'export' || job.state === 'queued' || job.state === 'running') return null
    const entry = entities.find(entity => job.request.format === 'CharacterExportRequest/1' && entity.selection_id === job.request.selection_id)
    return { portrait: getOperatorPortrait(entry?.entry_kind === 'playable' ? entry.character_id : null), id: job.job_id, state: job.state, entity: entry ? `${entry.english_name ?? entityCode(entry)} / ${entityName(entry)}` : job.job_id, output: job.result?.output_directory ?? null, files: job.result?.files ?? [], pending: job.result?.pending ?? [], error: job.error ? `${job.error.code}: ${job.error.message} · ${job.error.stage} · ${job.error.diagnostic_id}` : null, development: false, materialOptions: job.result?.material_options }
  }, [api.job, developmentPreview, previewReport, entities])
  const selectedReport = report && (developmentPreview || api.job?.request.format === 'CharacterExportRequest/1' && api.job.request.selection_id === selectionId) ? report : null

  useLayoutEffect(() => {
    if (directoryContent.current && directoryAnchor.current) restoreCatalogAnchor(directoryContent.current, directoryAnchor.current)
    directoryAnchor.current = null
  }, [directoryExpanded])

  const toggleDirectory = (): void => {
    if (directoryContent.current) directoryAnchor.current = readCatalogAnchor(directoryContent.current)
    setDirectoryExpanded(value => !value)
  }

  useEffect(() => { onCatalogCountChange(ready ? entities.length : null) }, [ready, entities.length, onCatalogCountChange])
  useEffect(() => { onBusyChange(busy) }, [busy, onBusyChange])
  useEffect(() => {
    const job = api.job
    const request = api.operationRequest
    const entity = request?.format === 'CharacterExportRequest/1' ? entities.find(row => row.selection_id === request.selection_id) : null
    onTaskChange(!developmentPreview && request ? `TASK: ${job?.state.toUpperCase() ?? 'CONNECTING'} ${entity ? entityName(entity) : request.format === 'CharacterPreparationRequest/1' ? '角色环境' : '角色导出'} ${job ? Math.round(job.progress * 100) + '%' : ''}` : null)
  }, [api.job, api.operationRequest, entities, developmentPreview, onTaskChange])
  useEffect(() => {
    if (!selected || initializedSelection.current === selected.selection_id) return
    initializedSelection.current = selected.selection_id
    setVariantId(selected.variants[0]?.variant_id ?? null)
    setActionId('')
    setMeshOnly(!selected.capabilities.animation)
    setMmd(selected.capabilities.basic_mmd)
    setRaw(false)
  }, [selected])
  useEffect(() => { if (!actionId && actions.length) setActionId(actions[0].action_id) }, [actionId, actions])
  useEffect(() => { if (!actionId && !actions.length && cpuReferences.length) { setActionId(`reference:${cpuReferences[0].reference_id}`); setMeshOnly(false) } }, [actionId, actions.length, cpuReferences])
  useEffect(() => {
    if (developmentPreview || !api.operationRequest || api.operationRequest.format !== 'CharacterExportRequest/1' || selectionId) return
    setSelectionId(api.operationRequest.selection_id)
    setVariantId(api.operationRequest.variant_id)
    setLabel(api.operationRequest.output_label)
  }, [api.operationRequest, developmentPreview, selectionId])
  useEffect(() => {
    if (!active || !report || shownReport.current === report.id) return
    shownReport.current = report.id
    if (report.state === 'completed' || report.state === 'partial') setCompletionOpen(true)
  }, [active, report])
  useEffect(() => { if (!active) { setCompletionOpen(false); setDrawer(false) } }, [active])
  useEffect(() => { drag.reset() }, [resetRevision, drag.reset])
  useEffect(() => {
    const request = api.operationRequest
    if (!taskReturnRevision || request?.format !== 'CharacterExportRequest/1') return
    setSelectionId(request.selection_id)
    setVariantId(request.variant_id)
    setLabel(request.output_label)
    const row = entities.find(item => item.selection_id === request.selection_id)
    if (row) setExpanded(current => current.includes(row.entry_kind) ? current : [...current, row.entry_kind])
  }, [taskReturnRevision, api.operationRequest, entities])
  useEffect(() => {
    if (!active) return
    stopReveal.current()
    stopReveal.current = revealOperators([directory.current, archive.current, drag.panelRef.current], [0, 40, 80], animationReduced)
    return () => stopReveal.current()
  }, [active, animationReduced, drag.panelRef])
  useEffect(() => {
    const handleKey = (event: KeyboardEvent): void => {
      if (!active || event.target instanceof Element && event.target.closest('dialog[open]')) return
      if (event.key === '/' && !(document.activeElement instanceof HTMLInputElement) && !(document.activeElement instanceof HTMLSelectElement) && !(document.activeElement instanceof HTMLTextAreaElement)) { event.preventDefault(); search.current?.focus() }
      if (event.key === 'Escape') {
        setDrawer(false)
        if (directoryExpanded) {
          if (directoryContent.current) directoryAnchor.current = readCatalogAnchor(directoryContent.current)
          setDirectoryExpanded(false)
          directoryToggle.current?.focus({ preventScroll: true })
        }
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [active, directoryExpanded])

  const select = (entity: CharacterSummary): void => {
    const now = performance.now()
    stopReveal.current()
    setSelectionId(entity.selection_id)
    setVariantId(entity.variants[0]?.variant_id ?? null)
    setActionId('')
    setPreviewReport(null)
    stopReveal.current = revealOperators([archive.current, drag.panelRef.current], [0, 40], animationReduced || now - lastSelection.current < 180)
    lastSelection.current = now
  }
  const changeVariant = (id: string): void => { setVariantId(id); setActionId(''); onNotify('变体已更改，重新读取动作目录') }
  const toggleGroup = (kind: string): void => {
    const opening = !expanded.includes(kind)
    setEnteringGroup(opening && !revealedGroups.current.has(kind) ? kind : null)
    if (opening) revealedGroups.current.add(kind)
    setExpanded(current => current.includes(kind) ? current.filter(value => value !== kind) : [...current, kind])
  }
  const copy = useCallback((text: string, confirmation: string): void => { void navigator.clipboard.writeText(text).then(() => onNotify(confirmation)).catch((error: Error) => onNotify(`复制失败：${error.message}`)) }, [onNotify])
  const materialRestriction = simplifiedMaterialRestriction(selected)
  const reason = materialMode === 'simplified' && materialRestriction ? materialRestriction : !ready ? catalog?.availability.reason ?? '请先完成角色环境准备' : !selected ? '请先在左侧选择实体档案' : !selected.capabilities.blend_export ? selected.unavailable_reason ?? '当前实体没有可用导出能力' : !variant ? '请选择有效模型变体' : !developmentPreview && api.detailLoading ? '正在读取所选实体详情' : !validBudget ? '帧预算必须为 2 ~ 18000 范围内的整数' : !meshOnly && !developmentPreview && api.actionsLoading ? '正在读取动作目录及绑定能力' : !meshOnly && !selectedReference && (!selectedAction || !selected.capabilities.animation) ? '当前动作不可提交，请选择可用动作或不附带动作导出' : !validLabel ? '输出标签需为 1–80 个字母、数字、点、下划线或连字符' : ''
  const startExport = (): void => {
    if (reason || busy || !selected || !variant || !catalog?.source_identity) return
    if (developmentPreview) { setPreviewReport(developmentPreview.createReport(selected, label, previewOutcome)); return }
    const request: CharacterExportRequest = { format: 'CharacterExportRequest/1', request_id: crypto.randomUUID(), catalog_id: catalog.catalog_id, source_identity: catalog.source_identity, selection_id: selected.selection_id, variant_id: variant.variant_id, export_profile_id: 'hgrp-preview', output_label: label, options: { materials, raw_expressions: raw && selected.capabilities.raw_expressions, basic_mmd: mmd && selected.capabilities.basic_mmd, ...(!meshOnly ? { animation: actionId.startsWith('reference:') ? { reference_id: actionId.slice(10), maximum_stored_frames: numericBudget } : { action_id: actionId, maximum_stored_frames: numericBudget } } : {}) } }
    void api.startExport(request)
  }
  const prepare = (): void => {
    localStorage.setItem('endfield.character.paths.v1', JSON.stringify(source))
    environment.current?.close()
    if (developmentPreview) { setPreviewReady(true); onNotify('开发预览环境状态已设为就绪'); return }
    void api.prepare({ format: 'CharacterPreparationRequest/1', request_id: crypto.randomUUID(), source })
  }
  const choosePath = async (path: typeof PATHS[number]): Promise<void> => {
    setPathBusy(path.key)
    try {
      const response = await postJson('/api/v2/path-picker', { kind: path.picker, current: source[path.key] }, (value): value is { status: 'selected' | 'cancelled' | 'unavailable'; path?: string; error?: string } => isJsonObject(value) && ['selected', 'cancelled', 'unavailable'].includes(String(value.status)) && (value.path === undefined || typeof value.path === 'string'))
      if (response.status === 'selected' && response.path) setSource(current => ({ ...current, [path.key]: response.path! }))
      else if (response.status !== 'cancelled') throw new Error(response.error ?? '本机路径选择器不可用，请直接输入路径。')
    } catch (error) { onNotify(`路径选择失败：${error instanceof Error ? error.message : String(error)}`) }
    finally { setPathBusy(null) }
  }
  const openEnvironment = (): void => { environment.current?.showModal() }
  const disabled = !selected || !ready || busy
  const progress = Math.round((api.job?.progress ?? 0) * 100)
  const prepJob = api.job?.kind === 'preparation' ? api.job : null

  return <div ref={shell} className="operator-workspace-shell" hidden={!active} inert={!active} data-testid="operator-workspace">
    <svg aria-hidden="true" style={{ position: 'absolute', width: 0, height: 0, overflow: 'hidden' }} dangerouslySetInnerHTML={{ __html: ditherDefinitions }} />
    <div className="datum-floor" aria-hidden="true"><div className="floor-grid" /><i /><span>DATUM // 0.000</span></div>
    <div className="workspace-line"><span>ENTITY ARCHIVE <i>/</i> {developmentPreview ? '开发视觉预览' : '角色档案'}</span><span>{developmentPreview ? 'DEV DATA · NO LIVE EXTRACTION' : 'LOCAL CATALOG / REAL JOBS'}</span><button id="drawer-open" data-testid="operator-drawer-open" className="text-button" onClick={() => setDrawer(true)}>提取设置 ↗</button><button className="text-button operator-motion-toggle" data-testid="operator-motion-toggle" aria-pressed={animationReduced} onClick={() => setReduced(value => !value)}>{animationReduced ? '动态已减少' : '减少动态'}</button></div>
    <div ref={workspace} className={`workspace ${directoryExpanded ? 'directory-expanded' : ''}`} id="operator-inner-workspace">
      <aside ref={directory} className="directory" id="directory"><div className="section-title"><div><small>INDEX / 04</small><h2>实体目录</h2></div><span id="directory-count">{catalog && ready ? matched.length : '--'}<small>ENTITIES</small></span></div><button ref={directoryToggle} type="button" data-testid="operator-directory-expand" className="catalog-expand-toggle" aria-expanded={directoryExpanded} aria-controls="directory-content" onClick={toggleDirectory}>{directoryExpanded ? '收起目录 ↙' : '展开目录 ↗'}<small>{directoryExpanded ? 'COMPACT INDEX / ESC' : 'COMPACT INDEX'}</small></button><label className="search-field"><span aria-hidden="true">⌕</span><input ref={search} id="entity-search" data-testid="operator-search" type="search" placeholder="检索名称、拼音首字母、内部代号" autoComplete="off" aria-label="检索实体" value={query} onChange={event => { setQuery(event.currentTarget.value); setRowLimit({ playable: 80, independent_npc: 80 }) }} /><kbd>/</kbd></label>
        <div ref={directoryContent} className="directory-content" id="directory-content" onKeyDown={navigateCatalog}>{api.catalogLoading && !developmentPreview ? <p className="no-results">正在读取实体目录…</p> : GROUPS.map(group => {
          const rows = matched.filter(entity => entity.entry_kind === group.kind)
          const visibleRows = group.kind === 'independent_npc' && npcCategoryFilter !== 'all'
            ? rows.filter(entity => entity.asset_category === npcCategoryFilter)
            : rows
          const open = query.trim() ? rows.length > 0 : expanded.includes(group.kind)
          if (query.trim() && !rows.length) return null
          const categoryFilters = group.kind === 'independent_npc' ? NPC_CATEGORY_FILTERS.map(filter => ({
            ...filter,
            count: filter.value === 'all' ? rows.length : rows.filter(entity => entity.asset_category === filter.value).length,
          })).filter(filter => filter.value === 'all' || filter.count > 0) : []
          return <div key={group.kind}><button id={`category-${group.kind}`} data-testid={`operator-category-${group.kind}`} className="category-button" aria-expanded={open} onClick={() => toggleGroup(group.kind)}><span><small>{group.code}</small><b>{group.label}</b></span><strong>{catalog && ready ? String(rows.length).padStart(2, '0') : '--'}</strong><i>{open ? '−' : '+'}</i></button>{open && <div className="entity-list">{group.kind === 'independent_npc' && <div className="entity-category-filters" role="group" aria-label="独立实体分类筛选">{categoryFilters.map(filter => <button key={filter.value} type="button" data-testid={`operator-npc-filter-${filter.value}`} aria-pressed={npcCategoryFilter === filter.value} onClick={() => setNpcCategoryFilter(filter.value)}><span>{filter.label}</span><small>{catalog && ready ? String(filter.count).padStart(2, '0') : '--'}</small></button>)}</div>}<div className="entity-tiles">{visibleRows.slice(0, rowLimit[group.kind]).map((entity, index) => { const portrait = getOperatorPortrait(entity.entry_kind === 'playable' ? entity.character_id : null); return <button key={entity.selection_id} id={`entity-${entity.selection_id}`} data-testid={`entity-${entity.selection_id}`} data-catalog-item="" aria-pressed={selectionId === entity.selection_id} title={`${entityName(entity)} · ${entity.english_name ?? entityCode(entity)} · ${entity.selection_id}`} className={`entity-row ${selectionId === entity.selection_id ? 'selected' : ''}`} style={{ animation: !animationReduced && enteringGroup === group.kind && index < 8 ? `row-in 80ms ${index * 10}ms both` : 'none' } as CSSProperties} disabled={!ready} onClick={() => select(entity)}><span className="avatar-mini" aria-hidden="true">{portrait ? <img src={portrait.url} width="48" height="54" alt="" loading="lazy" /> : '◇'}</span><span className="entity-copy"><b>{highlight(entityName(entity), query.trim())}</b><small>{highlight(entity.english_name ?? entityCode(entity), query.trim())}</small></span><span className={`entity-state ${entity.capabilities.blend_export ? '' : 'missing'}`} title={entity.unavailable_reason ?? undefined}>{entity.capabilities.blend_export ? selectionId === entity.selection_id ? 'SELECTED' : 'DISCOVERED' : 'UNAVAILABLE'}</span></button> })}</div>{visibleRows.length > rowLimit[group.kind] && <button className="operator-load-more" data-testid={`operator-more-${group.kind}`} onClick={() => setRowLimit(current => ({ ...current, [group.kind]: current[group.kind] + 80 }))}>继续显示 · {visibleRows.length - rowLimit[group.kind]} 项</button>}{!visibleRows.length && <p className="no-results">当前分类没有匹配实体</p>}</div>}</div>
        })}{query.trim() && !matched.length && <p className="no-results">// NO MATCHING ENTITIES FOUND<br />没有命中实体</p>}</div>
        {directoryExpanded && <div className="catalog-selection-bar" data-testid="operator-directory-current" aria-live="polite"><small>当前选择</small><strong>{selected ? entityName(selected) : '尚未选择实体'}</strong>{selected && <code>{entityCode(selected)}</code>}</div>}
        <div className="directory-footnote"><span>LOCAL CATALOG</span><span>{ready ? String(assetCategoryCount).padStart(2, '0') : '--'} ASSET CLASSES / {ready ? entities.length : '--'} RECORDS</span></div>
        <section className={`readiness ${ready ? '' : 'not-ready'}`} id="readiness"><div className="readiness-header"><span>数据与环境准备</span><span className="readiness-state">{prepJob?.state === 'running' || prepJob?.state === 'queued' ? 'PREPARING' : ready ? 'SYSTEM READY' : 'NOT PREPARED'}</span></div>{prepJob && <><div className="readiness-progress"><i style={{ width: `${prepJob.progress * 100}%` }} /></div><p className="help">{prepJob.message}</p>{prepJob.error && <p className="validation-error">{prepJob.error.code} · {prepJob.error.message}</p>}</>}{!ready && <p className="help">{catalog?.availability.reason ?? api.error ?? '角色准备与地图准备分开，请配置角色运行环境。'}</p>}<div className="readiness-grid"><span>BLENDER <b>{source.blender_exe ? '已配置' : '未填写'}</b></span><span>GAME DATA <b>{ready ? '目录已载入' : '待准备'}</b></span><span>CACHE <b>{ready ? '已就绪' : '待准备'}</b></span><span>OUTPUT <b>{source.output_root ? '已设置' : '后端配置'}</b></span></div><button id="configure-environment" data-testid="operator-configure" disabled={busy} onClick={openEnvironment}>{ready ? '检查 / 重新配置运行环境' : '配置环境 / 重试准备'} →</button>{!developmentPreview && <button data-testid="operator-refresh" disabled={busy} onClick={() => void api.refreshCatalog()}>刷新实体目录 ↻</button>}</section>
      </aside>
      <section ref={archive} hidden={directoryExpanded} className={`archive ${selectedReport?.state === 'failed' ? 'failed-stage' : ''}`} id="archive" aria-live="polite"><div className="archive-top"><span id="archive-serial">{selected ? `ARCHIVE // ${entityCode(selected)}` : 'ARCHIVE // STANDBY'}</span><span id="archive-status">{selected ? selected.capabilities.blend_export ? 'ENTRY_AVAILABLE' : 'UNAVAILABLE' : 'AWAITING SELECTION'} <i /></span></div><div className="archive-heading"><div><small id="entity-type">{selected ? ASSET_CATEGORY_LABELS[selected.asset_category] : 'ENTITY DOSSIER'}</small><h1 id="entity-name">{selected ? selected.english_name?.toUpperCase() ?? entityCode(selected) : 'SELECT ENTITY'}</h1><h2 id="entity-cn">{selected ? entityName(selected) : '等待指定实体档案'}</h2></div><span className="archive-number">{selected ? String(entities.findIndex(entity => entity.selection_id === selected.selection_id) + 1).padStart(2, '0') : '—'}</span></div><div className="dossier"><span>// ENDFIELD ENTITY ARCHIVE</span><strong id="dossier-id">{selected ? `ID: ${entityCode(selected)} · VARIANT: ${variant?.variant ?? '—'}` : '从左侧目录选择一个实体'}</strong></div><div className="figure-stage" id="figure-stage"><div className="stage-rail rail-left"><span>ENTITY / DIAGRAM</span><i /><span>NO GEOMETRY DATA</span></div><div className="portrait" id="portrait">{!selected ? <span className="portrait-standby" aria-label="等待选择实体" /> : selected.entry_kind === 'independent_npc' ? <span className="portrait-npc"><svg viewBox="0 0 100 100" fill="none" stroke="currentColor"><path d="M50 8 86 29v42L50 92 14 71V29zM14 29l36 21 36-21M50 50v42M50 8v42" /></svg>[ {selected.asset_category.toUpperCase()} ]</span> : selectedPortrait ? <><img data-testid="operator-portrait" data-character-id={selectedPortrait.characterId} src={selectedPortrait.url} alt={`${entityName(selected)}侧面头像`} width="640" height="720" /><span className="portrait-label">OPERATOR / {selectedPortrait.characterId}</span></> : <span className="portrait-unavailable" data-testid="operator-portrait-unavailable">PORTRAIT / UNASSIGNED<br />暂无角色头像</span>}</div><div className="stage-axis" aria-hidden="true"><span>Z</span><i /><span>X</span><b>Y</b></div><span className="stage-caption">{selected ? selectedReport?.state === 'failed' ? 'ARCHIVE_BUILD_FAILED · 导出失败' : selectedPortrait ? 'OPERATOR PORTRAIT / 角色档案' : ASSET_CATEGORY_LABELS[selected.asset_category] + ' / 独立实体档案' : '请展开目录并指定实体'}</span></div>
        <section className={`budget-display ${meshOnly ? 'bypassed' : ''}`}><div className="readout-title"><span>导出帧预算 <small>// FRAME BUDGET</small></span><b id="budget-value">{Number.isFinite(numericBudget) ? numericBudget : '—'} <small>F</small></b></div><div className="budget-ruler"><div className="budget-fill" style={{ width: `${budgetPosition(numericBudget)}%` }} /><i id="budget-cursor" style={{ left: `${budgetPosition(numericBudget)}%` }} /></div><div className="ruler-labels"><span>2F</span><span>300F</span><span>1200F</span><span>18000F</span></div><p>{meshOnly ? 'BYPASSED: NO ACTION · 无动作导出不校验预算' : '建议初始值 · 预算不代表动作总帧数'}</p></section>
        <div className="pipeline"><span>PIPELINE SPECIFICATION</span><p>{catalog?.profiles[0]?.name ?? '基础骨骼管线'} <i>//</i> 单动作绑定 <i>//</i> Blender .blend</p><small>{selected ? selected.entry_kind === 'independent_npc' ? selected.expression_unavailable_reason : selected.optional_tools_reason ?? '表情与动作选项以所选实体能力为准' : '能力将随实体选择显示'}</small></div>
        {!developmentPreview && api.error && <section className="operator-error" data-testid="operator-error" role="alert"><p>{api.error}</p><button onClick={() => void (api.operationRequest ? api.reconnect() : api.refreshCatalog())}>重连状态</button><button onClick={openEnvironment}>检查环境缓存</button></section>}
        {selectedReport && <section className={`result ${selectedReport.state}`} id="result" data-testid="operator-result"><div className="result-head"><b>{selectedReport.state === 'partial' ? `导出完成，有待处理项 · ${selectedReport.pending.length} 项` : selectedReport.state === 'completed' ? '导出完成' : selectedReport.state === 'failed' ? '导出失败' : '任务已取消'}</b><small>{selectedReport.state.toUpperCase()}{selectedReport.development ? ' / DEV' : ''}</small></div>{selectedReport.output && <span className="result-path">{selectedReport.output}</span>}{selectedReport.error && <p className="error-code">{selectedReport.error}</p>}{selectedReport.development && <p>示例报告 · 不生成真实文件</p>}{!selectedReport.development && selectedReport.output && <p data-testid="operator-result-materials">{selectedReport.materialOptions ? `实际材质：${describeMaterials(selectedReport.materialOptions)}` : '后端未返回材质确认信息'}</p>}<div className="result-actions">{selectedReport.output && <button data-testid="operator-copy-path" onClick={() => copy(selectedReport.output!, '产物目录已复制')}>{selectedReport.development ? '复制示例路径' : '复制产物目录'}</button>}{selectedReport.pending.length > 0 && <button data-testid="operator-copy-diagnostics" onClick={() => copy(selectedReport.pending.map(item => `${item.code}: ${item.message}`).join('\n'), '完整诊断清单已复制')}>复制完整诊断清单</button>}{['completed', 'partial'].includes(selectedReport.state) && <button data-testid="operator-show-completion" onClick={() => setCompletionOpen(true)}>重播扫描</button>}{selectedReport.state === 'failed' && <><button data-testid="operator-retry-export" disabled={Boolean(reason) || busy} onClick={startExport}>重试导出</button><button onClick={openEnvironment}>检查环境缓存</button></>}</div>{selectedReport.files.length > 0 && <><h4>已生成文件 · {selectedReport.files.length} 项</h4><ul data-testid="operator-files">{selectedReport.files.map(file => <li key={file.relative_path}><code>{file.relative_path}</code> · {file.role} · {file.bytes.toLocaleString()} B</li>)}</ul></>}{selectedReport.pending.length > 0 && <><h4>待处理项 · {selectedReport.pending.length} 项（数量不等于缺失资产数量）</h4><ul data-testid="operator-pending">{selectedReport.pending.map((item, index) => <li key={`${index}-${item.code}`}><b>{item.code}</b>：{item.message}</li>)}</ul></>}</section>}
        <div className="archive-bottom"><span>04 / OPERATOR WORKSPACE</span><span>{developmentPreview ? 'DEV VISUAL PREVIEW' : 'LOCAL CHARACTER PIPELINE'}</span></div></section>
      <aside ref={drag.panelRef} className={`settings ${drawer ? 'drawer-visible' : ''} ${disabled ? 'readonly' : ''} ${drag.isDragging ? 'dragging' : ''}`} id="settings" data-testid="operator-settings"><header ref={drag.headerRef} className="settings-header" id="drag-handle" data-testid="operator-drag-handle" tabIndex={0} aria-label="提取设置，方向键移动，Home 复位" onPointerDown={drag.onPointerDown} onPointerMove={drag.onPointerMove} onPointerUp={drag.onPointerUp} onPointerCancel={drag.onPointerCancel} onLostPointerCapture={drag.onLostPointerCapture} onKeyDown={drag.onKeyDown}><div><small>EXPORT CONFIGURATION</small><h2>提取设置</h2></div><div><button id="panel-reset" data-testid="operator-panel-reset" className="text-button" onClick={drag.reset}>复位</button><button id="drawer-close" data-testid="operator-drawer-close" className="icon-button" aria-label="关闭设置" onClick={() => setDrawer(false)}>×</button></div></header><div className="settings-body" id="settings-body">
        <section className="config-block"><BlockTitle number={1} title="模型变体" code="MODEL VARIANT" />{selected?.variants.length ? selected.variants.map(item => <label className="variant-option" key={item.variant_id}><input id={`variant-${item.variant_id}`} data-testid={`operator-variant-${item.variant_id}`} type="radio" name="operator-variant" value={item.variant_id} checked={variantId === item.variant_id} disabled={disabled} onChange={() => changeVariant(item.variant_id)} /><span>{item.label}<small>{item.variant}</small></span><b>{item.geometry_status === 'not_verified' ? 'NOT VERIFIED' : item.geometry_status}</b></label>) : <label className="variant-option"><input type="radio" disabled /><span>选择实体后加载模型变体<small>—</small></span><b>STANDBY</b></label>}{selected?.unavailable_reason && <p className="help">{selected.unavailable_reason}</p>}</section>
        <section className="config-block"><BlockTitle number={2} title="动作绑定" code="SINGLE ACTION BINDING" /><label className="field-label" htmlFor="action-select">选择动作</label><select id="action-select" data-testid="operator-action" value={actionId} disabled={disabled || meshOnly || !actions.length && !cpuReferenceCatalog?.actions.length || api.actionsLoading && !developmentPreview} onChange={event => setActionId(event.currentTarget.value)}>{!actions.length && !cpuReferences.length && <option value="">{api.actionsLoading && !developmentPreview ? '加载动作目录中…' : '无可用动作'}</option>}{actions.map(action => <option key={action.action_id} value={action.action_id} disabled={!selected?.capabilities.animation}>{action.name}</option>)}{Boolean(cpuReferenceCatalog?.actions.length) && <optgroup label="精确 NPC 动作引用">{cpuReferenceCatalog?.actions.map(reference => <option key={reference.reference_id} value={`reference:${reference.reference_id}`} disabled={!reference.request_available || !cpuReferenceCatalog.request_available}>{reference.state_name}{reference.request_available && cpuReferenceCatalog.request_available ? '' : ' · 不可提交'}</option>)}</optgroup>}</select><ActionCatalog active={active} disabled={disabled || meshOnly || api.actionsLoading && !developmentPreview} loading={api.actionsLoading} options={actionOptions} value={actionId} total={(api.animationCatalog?.total ?? actions.length) + (cpuReferenceCatalog?.actions.length ?? 0)} hasMore={!developmentPreview && api.hasMoreActions} onChange={setActionId} onLoadMore={() => void api.loadMoreActions()} />{!developmentPreview && api.hasMoreActions && <button data-testid="operator-more-actions" className="text-button" disabled={api.actionsLoading} onClick={() => void api.loadMoreActions()}>继续读取动作 · 已载入 {actions.length} / {api.animationCatalog?.total}</button>}{!developmentPreview && api.error && (selected?.capabilities.animation || selected?.entry_kind === 'independent_npc' && selected.animation_catalog_available) && <button data-testid="operator-retry-actions" className="text-button" onClick={() => void api.refreshCatalog()}>读取失败 · 重试动作目录</button>}<label className="checkbox-option"><input id="mesh-only" data-testid="operator-mesh-only" type="checkbox" checked={meshOnly} disabled={disabled} onChange={event => setMeshOnly(event.currentTarget.checked)} />不附带动作导出 <small>Mesh &amp; Rig Only</small></label>{selected?.entry_kind === 'independent_npc' && <p className="help" data-testid="operator-animation-capability">动作目录：{selected.animation_catalog_available ? '可读取' : '不可读取'} · action 提交：{selected.capabilities.animation ? '可用' : '不可用'}<br />{selected.animation_scope}{selected.optional_tools_reason && <><br />{selected.optional_tools_reason}</>}</p>}{selectedAction && <p className="help" data-testid="operator-action-binding">{selectedAction.binding_status}<br />目录发现不代表本次动作已通过导出验收。</p>}{selectedReference && cpuReferenceCatalog?.status === 'exact_avatar_base_clip_references' && <p className="help" data-testid="operator-reference-binding">reference · {cpuReferenceCatalog.avatar.binding_status}<br />精确来源引用可提交；本次动作仍待导出验收。</p>}{!developmentPreview && <><p className="help" data-testid="operator-animation-status">{api.animationCatalog?.binding_status}{api.animationCatalog?.unavailable_reason && <><br />{api.animationCatalog.unavailable_reason}</>}{cpuReferenceCatalog && <><br />{cpuReferenceCatalog.status === 'exact_avatar_base_clip_references' ? cpuReferenceCatalog.request_unavailable_reason ?? '当前引用不可提交' : cpuReferenceCatalog.unavailable_reason}</>}</p>{api.animationCatalog?.coverage_counts && <p className="help" data-testid="operator-action-coverage">来源统计：可用引用 {api.animationCatalog.coverage_counts.available_action_count} · 不支持 {api.animationCatalog.coverage_counts.unsupported_action_count}<br />{api.animationCatalog.coverage_counts.scope}</p>}{api.animationCatalog?.unsupported_actions?.map((item, index) => <p className="help" data-testid="operator-unsupported-action" key={`${item.state_name}-${index}`}>{item.clip_name} · {item.state_name}<br />{item.binding_status}<br />{item.reason}</p>)}{api.animationCatalog?.limitations.map(item => <p className="help" key={item}>{item}</p>)}{cpuReferenceCatalog?.status === 'exact_avatar_base_clip_references' && cpuReferenceCatalog.limitations.map(item => <p className="help" key={item}>{item}</p>)}</>}</section>
        <section className="config-block"><BlockTitle number={3} title="导出帧预算" code="FRAME BUDGET" /><div className={`budget-input ${validBudget ? '' : 'invalid'}`} id="budget-input-shell"><input id="budget-input" data-testid="operator-budget" type="number" inputMode="numeric" min="2" max="18000" step="1" value={budget} disabled={disabled || meshOnly} aria-label="本次导出帧预算" onChange={event => setBudget(event.currentTarget.value)} /><span>帧</span><button data-testid="operator-budget-decrease" disabled={disabled || meshOnly} aria-label="减少帧预算" onClick={() => setBudget(String(Math.max(2, Number(budget) - 1)))}>−</button><button data-testid="operator-budget-increase" disabled={disabled || meshOnly} aria-label="增加帧预算" onClick={() => setBudget(String(Math.min(18000, Number(budget) + 1)))}>+</button></div>{!validBudget && <p className="validation-error">输入非法：需为 2 ~ 18000 范围内的整数。</p>}<p className="help">合法范围 2 ~ 18000 帧，300 为建议初始值。<br />动作目录不提供总帧数；超出预算将报错，不截断动作。</p></section>
        <section className="config-block"><BlockTitle number={4} title="导出能力" code="EXPRESSION CAPABILITIES" /><label className="checkbox-option"><input id="mmd-expression" data-testid="operator-mmd" type="checkbox" checked={mmd && Boolean(selected?.capabilities.basic_mmd)} disabled={disabled || !selected?.capabilities.basic_mmd} onChange={event => setMmd(event.currentTarget.checked)} />基础 MMD 表情驱动兼容</label><label className="checkbox-option"><input id="shape-keys" data-testid="operator-raw-expressions" type="checkbox" checked={raw && Boolean(selected?.capabilities.raw_expressions)} disabled={disabled || !selected?.capabilities.raw_expressions} onChange={event => setRaw(event.currentTarget.checked)} />原始表情形态键 <small>ShapeKeys</small></label>{selected && !selected.capabilities.raw_expressions && <p className="help">{selected.entry_kind === 'independent_npc' ? selected.expression_unavailable_reason : selected.optional_tools_reason ?? '当前实体无可用表情模板'}</p>}</section>
        <section className="config-block"><BlockTitle number={5} title="材质与天气" code="MATERIAL & WEATHER" /><MaterialSettings value={materials} disabled={disabled} restriction={materialRestriction} onModeChange={setMaterialMode} onSimplifiedChange={setSimplifiedMaterials} /></section>
        <section className="config-block"><BlockTitle number={6} title="输出配置" code="OUTPUT CONFIGURATION" /><label className="field-label" htmlFor="output-label">输出标签</label><input id="output-label" data-testid="operator-output-label" type="text" value={label} maxLength={80} disabled={disabled} onChange={event => setLabel(event.currentTarget.value)} /><span className="format-pill">Blender (.blend 资产包)</span><p className="help">{developmentPreview ? '开发视觉预览 · 不生成真实资产文件' : '产物由本机角色运行配置指定的输出目录生成'}</p></section>
      </div><div className="action-dock"><p id="export-reason">{busy ? api.cancelRequested ? '正在终止任务，等待后端终态' : `${api.job?.phase ?? '连接任务'} · ${api.job?.message ?? '后台状态待确认'}` : reason || (developmentPreview ? '开发预览就绪 · 预览结果不执行提取' : '配置就绪 · 提交后由后端验证')}</p><button id="export-button" data-testid="operator-export" className={`export-button ${busy ? 'running' : ''}`} disabled={Boolean(reason) || busy} onClick={startExport}><span>{busy ? `正在导出资产 · ${progress}%` : developmentPreview ? '预览角色 .blend 构建结果' : '构建角色 .blend 资产包'}</span><b>{busy ? '…' : '→'}</b></button>{developmentPreview && <div className="demo-result-options"><span>开发样例结果</span><select data-testid="operator-preview-outcome" value={previewOutcome} aria-label="开发预览结果" onChange={event => setPreviewOutcome(event.currentTarget.value as OperatorReport['state'])}><option value="completed">导出完成</option><option value="partial">导出完成，有待处理项</option><option value="failed">导出失败</option><option value="cancelled">任务已取消</option></select></div>}{busy && <button id="cancel-task" data-testid="operator-cancel" className="text-button" disabled={!api.canCancel || api.cancelRequested} onClick={() => void api.cancel()}>{api.cancelRequested ? '正在终止…' : '取消任务'}</button>}</div></aside>
    </div>
    <div className="mobile-action"><button id="mobile-export" data-testid="operator-mobile-export" className="export-button" disabled={Boolean(reason) || busy} onClick={() => setDrawer(true)}>{busy ? `后台任务 ${progress}% · 查看设置` : reason || '提取设置 / 构建角色资产包 →'}</button></div>
    <dialog ref={environment} id="environment-dialog" data-testid="operator-environment"><form id="environment-form" onSubmit={event => { event.preventDefault(); prepare() }}><div className="dialog-title"><div><small>READINESS / {developmentPreview ? 'DEV PREVIEW' : 'CHARACTER DATA'}</small><h2>数据与环境准备</h2></div><button type="button" className="icon-button" data-testid="operator-environment-close" aria-label="关闭环境配置" onClick={() => environment.current?.close()}>×</button></div><p>{developmentPreview ? '开发样例只改变预览状态，不读取磁盘或调用提取后端。' : '角色与地图分别准备。选择只读游戏来源、Blender 4.4 或更新版本、角色缓存和角色输出目录。'}</p>{PATHS.map(path => <label key={path.key}>{path.label}<div className="operator-path-input"><input id={`operator-${path.key}`} data-testid={`operator-path-${path.key}`} required={!developmentPreview} value={source[path.key]} onChange={event => setSource({ ...source, [path.key]: event.currentTarget.value })} /><button type="button" data-testid={`operator-path-picker-${path.key}`} disabled={Boolean(pathBusy) || Boolean(developmentPreview)} onClick={() => void choosePath(path)} aria-label={`浏览 ${path.label}`}>…</button></div></label>)}<div className="dialog-actions">{developmentPreview && <button type="button" data-testid="operator-preview-unprepared" className="text-button" onClick={() => { setPreviewReady(false); environment.current?.close() }}>演示未准备</button>}<button type="submit" data-testid="operator-prepare" className="secondary-button" disabled={busy}>执行准备与自检 →</button></div></form></dialog>
    {selectedReport && <OperatorCompletion report={selectedReport} open={completionOpen} reduced={animationReduced} onClose={() => setCompletionOpen(false)} onCopy={copy} />}
  </div>
}





