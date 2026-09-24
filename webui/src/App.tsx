import {
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  ViewTransition,
} from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import {
  Box,
  Check,
  ChevronDown,
  CircleDotDashed,
  Clipboard,
  Database,
  Folder,
  HardDrive,
  Layers3,
  Play,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Square,
  SquareDashedMousePointer,
  TriangleAlert,
  Waves,
  X,
} from 'lucide-react'
import { EXPORT_GROUPS, LAYER_LABELS, MAPS } from './data/maps'
import { ExtractionCompleteOverlay } from './components/ExtractionCompleteOverlay'
import { MapViewport } from './components/MapViewport'
import { REGION_MAP_RUNTIME_MANIFEST } from './data/regionMapRuntime'
import { resolveSelection } from './lib/mapChunking'
import type { ChunkMode, ChunkSelection } from './lib/mapChunking'
import type {
  FirstRunError,
  FirstRunEvent,
  FirstRunRequest,
  FirstRunStatus,
  FirstRunValidation,
  BlenderBuildResult,
  ExportMode,
  LayerId,
  LayerState,
  MapId,
} from './types'

const DEFAULT_LAYERS: LayerState = {
  terrain: true,
  instances: true,
  water: true,
  effects: true,
}

const configuredPath = (name: string, fallback: string) => {
  const value = import.meta.env[name]?.trim()
  return value || fallback
}

const DEFAULT_SOURCE_PATHS = {
  game: configuredPath('VITE_ENDFIELD_GAME_ROOT', ''),
  output: configuredPath('VITE_ENDFIELD_WEBUI_OUTPUT_ROOT', ''),
  cache: configuredPath('VITE_ENDFIELD_CACHE_ROOT', ''),
  blender: configuredPath('VITE_ENDFIELD_BLENDER', ''),
}

const DEFAULT_BLENDER_BY_MAP: Record<MapId, string> = {
  map01: DEFAULT_SOURCE_PATHS.blender,
  map02: DEFAULT_SOURCE_PATHS.blender,
}

type PathKind = keyof typeof DEFAULT_SOURCE_PATHS
const SOURCE_PATH_STORAGE_KEY = 'endfield-atlas-source-paths-v1'

const loadSourcePaths = () => {
  if (typeof window === 'undefined') return DEFAULT_SOURCE_PATHS
  try {
    const stored = JSON.parse(window.localStorage.getItem(SOURCE_PATH_STORAGE_KEY) ?? '{}') as Partial<Record<PathKind, unknown>>
    return {
      game: typeof stored.game === 'string' ? stored.game : DEFAULT_SOURCE_PATHS.game,
      output: typeof stored.output === 'string' ? stored.output : DEFAULT_SOURCE_PATHS.output,
      cache: typeof stored.cache === 'string' ? stored.cache : DEFAULT_SOURCE_PATHS.cache,
      blender: typeof stored.blender === 'string' ? stored.blender : DEFAULT_SOURCE_PATHS.blender,
    }
  } catch {
    return DEFAULT_SOURCE_PATHS
  }
}

type PathPickerResponse = {
  format: 'EndfieldPathPickerResponse/1'
  status: 'selected' | 'cancelled' | 'unavailable'
  kind?: PathKind
  path?: string
  valid?: boolean
  writable?: boolean
  writableParent?: boolean
  error?: string
}

type FirstRunApiError = {
  format?: 'EndfieldWebUIFirstRunError/1'
  error?: FirstRunError
  firstRun?: FirstRunStatus
}

const FIRST_RUN_PHASE_LABELS: Record<string, string> = {
  idle: '等待部署',
  dependencies: '安装运行依赖',
  preflight: '只读边界预检',
  helper_dependencies: '同步固定版本工具',
  bootstrap_metadata: '生成基础元数据',
  resource_index: '建立资源索引',
  bundle_scan_index: '扫描资源容器',
  scene_discovery: '发现地图场景',
  scene_candidate_bundles: '收束候选资源',
  asset_bootstrap: '构建自动资产层',
  map_layers: '生成地图图层',
  map_overviews: '生成地图底图',
  audit: '执行完整审计',
  complete: '数据准备完成',
  partial: '保留部分结果',
  failed: '准备失败',
  cancelled: '已安全取消',
}

const FIRST_RUN_ERROR_LABELS: Record<string, string> = {
  invalid_request: '目录或请求格式无效',
  first_run_paths_incomplete: '目录校验尚未通过',
  already_running: '数据准备任务正在运行',
  first_run_required: '需要先完成首次数据准备',
  already_ready: '相同数据准备已经完成',
  dependency_install_failed: '运行依赖安装失败',
  cancelled: '数据准备已取消',
  backend_failure: '数据准备后端失败',
  internal_error: '本机服务发生内部错误',
  invalid_path_kind: '不支持的路径类型',
}

const formatBytes = (bytes: number | null) => {
  if (bytes === null) return 'UNKNOWN'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes / 1024
  let unit = units[0]
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024
    unit = units[index]
  }
  return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${unit}`
}

const firstRunErrorMessage = (payload: FirstRunApiError, fallback: string) => {
  const error = payload.error
  if (!error) return fallback
  const label = FIRST_RUN_ERROR_LABELS[error.code]
  return label ? `${label}：${error.message}` : error.message
}

const JOB_PHASES = [
  ['PREPARE', '准备只读索引', 0],
  ['CHUNKS', '生成区块清单', 18],
  ['EXTRACT', '导出分层数据', 32],
  ['BUILD', '构建场景与数据包', 48],
  ['AUDIT', '校验输出文件', 91],
] as const

const RECONSTRUCTABLE_BLEND_GROUPS = ['building', 'prop', 'vegetation', 'terrain', 'road', 'unknown']
const EXPORT_GROUP_NOTES: Record<string, string> = {
  lighting: '仅数据记录',
  particle: '仅数据记录',
  water: '自动绑定待接入',
  effects: '场景重建待接入',
}

type LaunchState = 'idle' | 'running' | 'completed' | 'cancelled' | 'failed'

type ExportJob = {
  job_id: string
  state: 'queued' | 'preparing' | 'extracting' | 'building' | 'auditing' | 'completed' | 'failed' | 'cancelled'
  phase: string
  progress: number
  result?: {
    package?: { output_dir?: string; status?: string; exportMode?: ExportMode; warnings?: string[] }
    blenderBuild?: BlenderBuildResult
  } | null
  error?: { message?: string; type?: string } | null
}

const CHUNK_MODE_LABELS: Record<ChunkMode, string> = {
  per_sector: '每个 128 × 128 sector 独立导出',
  cluster_4x4_sectors: '按全局坐标稳定分组为 4 × 4 sectors',
  merged_selection: '合并选区输出，并保留内部 sector 清单',
}

function SettingSection({
  title,
  code,
  children,
  defaultOpen = true,
}: {
  title: string
  code: string
  children: ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <details
      className="setting-section"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <strong>{title}</strong>
        <small>{code}</small>
        <ChevronDown size={13} aria-hidden="true" />
      </summary>
      <div className="setting-section-body">{children}</div>
    </details>
  )
}

function PathField({
  id,
  label,
  value,
  caption,
  icon,
  onChoose,
  busy = false,
}: {
  id: string
  label: string
  value: string
  caption: string
  icon: ReactNode
  onChoose: () => void
  busy?: boolean
}) {
  return (
    <div className="path-field">
      <label htmlFor={id}>{label}</label>
      <div className="path-input">
        <input id={id} value={value} readOnly />
        <button type="button" onClick={onChoose} aria-label={`选择${label}`} disabled={busy}>
          {busy ? <RefreshCw className="path-picker-spin" size={14} /> : icon}
        </button>
      </div>
      <small>{caption}</small>
    </div>
  )
}

function LegalArchive() {
  const reduceMotion = useReducedMotion()
  const [legalOpen, setLegalOpen] = useState(false)

  const toggleLegal = () => {
    startTransition(() => setLegalOpen((value) => !value))
  }

  return (
    <motion.section
      className={`legal-archive ${legalOpen ? 'is-open' : ''}`}
      aria-labelledby="legal-archive-title"
      initial={reduceMotion ? false : { opacity: 0.14, y: 42, filter: 'blur(3px)' }}
      whileInView={reduceMotion ? undefined : { opacity: 1, y: 0, filter: 'blur(0px)' }}
      viewport={{ once: false, amount: 0.16 }}
      transition={{ duration: 0.72, ease: [0.16, 1, 0.3, 1] }}
    >
      <motion.div
        className="legal-archive-grid"
        initial={reduceMotion ? false : 'hidden'}
        whileInView={reduceMotion ? undefined : 'visible'}
        viewport={{ once: false, amount: 0.22 }}
        variants={{
          hidden: { opacity: 0.45 },
          visible: { opacity: 1, transition: { staggerChildren: 0.1, delayChildren: 0.08 } },
        }}
      >
        <motion.div
          className="legal-archive-intro"
          variants={{
            hidden: { opacity: 0, x: -24 },
            visible: { opacity: 1, x: 0, transition: { duration: 0.54, ease: [0.16, 1, 0.3, 1] } },
          }}
        >
          <span className="legal-kicker">LEGAL / RESEARCH BOUNDARY</span>
          <h2 id="legal-archive-title">研究工具，非官方服务</h2>
          <p>
            本项目由《明日方舟：终末地》爱好者独立制作，用于个人技术研究、资料整理与二次创作辅助。
            它不代表游戏官方立场，也不构成任何授权、合作或商业背书。
          </p>
          <div className="legal-stamp">FAN-MADE / NON-COMMERCIAL / LOCAL-FIRST</div>
        </motion.div>

        <div className="legal-ledger" aria-label="主要使用边界">
          <motion.div
            className="legal-ledger-row"
            variants={{
              hidden: { opacity: 0, x: 28 },
              visible: { opacity: 1, x: 0, transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] } },
            }}
          >
            <span>01</span>
            <div>
              <strong>权利归属</strong>
              <p>游戏名称、商标、角色、图像、模型、动画、音频、文本及其他游戏资产归其各自权利人所有。</p>
            </div>
          </motion.div>
          <motion.div
            className="legal-ledger-row"
            variants={{
              hidden: { opacity: 0, x: 28 },
              visible: { opacity: 1, x: 0, transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] } },
            }}
          >
            <span>02</span>
            <div>
              <strong>使用边界</strong>
              <p>用户应仅处理合法取得并有权访问的本地文件，并自行确认导出、修改、发布和二创行为符合适用规则。</p>
            </div>
          </motion.div>
          <motion.div
            className="legal-ledger-row"
            variants={{
              hidden: { opacity: 0, x: 28 },
              visible: { opacity: 1, x: 0, transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] } },
            }}
          >
            <span>03</span>
            <div>
              <strong>结果责任</strong>
              <p>工具按现状提供，不保证导出结果完整或适用于特定用途。执行前请备份，并检查输出内容与路径。</p>
            </div>
          </motion.div>
        </div>
      </motion.div>

      <div className={`legal-details ${legalOpen ? 'is-open' : ''}`}>
        <button
          className="legal-details-toggle"
          type="button"
          aria-expanded={legalOpen}
          aria-controls="legal-details-content"
          onClick={toggleLegal}
        >
          <span>查看完整使用边界与责任说明</span>
          <small>OPEN / READ BEFORE DISTRIBUTION</small>
          <ChevronDown size={16} aria-hidden="true" />
        </button>
        {legalOpen && (
          <ViewTransition enter="slide-up" exit="slide-down" default="none">
            <div id="legal-details-content" className="legal-details-clip">
              <motion.div
                className="legal-details-body"
                initial={reduceMotion ? false : { opacity: 0, y: -10 }}
                animate={reduceMotion ? undefined : { opacity: 1, y: 0 }}
                transition={{ duration: 0.32, delay: 0.04, ease: [0.16, 1, 0.3, 1] }}
              >
                <div>
                  <h3>项目与第三方权利</h3>
                  <p>
                    本工具与上海鹰角网络科技有限公司、GRYPHLINE 及其关联公司不存在隶属、合作、授权或背书关系。
                    本工具不授予用户复制、传播、商业使用、再许可或重新发布游戏资产的权利。
                  </p>
                  <p>
                    除非另有说明，项目原创代码、界面文案与项目文档按照仓库中列明的开源许可证提供。
                    该许可不覆盖游戏派生内容或其他第三方素材。
                  </p>
                </div>
                <div>
                  <h3>用户责任与风险</h3>
                  <p>
                    用户应自行承担因不当路径、存储空间不足、系统故障、第三方软件、错误操作或不当传播造成的风险，
                    并应在操作前保留必要备份。请勿使用本工具绕过访问控制、传播完整游戏资源或侵犯他人知识产权。
                  </p>
                  <p>
                    本说明不排除或限制法律规定不得排除或限制的责任。若导出结果包含第三方受保护内容，
                    用户在发布、分享或用于衍生作品前，应自行确认所需授权与使用范围。
                  </p>
                </div>
              </motion.div>
            </div>
          </ViewTransition>
        )}
      </div>

      <ViewTransition update="auto">
        <div className="legal-archive-footnote">
          <span>原创内容授权参考</span>
          <a href="https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans" target="_blank" rel="noreferrer">
            知识共享署名-非商业性使用-相同方式共享 4.0 国际许可
          </a>
          <span>仅适用于项目自身拥有权利的内容</span>
        </div>
      </ViewTransition>
    </motion.section>
  )
}

function MapTransitionVeil({ mapId, direction, overviewSource }: { mapId: MapId; direction: 'forward' | 'back'; overviewSource: 'loading' | 'dev-override' | 'runtime-config' | 'fallback' }) {
  const ready = overviewSource !== 'loading'
  const reduceMotion = useReducedMotion()

  return (
    <AnimatePresence initial={false} mode="popLayout">
      <motion.div
        key={mapId}
        className={`scene-rebuild scene-rebuild-${mapId} scene-rebuild-${direction} ${ready ? 'is-ready' : 'is-syncing'}`}
        initial={{ opacity: 1 }}
        animate={{ opacity: ready ? 0 : 1 }}
        exit={{ opacity: 0 }}
        transition={{
          duration: ready ? (reduceMotion ? 0.01 : 0.86) : 0,
          delay: ready && !reduceMotion ? 0.22 : 0,
          ease: [0.4, 0, 0.2, 1],
        }}
        aria-hidden="true"
      >
        <div className="rebuild-slide-plane" />
        <div className="wuling-color-bars">
          <i className="wuling-bar bar-ink" />
          <i className="wuling-bar bar-mint" />
          <i className="wuling-bar bar-mineral" />
          <i className="wuling-bar bar-warm" />
        </div>
        <div className="rebuild-plane" />
        <div className="rebuild-trace trace-a" />
        <div className="rebuild-trace trace-b" />
        <div className="rebuild-blocks">
          {Array.from({ length: 18 }, (_, index) => (
            <i key={index} style={{ '--block-index': index } as CSSProperties} />
          ))}
        </div>
      </motion.div>
    </AnimatePresence>
  )
}

export default function App() {
  const reduceMotion = useReducedMotion()
  const [mapId, setMapId] = useState<MapId>('map01')
  const [layers, setLayers] = useState<LayerState>(DEFAULT_LAYERS)
  const [selection, setSelection] = useState<ChunkSelection | null>(null)
  const [sourcePaths, setSourcePaths] = useState(loadSourcePaths)
  const [choosingPath, setChoosingPath] = useState<PathKind | null>(null)
  const [firstRunStatus, setFirstRunStatus] = useState<FirstRunStatus | null>(null)
  const [firstRunValidation, setFirstRunValidation] = useState<FirstRunValidation | null>(null)
  const [firstRunEvents, setFirstRunEvents] = useState<FirstRunEvent[]>([])
  const [firstRunAction, setFirstRunAction] = useState<'status' | 'validate' | 'start' | 'resume' | 'cancel' | null>('status')
  const [firstRunError, setFirstRunError] = useState('')
  const [exportMode, setExportMode] = useState<ExportMode>('blend')
  const [allGroupsSelected, setAllGroupsSelected] = useState(true)
  const [selectedGroups, setSelectedGroups] = useState<string[]>([])
  const [effectsMode, setEffectsMode] = useState('full_system_by_anchor')
  const [waterMode, setWaterMode] = useState('stable_eevee')
  const [chunkMode, setChunkMode] = useState<ChunkMode>('per_sector')
  const [outputLabel, setOutputLabel] = useState('map01_region_visual')
  const [drawerCollapsed, setDrawerCollapsed] = useState(false)
  const [launchState, setLaunchState] = useState<LaunchState>('idle')
  const [completionVisible, setCompletionVisible] = useState(false)
  const [completedExportMode, setCompletedExportMode] = useState<ExportMode>('blend')
  const [completedBlenderBuild, setCompletedBlenderBuild] = useState<BlenderBuildResult | null>(null)
  const [progress, setProgress] = useState(0)
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [jobMessage, setJobMessage] = useState('')
  const [jobError, setJobError] = useState('')
  const [armReveal, setArmReveal] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  const [overviewSource, setOverviewSource] = useState<'loading' | 'dev-override' | 'runtime-config' | 'fallback'>('loading')
  const [transitionDirection, setTransitionDirection] = useState<'forward' | 'back'>('forward')
  const jobTimer = useRef<number | null>(null)
  const toastTimer = useRef<number | null>(null)
  const firstRunPollTimer = useRef<number | null>(null)
  const firstRunEventCursor = useRef(0)
  const firstRunHydrated = useRef(false)
  const previousReady = useRef(false)
  const map = MAPS[mapId]
  const preparedRequest = firstRunStatus?.request
  const preparedPathsMatch = Boolean(
    preparedRequest &&
      preparedRequest.game_root === sourcePaths.game &&
      preparedRequest.export_root === sourcePaths.output &&
      preparedRequest.cache_root === sourcePaths.cache,
  )
  const sourceReady = Boolean(firstRunStatus?.ready)
  const firstRunRunning = firstRunStatus?.state === 'queued' || firstRunStatus?.state === 'running'
  const overviewRevision = [
    String(firstRunStatus?.result?.runtime_config ?? ''),
    firstRunStatus?.ready ? 'ready' : 'pending',
    firstRunStatus?.request_identity?.sha256 ?? '',
  ].join(':')
  const overviewLabel = overviewSource === 'dev-override'
    ? 'DEV LOCAL OVERVIEW / SECTOR128'
    : overviewSource === 'runtime-config'
      ? 'ACTIVE RUNTIME / OVERVIEW READY'
      : overviewSource === 'loading'
        ? 'RUNTIME OVERVIEW / CONNECTING'
        : 'SCHEMATIC / NO GAME ASSETS'

  const handleOverviewSourceChange = useCallback((source: 'loading' | 'dev-override' | 'runtime-config' | 'fallback') => {
    setOverviewSource(source)
  }, [])

  const notify = useCallback((message: string) => {
    setToast(message)
    if (toastTimer.current) window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 2100)
  }, [])

  const hydrateFirstRunPaths = useCallback((status: FirstRunStatus) => {
    if (firstRunHydrated.current) return
    firstRunHydrated.current = true
    const paths = status.request ?? status.defaults
    setSourcePaths((current) => ({
      ...current,
      game: current.game || paths.game_root || '',
      output: current.output || paths.export_root || '',
      cache: current.cache || paths.cache_root || '',
      blender: current.blender || paths.blender_exe || '',
    }))
  }, [])

  const loadFirstRunStatus = useCallback(async (silent = false) => {
    if (!silent) setFirstRunAction('status')
    try {
      const response = await fetch('/api/v2/first-run', { cache: 'no-store' })
      const payload = await response.json() as FirstRunStatus | FirstRunApiError
      if (!response.ok || !('state' in payload)) {
        throw new Error(firstRunErrorMessage(payload as FirstRunApiError, '无法读取首次运行状态'))
      }
      setFirstRunStatus(payload)
      hydrateFirstRunPaths(payload)
      if (payload.error?.message) setFirstRunError(payload.error.message)
      else if (payload.state !== 'failed') setFirstRunError('')
      return payload
    } catch (error) {
      if (!silent) setFirstRunError(error instanceof Error ? error.message : String(error))
      return null
    } finally {
      if (!silent) setFirstRunAction(null)
    }
  }, [hydrateFirstRunPaths])

  const loadFirstRunEvents = useCallback(async () => {
    const response = await fetch(`/api/v2/first-run/events?after=${firstRunEventCursor.current}&limit=100`, { cache: 'no-store' })
    if (!response.ok) return
    const payload = await response.json() as { events?: FirstRunEvent[]; next_after?: number }
    const events = payload.events ?? []
    if (events.length) {
      setFirstRunEvents((current) => [...current, ...events].slice(-24))
    }
    firstRunEventCursor.current = payload.next_after ?? events.at(-1)?.sequence ?? firstRunEventCursor.current
  }, [])

  const stopJob = useCallback(() => {
    if (jobTimer.current) {
      window.clearTimeout(jobTimer.current)
      jobTimer.current = null
    }
  }, [])

  const dismissCompletion = useCallback(() => {
    setCompletionVisible(false)
  }, [])

  const invalidateExportAttempt = useCallback(() => {
    if (launchState === 'running') return
    setLaunchState('idle')
    setProgress(0)
    setJobError('')
    setJobMessage('')
    setCompletedBlenderBuild(null)
  }, [launchState])

  useEffect(
    () => () => {
      if (toastTimer.current) window.clearTimeout(toastTimer.current)
      if (firstRunPollTimer.current) window.clearTimeout(firstRunPollTimer.current)
      stopJob()
    },
    [stopJob],
  )

  useEffect(() => {
    window.localStorage.setItem(SOURCE_PATH_STORAGE_KEY, JSON.stringify(sourcePaths))
  }, [sourcePaths])

  useEffect(() => {
    void loadFirstRunStatus()
  }, [loadFirstRunStatus])

  useEffect(() => {
    if (!firstRunRunning) return
    let disposed = false
    const poll = async () => {
      await Promise.all([loadFirstRunStatus(true), loadFirstRunEvents()])
      if (!disposed) firstRunPollTimer.current = window.setTimeout(poll, 850)
    }
    void poll()
    return () => {
      disposed = true
      if (firstRunPollTimer.current) window.clearTimeout(firstRunPollTimer.current)
      firstRunPollTimer.current = null
    }
  }, [firstRunRunning, loadFirstRunEvents, loadFirstRunStatus])

  const handleSelectionChange = useCallback((next: ChunkSelection | null) => {
    if (launchState === 'running') return
    setSelection(next)
    setLaunchState('idle')
    setProgress(0)
  }, [launchState])

  const handleMapChange = (nextMap: MapId) => {
    if (nextMap === mapId) return
    if (launchState === 'running') {
      notify('任务运行中不能切换地图；请先请求安全取消')
      return
    }
    stopJob()
    setTransitionDirection(nextMap === 'map02' ? 'forward' : 'back')
    startTransition(() => {
      setOverviewSource('loading')
      setMapId(nextMap)
      setSourcePaths((current) => ({
        ...current,
        blender: current.blender === DEFAULT_BLENDER_BY_MAP[mapId]
          ? DEFAULT_BLENDER_BY_MAP[nextMap]
          : current.blender,
      }))
      setSelection(null)
      setLaunchState('idle')
      setProgress(0)
      setOutputLabel(nextMap === 'map01' ? 'map01_region_visual' : 'map02_region_visual')
    })
  }

  const toggleLayer = (layer: LayerId) => {
    setLayers((current) => ({ ...current, [layer]: !current[layer] }))
  }

  const toggleGroup = (group: string) => {
    if (launchState === 'running') return
    if (allGroupsSelected) {
      setAllGroupsSelected(false)
      setSelectedGroups([group])
    } else {
      setSelectedGroups((current) =>
        current.includes(group)
          ? current.filter((item) => item !== group)
          : [...current, group],
      )
    }
    invalidateExportAttempt()
  }

  const selectAllGroups = () => {
    if (launchState === 'running') return
    setAllGroupsSelected(true)
    setSelectedGroups([])
    invalidateExportAttempt()
  }

  const updateExportMode = (nextMode: ExportMode) => {
    if (launchState === 'running') return
    setExportMode(nextMode)
    invalidateExportAttempt()
  }

  const choosePath = async (kind: PathKind) => {
    if (choosingPath) return
    setChoosingPath(kind)
    try {
      const response = await fetch('/api/v2/path-picker', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, current: sourcePaths[kind] }),
      })
      const payload = await response.json() as PathPickerResponse
      if (!response.ok) throw new Error(payload.error ?? 'path_picker_failed')
      if (payload.status === 'cancelled') return
      if (payload.status !== 'selected' || !payload.path) throw new Error(payload.error ?? 'path_picker_unavailable')
      setSourcePaths((current) => ({ ...current, [kind]: payload.path as string }))
      invalidateExportAttempt()
      if (kind !== 'blender') {
        setFirstRunValidation(null)
        setFirstRunError('')
      }
      notify(payload.valid === false ? '路径已选择，但当前校验未通过' : '路径已更新')
    } catch (error) {
      const message = error instanceof Error && error.message === 'path_picker_unavailable'
        ? '请通过 launch_webui.py 启动后再选择路径'
        : '无法打开本机路径选择器'
      notify(message)
    } finally {
      setChoosingPath(null)
    }
  }

  const firstRunRequest = useMemo<FirstRunRequest>(() => ({
    format: 'EndfieldWebUIFirstRunRequest/1',
    game_root: sourcePaths.game.trim(),
    export_root: sourcePaths.output.trim(),
    ...(sourcePaths.cache.trim() ? { cache_root: sourcePaths.cache.trim() } : {}),
    ...(sourcePaths.blender.trim() ? { blender_exe: sourcePaths.blender.trim() } : {}),
    run_name: firstRunStatus?.request?.run_name || firstRunStatus?.defaults?.run_name || 'bootstrap_v1',
  }), [firstRunStatus?.defaults?.run_name, firstRunStatus?.request?.run_name, sourcePaths.blender, sourcePaths.cache, sourcePaths.game, sourcePaths.output])

  const validateFirstRun = async () => {
    if (firstRunAction || firstRunRunning) return
    setFirstRunAction('validate')
    setFirstRunError('')
    setFirstRunValidation(null)
    try {
      const response = await fetch('/api/v2/first-run/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(firstRunRequest),
      })
      const payload = await response.json() as FirstRunValidation | FirstRunApiError
      if (!response.ok || !('canonical' in payload)) {
        throw new Error(firstRunErrorMessage(payload as FirstRunApiError, '首次运行校验失败'))
      }
      setFirstRunValidation(payload)
      setSourcePaths((current) => ({ ...current, cache: payload.canonical.cache_root }))
      if (payload.status !== 'valid') {
        const message = `目录校验未通过：${payload.missing.join(' / ')}`
        setFirstRunError(message)
        notify(message)
        return
      }
      notify(payload.space_warning ? '目录校验通过，但磁盘空间低于保守基线' : '目录与运行前提校验完成')
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setFirstRunError(message)
      notify(message)
    } finally {
      setFirstRunAction(null)
    }
  }

  const submitFirstRun = async (resume: boolean) => {
    if (firstRunAction || firstRunRunning) return
    setFirstRunAction(resume ? 'resume' : 'start')
    setFirstRunError('')
    if (!resume) {
      setFirstRunEvents([])
      firstRunEventCursor.current = 0
    }
    try {
      const response = await fetch('/api/v2/first-run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(firstRunRequest),
      })
      const payload = await response.json() as FirstRunStatus | FirstRunApiError
      if (!response.ok || !('state' in payload)) {
        const errorPayload = payload as FirstRunApiError
        if (errorPayload.firstRun) setFirstRunStatus(errorPayload.firstRun)
        throw new Error(firstRunErrorMessage(errorPayload, '无法启动首次数据准备'))
      }
      setFirstRunStatus(payload)
      if (payload.disposition === 'already_ready' || payload.ready) {
        notify('相同数据请求已经准备完成，地图导出已解锁')
      } else {
        notify(resume ? '继续准备请求已提交；将复用已保留的阶段结果' : '首次数据准备已排队；游戏目录始终保持只读')
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setFirstRunError(message)
      notify(message)
    } finally {
      setFirstRunAction(null)
    }
  }

  const cancelFirstRun = async () => {
    if (firstRunAction || !firstRunStatus?.actions.can_cancel) return
    setFirstRunAction('cancel')
    setFirstRunError('')
    try {
      const response = await fetch('/api/v2/first-run/cancel', { method: 'POST' })
      const payload = await response.json() as FirstRunStatus | FirstRunApiError
      if (!response.ok || !('state' in payload)) {
        throw new Error(firstRunErrorMessage(payload as FirstRunApiError, '无法发送取消请求'))
      }
      setFirstRunStatus(payload)
      notify('取消请求已提交；正在终止子进程树，已生成的部分数据会保留')
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setFirstRunError(message)
      notify(message)
    } finally {
      setFirstRunAction(null)
    }
  }

  const restoreFirstRunRequestPaths = () => {
    if (!firstRunStatus?.request) return
    setSourcePaths((current) => ({
      ...current,
      game: firstRunStatus.request?.game_root ?? current.game,
      output: firstRunStatus.request?.export_root ?? current.output,
      cache: firstRunStatus.request?.cache_root ?? current.cache,
      blender: firstRunStatus.request?.blender_exe ?? '',
    }))
    setFirstRunValidation(null)
    setFirstRunError('')
    notify('已载入可恢复任务的原始目录，请确认后继续准备')
  }

  const resetPrototype = () => {
    if (launchState === 'running') {
      notify('任务运行中不能复位；请先请求安全取消')
      return
    }
    stopJob()
    startTransition(() => {
      setMapId('map01')
      setLayers(DEFAULT_LAYERS)
      setSelection(null)
      setAllGroupsSelected(true)
      setSelectedGroups([])
      setExportMode('blend')
      setEffectsMode('full_system_by_anchor')
      setWaterMode('stable_eevee')
      setChunkMode('per_sector')
      setOutputLabel('map01_region_visual')
      setLaunchState('idle')
      setProgress(0)
    })
    notify('WebUI 2.0 视觉状态已复位')
  }

  const readyReason = useMemo(() => {
    if (firstRunAction === 'status') return '正在连接本机首次运行服务'
    if (!firstRunStatus) return '无法确认首次运行状态'
    if (firstRunRunning) return `正在准备数据 · ${FIRST_RUN_PHASE_LABELS[firstRunStatus.phase] ?? firstRunStatus.phase}`
    if (!sourceReady) {
      if (firstRunStatus.resume_available) return preparedPathsMatch ? '检测到可恢复数据，请继续准备' : '可恢复任务属于另一组目录，请先载入原始路径'
      if (firstRunStatus.state === 'failed') return '首次数据准备失败，可检查错误后重试'
      if (firstRunStatus.state === 'cancelled') return '首次数据准备已取消，可从保留结果继续'
      return '还需完成首次运行数据准备'
    }
    if (!selection) return '还需在地图中框选导出区块'
    if (!allGroupsSelected && selectedGroups.length === 0) return '至少选择一个导出分组'
    if (exportMode === 'blend' && !sourcePaths.blender.trim()) return 'Blender 场景模式需要选择本机 Blender 4.4 或更新版本'
    if (exportMode === 'blend' && !allGroupsSelected && !selectedGroups.some((group) => RECONSTRUCTABLE_BLEND_GROUPS.includes(group))) {
      return '当前分组只有待接入组件，无法生成场景；请选择建筑、道具、植被、地形、道路或未知 / 其它'
    }
    if (!outputLabel.trim()) return '请填写输出标签'
    return '所有前置条件已完成'
  }, [allGroupsSelected, exportMode, firstRunAction, firstRunRunning, firstRunStatus, outputLabel, preparedPathsMatch, selectedGroups, selection, sourcePaths.blender, sourceReady])

  const reconstructableSelection = allGroupsSelected || selectedGroups.some((group) => RECONSTRUCTABLE_BLEND_GROUPS.includes(group))

  const isReady = Boolean(
    sourceReady &&
      selection &&
      (allGroupsSelected || selectedGroups.length > 0) &&
      (exportMode !== 'blend' || (sourcePaths.blender.trim() && reconstructableSelection)) &&
      outputLabel.trim() &&
      launchState !== 'running',
  )

  useEffect(() => {
    if (isReady && !previousReady.current) {
      setArmReveal(false)
      const frame = window.requestAnimationFrame(() => setArmReveal(true))
      const timer = window.setTimeout(() => setArmReveal(false), 1150)
      previousReady.current = true
      return () => {
        window.cancelAnimationFrame(frame)
        window.clearTimeout(timer)
      }
    }
    if (!isReady) previousReady.current = false
  }, [isReady])

  useEffect(() => {
    setCompletionVisible(launchState === 'completed')
  }, [launchState])

  const launchVisualTask = () => {
    if (!isReady || !selection) return
    stopJob()
    setLaunchState('running')
    setProgress(0)
    setJobError('')
    setJobMessage('正在校验请求并准备本机构建')
    const request = {
      format: 'EndfieldWebUIExportJob/2',
      export_mode: exportMode,
      map_id: mapId,
      selection: { type: 'world_bounds', ...selection.canonicalBounds },
      batch_mode: chunkMode,
      layers: {
        terrain: layers.terrain,
        instances: layers.instances,
        roads: layers.instances,
        vegetation: layers.instances,
        water: layers.water,
        effects: layers.effects,
        lights: layers.effects,
      },
      export_groups: allGroupsSelected ? ['all'] : selectedGroups,
      effects: { selection_mode: effectsMode },
      water_mode: exportMode === 'blend' ? 'stable_eevee' : waterMode,
      source: { game_root: sourcePaths.game, blender_exe: sourcePaths.blender.trim() },
      output: { root: sourcePaths.output, label: outputLabel },
    }

    const poll = (jobId: string, after = 0) => {
      const tick = async () => {
        try {
          const [jobResponse, eventsResponse] = await Promise.all([
            fetch(`/api/v2/export-jobs/${encodeURIComponent(jobId)}`),
            fetch(`/api/v2/export-jobs/${encodeURIComponent(jobId)}/events?after=${after}`),
          ])
          const job = await jobResponse.json() as ExportJob & { message?: string }
          const eventPayload = await eventsResponse.json() as { events?: Array<{ sequence?: number; message?: string }> }
          if (!jobResponse.ok) throw new Error(job.message ?? 'job_status_failed')
          const events = eventPayload.events ?? []
          const newest = events.at(-1)
          const nextAfter = newest?.sequence ?? after
          if (newest?.message) setJobMessage(newest.message)
          setProgress(Math.max(0, Math.min(100, job.progress * 100)))
          if (job.state === 'completed') {
            stopJob()
            const resultMode = job.result?.package?.exportMode === 'blend'
              ? 'blend'
              : job.result?.package?.exportMode === 'data_package'
                ? 'data_package'
                : request.export_mode
            const build = job.result?.blenderBuild ?? null
            setCompletedExportMode(resultMode)
            setCompletedBlenderBuild(build)
            setLaunchState('completed')
            setActiveJobId(null)
            const partialBuild = build?.status === 'partial' || (build?.pending ?? 0) > 0
            notify(resultMode === 'blend'
              ? partialBuild
                ? `场景文件已导出；${build?.pending ?? 0} 项待处理，详见报告`
                : `场景文件已导出：${build?.scenes?.length ?? 0} 批`
              : `数据包导出完成：${job.result?.package?.status ?? 'completed'}`)
            return
          }
          if (job.state === 'cancelled') {
            stopJob()
            setLaunchState('cancelled')
            setActiveJobId(null)
            notify('任务已在阶段边界取消；已有日志和部分产物均已保留')
            return
          }
          if (job.state === 'failed') {
            stopJob()
            const message = job.error?.message ?? '未知后端错误'
            setJobError(message)
            setLaunchState('failed')
            setActiveJobId(null)
            notify(`提取失败：${message}`)
            return
          }
          jobTimer.current = window.setTimeout(() => void poll(jobId, nextAfter), 500)
        } catch (error) {
          stopJob()
          const message = error instanceof Error ? error.message : String(error)
          setJobError(message)
          setLaunchState('failed')
          setActiveJobId(null)
          notify(`无法读取任务状态：${message}`)
        }
      }
      void tick()
    }

    void (async () => {
      try {
        const validationResponse = await fetch('/api/v2/export-jobs/validate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(request),
        })
        const validation = await validationResponse.json() as {
          status?: string
          message?: string
          error?: { message?: string }
        }
        if (!validationResponse.ok || validation.status !== 'valid') {
          throw new Error(validation.message ?? validation.error?.message ?? 'export_request_validation_failed')
        }
        const response = await fetch('/api/v2/export-jobs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(request),
        })
        const payload = await response.json() as (ExportJob & { message?: string }) | FirstRunApiError
        if (!response.ok || !('job_id' in payload) || !payload.job_id) {
          const firstRunPayload = payload as FirstRunApiError
          if (firstRunPayload.firstRun) setFirstRunStatus(firstRunPayload.firstRun)
          if (firstRunPayload.error?.code === 'first_run_required') {
            setDrawerCollapsed(false)
            throw new Error('需要先完成首次运行数据准备')
          }
          throw new Error('message' in payload && payload.message ? payload.message : firstRunErrorMessage(firstRunPayload, 'export_job_submit_failed'))
        }
        setActiveJobId(payload.job_id)
        notify(exportMode === 'blend' ? 'Blender 场景构建任务已提交' : '数据包导出任务已提交')
        poll(payload.job_id)
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        setJobError(message)
        setLaunchState('failed')
        notify(`无法启动提取：${message}`)
      }
    })()
  }

  const cancelVisualTask = async () => {
    if (launchState !== 'running' || !activeJobId) return
    try {
      const response = await fetch(`/api/v2/export-jobs/${encodeURIComponent(activeJobId)}/cancel`, { method: 'POST' })
      if (!response.ok) throw new Error('cancel_request_failed')
      setJobMessage('已请求协作取消；等待当前安全阶段结束')
      notify('取消请求已提交；已经写入的结果会保留')
    } catch {
      notify('取消请求发送失败；任务仍在运行')
    }
  }

  const copyRequest = async () => {
    const summary = {
      format: 'EndfieldWebUIExportJob/2',
      mode: exportMode === 'blend' ? 'portable-scene-and-data-package-export' : 'stage119-data-package-export',
      export_mode: exportMode,
      map_id: mapId,
      requested_bounds: selection?.requestedBounds ?? null,
      canonical_bounds: selection?.canonicalBounds ?? null,
      sector_keys: selection?.sectors.map((sector) => sector.key) ?? [],
      batch_mode: chunkMode,
      resolver_version: REGION_MAP_RUNTIME_MANIFEST.resolverVersion,
      preview_layers: layers,
      export_groups: allGroupsSelected ? ['all'] : selectedGroups,
      ui_requested_groups: allGroupsSelected ? ['all'] : selectedGroups,
      effects_mode: effectsMode,
      water_mode: exportMode === 'blend' ? 'stable_eevee' : waterMode,
      region_map_chunk_selection: selection,
      source: { game_root: sourcePaths.game, blender_exe: sourcePaths.blender.trim() },
      output: { root: sourcePaths.output, label: outputLabel },
    }
    try {
      await navigator.clipboard.writeText(JSON.stringify(summary, null, 2))
      notify('请求摘要已复制')
    } catch {
      notify('当前浏览器未开放剪贴板权限')
    }
  }

  const activePhase = useMemo(() => {
    let phase = 0
    JOB_PHASES.forEach((entry, index) => {
      if (progress >= entry[2]) phase = index
    })
    return phase
  }, [progress])

  const chunkPreview = useMemo(
    () => (selection?.sectors ?? []).slice(0, 24).map((sector, index) => ({
      id: `B${String(index + 1).padStart(2, '0')}`,
      x: sector.unityBoundsXZ.xmin,
      z: sector.unityBoundsXZ.zmin,
      levelId: sector.levelId ?? 'UNMAPPED',
      bindingStatus: sector.coverage,
    })),
    [selection],
  )

  const workload = useMemo(() => {
    if (!selection) return 'NO REGION'
    if (selection.sectors.length < 25) return 'QUICK'
    if (selection.sectors.length < 90) return 'STANDARD'
    return 'EXTENDED'
  }, [selection])

  const coverageSummary = useMemo(() => {
    if (!selection) return { mapped: 0, unmapped: 0 }
    const mapped = selection.sectors.filter((sector) => sector.coverage === 'mapped').length
    return { mapped, unmapped: selection.sectors.length - mapped }
  }, [selection])

  const updateChunkMode = (nextMode: ChunkMode) => {
    if (launchState === 'running') {
      notify('任务运行中不能修改分块模式')
      return
    }
    setChunkMode(nextMode)
    setSelection((current) =>
      current
        ? resolveSelection(
            REGION_MAP_RUNTIME_MANIFEST,
            mapId,
            current.requestedBounds,
            nextMode,
          )
        : null,
    )
    setLaunchState('idle')
    setProgress(0)
  }

  const validationMatches = Boolean(
    firstRunValidation &&
      firstRunValidation.canonical.game_root === sourcePaths.game &&
      firstRunValidation.canonical.export_root === sourcePaths.output &&
      firstRunValidation.canonical.cache_root === sourcePaths.cache &&
      (firstRunValidation.canonical.blender_exe ?? '') === sourcePaths.blender.trim(),
  )
  const firstRunCanValidate = Boolean(
    sourcePaths.game.trim() &&
      sourcePaths.output.trim() &&
      !firstRunRunning &&
      firstRunStatus?.actions.can_validate &&
      !firstRunAction,
  )
  const firstRunCanReconnect = !firstRunStatus && !firstRunAction
  const firstRunCanStart = Boolean(
    validationMatches &&
      firstRunValidation?.status === 'valid' &&
      firstRunStatus?.actions.can_start &&
      !firstRunAction,
  )
  const firstRunCanResume = Boolean(
    firstRunStatus?.actions.can_resume &&
      preparedPathsMatch &&
      !firstRunAction,
  )
  const firstRunPhaseProgress = Math.round((firstRunStatus?.progress ?? 0) * 100)
  const firstRunPhaseNumber = firstRunStatus?.phase_index === null || firstRunStatus?.phase_index === undefined
    ? '--'
    : String(firstRunStatus.phase_index + 1).padStart(2, '0')
  const firstRunPhaseCount = String(firstRunStatus?.phase_count ?? 14).padStart(2, '0')
  const firstRunStateLabel = sourceReady
    ? 'DATA READY'
    : firstRunRunning
      ? 'PREPARING'
      : firstRunStatus?.resume_available
        ? 'RESUMABLE'
        : firstRunStatus?.state === 'failed'
          ? 'FAILED'
          : firstRunStatus?.state === 'cancelled'
            ? 'CANCELLED'
            : 'SETUP REQUIRED'

  const buttonTitle =
    launchState === 'running'
      ? JOB_PHASES[activePhase][1]
      : launchState === 'completed'
        ? completedExportMode === 'blend' && (completedBlenderBuild?.status === 'partial' || (completedBlenderBuild?.pending ?? 0) > 0)
          ? '场景已导出 · 有待处理项'
          : completedExportMode === 'blend'
            ? '场景导出与校验完成'
            : '数据包导出与审计完成'
        : launchState === 'failed'
          ? '提取失败 · 检查提示'
          : launchState === 'cancelled'
            ? '任务已安全取消'
        : isReady
          ? exportMode === 'blend' ? '构建 Blender 场景' : '开始导出数据包'
          : '等待设置完成'

  return (
    <div className={`app-shell theme-${mapId}`} data-map-theme={mapId}>
      <div className="ambient-grid" aria-hidden="true" />
      <div className="ambient-contours" aria-hidden="true"><i /><i /></div>

      <header
        className="topbar"
        data-active-map={mapId === 'map01' ? 'M01 / IV' : 'M02 / WL'}
        style={{ viewTransitionName: 'persistent-header' }}
      >
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true"><span>//</span></div>
          <div>
            <small>Endfield map recovery</small>
            <strong>ATLAS / COMPOSER</strong>
          </div>
        </div>

        <div className="map-switch-zone">
          <span className="map-switch-hint">选择地图 · 点击切换场景</span>
          <nav className="map-switch" aria-label="选择地图">
            {(Object.keys(MAPS) as MapId[]).map((id) => {
              const item = MAPS[id]
              const active = id === mapId
              return (
                <button
                  key={id}
                  type="button"
                  data-testid={`map-tab-${id}`}
                  className={active ? 'active' : ''}
                  aria-pressed={active}
                  onClick={() => handleMapChange(id)}
                >
                  {active && (
                    <ViewTransition name="map-switch-active" share="tab-underline" default="none">
                      <span className="map-switch-active" aria-hidden="true" />
                    </ViewTransition>
                  )}
                  <span className={`map-thumb map-thumb-${id}`} aria-hidden="true" />
                  <span className="map-switch-copy">
                    <strong>{item.shortLabel}</strong>
                    <small>{item.chineseName} / {id === 'map01' ? '2048' : '4096'}</small>
                  </span>
                  <i aria-hidden="true">→</i>
                </button>
              )
            })}
          </nav>
        </div>

          <div className="top-actions">
            <div className={`top-status ${sourceReady ? 'ready' : ''}`}>
              <i />
              <span>{sourceReady ? 'DATA / READY' : firstRunRunning ? 'DATA / PREPARING' : 'FIRST RUN / REQUIRED'}</span>
            </div>
          <button type="button" onClick={copyRequest}><Clipboard size={14} /><span>复制摘要</span></button>
          <button type="button" onClick={resetPrototype}><RefreshCw size={14} /><span>复位</span></button>
        </div>
      </header>

      <main className="workspace">
        <section
          className="map-heading"
          data-map-code={mapId === 'map01' ? 'M01 / FRONTIER' : 'M02 / XI-RANG'}
          data-map-title={map.title}
          aria-live="polite"
        >
          <span>Orthographic region selector / sector128 resolver</span>
          <ViewTransition key={mapId} name="map-title" share="text-morph" default="none">
            <div className="map-title-lockup">
              <h1>{map.title}</h1>
              <strong>{map.chineseName}</strong>
            </div>
          </ViewTransition>
          <div className="map-heading-meta">
            <span>{map.tagline}</span>
            <i />
            <span>{map.coordinates}</span>
          </div>
        </section>

        <section className="map-stage" aria-label="地图选择主舞台">
          <div className="map-stage-frame" data-map-stage={mapId}>
            <div className="frame-corner corner-nw" aria-hidden="true" />
            <div className="frame-corner corner-se" aria-hidden="true" />
            <div className="map-stage-signature" aria-hidden="true">
              <span className="signature-index">{mapId === 'map01' ? '04' : '息'}</span>
              <span className="signature-rule" />
              <span className="signature-copy">
                {mapId === 'map01' ? 'ORTHO / INDUSTRIAL SURVEY' : 'XI-RANG / ECOLOGIC MATRIX'}
              </span>
              <span className="signature-blocks"><i /><i /><i /><i /><i /><i /></span>
            </div>
            <div className="map-stage-vector" aria-hidden="true">
              <span>{mapId === 'map01' ? 'ADVANCE / CLAIM / BUILD' : 'GROW / BIND / COEXIST'}</span>
              <i /><i /><i />
            </div>
            <div className={`map-placeholder-note ${overviewSource === 'fallback' ? 'fallback' : ''}`}><i /> {overviewLabel}</div>
            <AnimatePresence initial={false} mode="wait">
              <motion.div
                key={mapId}
                className={`map-transition-screen map-transition-${transitionDirection}`}
                initial={{
                  opacity: 0,
                  x: reduceMotion ? '0%' : transitionDirection === 'forward' ? '4.2%' : '-4.2%',
                  scale: reduceMotion ? 1 : 1.006,
                }}
                animate={{
                  opacity: overviewSource === 'loading' ? 0 : 1,
                  x: overviewSource === 'loading' && !reduceMotion ? (transitionDirection === 'forward' ? '4.2%' : '-4.2%') : '0%',
                  scale: overviewSource === 'loading' && !reduceMotion ? 1.006 : 1,
                  transition: {
                    duration: overviewSource === 'loading' ? 0 : reduceMotion ? 0.01 : 0.86,
                    ease: [0.16, 1, 0.3, 1],
                  },
                }}
                exit={{
                  opacity: 0,
                  x: reduceMotion ? '0%' : transitionDirection === 'forward' ? '-2.8%' : '2.8%',
                  scale: reduceMotion ? 1 : 0.994,
                  transition: {
                    duration: reduceMotion ? 0.01 : 0.38,
                    ease: [0.4, 0, 1, 1],
                  },
                }}
              >
                <MapViewport
                  mapId={mapId}
                  layers={layers}
                  chunkMode={chunkMode}
                  selection={selection}
                  overviewRevision={overviewRevision}
                  onSelectionChange={handleSelectionChange}
                  onOverviewSourceChange={handleOverviewSourceChange}
                />
              </motion.div>
            </AnimatePresence>
            <MapTransitionVeil mapId={mapId} direction={transitionDirection} overviewSource={overviewSource} />
          </div>

          <div className="map-data-ribbon" aria-label="选区摘要">
            <div>
              <span>CANONICAL REGION</span>
              <strong>{selection ? `X ${selection.canonicalBounds.xmin}:${selection.canonicalBounds.xmax} / Z ${selection.canonicalBounds.zmin}:${selection.canonicalBounds.zmax}` : 'DRAG TO SELECT'}</strong>
            </div>
            <div>
              <span>SECTORS</span>
              <strong>{selection?.sectors.length ?? '00'}</strong>
              <small>{selection ? `${coverageSummary.mapped} M / ${coverageSummary.unmapped} U` : '128 × 128'}</small>
            </div>
            <div>
              <span>PACKAGING</span>
              <strong>{chunkMode === 'per_sector' ? 'PER SECTOR' : chunkMode === 'cluster_4x4_sectors' ? '4 × 4 SECTORS' : 'MERGED'}</strong>
              <small>{workload}</small>
            </div>
            <div className="manifest-state">
              <span>SELECTION RESOLVER</span>
              <strong>SECTOR128 / RESOLVED</strong>
              <small>LEVEL ID · H TILE · BLENDER XY</small>
              <div className="ribbon-pulse" aria-hidden="true">
                {Array.from({ length: 12 }, (_, index) => <i key={index} style={{ '--pulse-index': index } as CSSProperties} />)}
              </div>
            </div>
          </div>
        </section>

        <section
          className={`settings-drawer ${drawerCollapsed ? 'collapsed' : ''}`}
          aria-label="提取设置"
          style={{ viewTransitionName: 'persistent-settings' }}
        >
          <header>
            <div><small>Extraction settings</small><strong>提取设置</strong></div>
            <button
              type="button"
              onClick={() => setDrawerCollapsed((value) => !value)}
              aria-expanded={!drawerCollapsed}
              aria-label={drawerCollapsed ? '展开提取设置' : '折叠提取设置'}
            >
              <ChevronDown size={17} />
            </button>
          </header>

          <div className="settings-scroll">
            <SettingSection key={sourceReady ? 'first-run-ready' : 'first-run-setup'} title="首次运行准备" code="FIRST RUN / DEPLOY" defaultOpen={!sourceReady}>
              <div className={`first-run-console state-${firstRunStatus?.state ?? 'offline'} ${sourceReady ? 'is-ready' : ''}`}>
                <div className="first-run-heading">
                  <div>
                    <small>LOCAL DATA CALIBRATION</small>
                    <strong>{firstRunStateLabel}</strong>
                  </div>
                  <span>{firstRunPhaseNumber} / {firstRunPhaseCount}</span>
                </div>
                <div className="first-run-progress" aria-label={`当前阶段进度 ${firstRunPhaseProgress}%`}>
                  <i style={{ width: `${firstRunPhaseProgress}%` }} />
                  <b style={{ left: `${firstRunPhaseProgress}%` }} />
                </div>
                <div className="first-run-phase">
                  <span>{FIRST_RUN_PHASE_LABELS[firstRunStatus?.phase ?? 'idle'] ?? firstRunStatus?.phase ?? '连接本机服务'}</span>
                  <strong>{firstRunPhaseProgress}% <small>PHASE</small></strong>
                </div>
                <p>{firstRunStatus?.message ?? '正在读取本机首次运行状态。首次完整准备约需 46.3 GiB，并可能持续约 1 小时 50 分钟。'}</p>
              </div>

              <PathField
                id="game-root"
                label="游戏目录 / READ ONLY"
                value={sourcePaths.game}
                caption="SOURCE LOCK · 游戏安装保持只读"
                icon={<Folder size={14} />}
                busy={choosingPath === 'game'}
                onChoose={() => void choosePath('game')}
              />
              <PathField
                id="output-root"
                label="输出目录"
                value={sourcePaths.output}
                caption="EXPORT ROOT · 所有结果写入外部目录"
                icon={<Folder size={14} />}
                busy={choosingPath === 'output'}
                onChoose={() => void choosePath('output')}
              />
              <PathField
                id="cache-root"
                label="外部缓存目录 / 可选"
                value={sourcePaths.cache}
                caption="FIRST-RUN WORKSPACE · 留空时使用输出目录下的外部缓存"
                icon={<Database size={14} />}
                busy={choosingPath === 'cache'}
                onChoose={() => void choosePath('cache')}
              />
              <PathField
                id="blender-exe"
                label="Blender 程序 / 可选"
                value={sourcePaths.blender}
                caption="BLENDER EXECUTABLE · 场景导出需要 Blender 4.4+；仅校验路径，不执行版本探测"
                icon={<Folder size={14} />}
                busy={choosingPath === 'blender' || launchState === 'running'}
                onChoose={() => void choosePath('blender')}
              />
              <div className="first-run-boundary-note">
                <HardDrive size={13} />
                <span>首次准备会生成大量本机缓存；游戏安装目录保持严格只读，缓存与导出根必须位于游戏目录之外。</span>
              </div>

              {firstRunValidation && validationMatches && (
                <div className={`first-run-validation ${firstRunValidation.status} ${firstRunValidation.space_warning ? 'has-warning' : ''}`}>
                  <div className="validation-space">
                    {(Object.entries(firstRunValidation.space) as Array<[string, FirstRunValidation['space']['cache']]>).map(([key, item]) => (
                      <div key={key} className={item.warning ? 'warning' : ''}>
                        <span>{key === 'cache' ? 'CACHE BASELINE' : 'EXPORT BASELINE'}</span>
                        <strong>{formatBytes(item.free_bytes)} FREE</strong>
                        <small>{formatBytes(item.required_bytes)} REQUIRED</small>
                      </div>
                    ))}
                  </div>
                  <div className="validation-prerequisites">
                    <span className={firstRunValidation.prerequisites.git_available ? 'ok' : 'missing'}>GIT</span>
                    <span className={firstRunValidation.prerequisites.dotnet_available ? 'ok' : 'missing'}>.NET</span>
                    <span className="ok">PINNED HELPER</span>
                    <span>NETWORK / INITIAL</span>
                  </div>
                  {firstRunValidation.missing.length > 0 && <p>{firstRunValidation.missing.join(' / ')}</p>}
                </div>
              )}

              {firstRunError && (
                <div className="first-run-error" role="alert">
                  <TriangleAlert size={13} />
                  <span>{firstRunError}</span>
                </div>
              )}

              <div className="first-run-actions">
                <button
                  type="button"
                  onClick={() => void (firstRunStatus ? validateFirstRun() : loadFirstRunStatus())}
                  disabled={firstRunStatus ? !firstRunCanValidate : !firstRunCanReconnect}
                >
                  {firstRunAction === 'validate' || firstRunAction === 'status' ? <RefreshCw className="path-picker-spin" size={13} /> : firstRunStatus ? <ShieldCheck size={13} /> : <RefreshCw size={13} />}
                  <span>{firstRunStatus ? '校验目录' : '重新连接服务'}</span>
                </button>
                {firstRunStatus?.resume_available ? (
                  <button
                    className="primary"
                    type="button"
                    onClick={firstRunCanResume ? () => void submitFirstRun(true) : restoreFirstRunRequestPaths}
                    disabled={Boolean(firstRunAction) || !firstRunStatus.request}
                  >
                    {firstRunAction === 'resume' ? <RefreshCw className="path-picker-spin" size={13} /> : <Play size={13} fill="currentColor" />}
                    <span>{firstRunCanResume ? '继续准备' : '载入恢复路径'}</span>
                  </button>
                ) : (
                  <button className="primary" type="button" onClick={() => void submitFirstRun(false)} disabled={!firstRunCanStart}>
                    {firstRunAction === 'start' ? <RefreshCw className="path-picker-spin" size={13} /> : <Play size={13} fill="currentColor" />}
                    <span>{sourceReady ? '必要文件已就绪' : '提取必要文件'}</span>
                  </button>
                )}
                {firstRunStatus?.actions.can_cancel && (
                  <button className="danger" type="button" onClick={() => void cancelFirstRun()} disabled={firstRunAction === 'cancel'}>
                    <Square size={11} fill="currentColor" />
                    <span>{firstRunStatus.cancel_requested ? '正在取消' : '安全取消'}</span>
                  </button>
                )}
              </div>

              {(firstRunEvents.length > 0 || firstRunRunning) && (
                <div className="first-run-events" aria-live="polite">
                  {firstRunEvents.slice(-4).map((event) => (
                    <div key={event.sequence}>
                      <span>{String(event.sequence).padStart(4, '0')}</span>
                      <i />
                      <p>{event.message ?? FIRST_RUN_PHASE_LABELS[event.phase] ?? event.phase}</p>
                      <strong>{Math.round(event.progress * 100)}%</strong>
                    </div>
                  ))}
                  {!firstRunEvents.length && <div><span>0000</span><i /><p>等待首个阶段事件</p><strong>--</strong></div>}
                </div>
              )}

              <div className="first-run-capabilities" aria-label="首次准备能力边界">
                <div><span>MAP DATASET</span><strong>{firstRunStatus?.capabilities.map_dataset ?? 'not_ready'}</strong></div>
                <div><span>ASSET RESOLUTION</span><strong>{Object.values(firstRunStatus?.capabilities.asset_resolution ?? {}).some((value) => value === 'automatic_partial') ? 'AUTOMATIC / PARTIAL' : Object.values(firstRunStatus?.capabilities.asset_resolution ?? {}).join(' / ') || 'not_ready'}</strong></div>
                <div><span>SCENE INPUT CACHE</span><strong>{firstRunStatus?.capabilities.blender_build ?? 'not_ready'}</strong></div>
              </div>
              <p className="capability-disclaimer">AUTOMATIC_PARTIAL 仅表示自动收集的部分解析结果，不代表完整可信的 Blender 几何或材质。</p>
              <p className="capability-disclaimer">场景从本机首次准备缓存构建；未解析资源和暂未接入的组件会保留在报告中。场景导出需要 Blender 4.4 或更新版本。</p>
            </SettingSection>

            <SettingSection title="预览与导出" code="04 LAYERS / 10 GROUPS">
              <div className="setting-block">
                <span className="setting-label"><Layers3 size={12} />图层开关 · 同时影响地图预览与导出内容</span>
                <div className="layer-grid">
                  {(Object.keys(LAYER_LABELS) as LayerId[]).map((layer) => (
                    <label key={layer}>
                      <input type="checkbox" checked={layers[layer]} onChange={() => toggleLayer(layer)} />
                      <span className="fake-check"><Check size={10} /></span>
                      <strong>{LAYER_LABELS[layer][0]}</strong>
                      <small>{LAYER_LABELS[layer][1]}</small>
                    </label>
                  ))}
                </div>
              </div>

              <label className="full-field">
                <span>输出格式</span>
                <div className="select-shell">
                  <select value={exportMode} onChange={(event) => updateExportMode(event.target.value as ExportMode)} disabled={launchState === 'running'}>
                    <option value="blend">Blender 场景 + 数据包（默认）</option>
                    <option value="data_package">仅审计数据包</option>
                  </select>
                  <ChevronDown size={13} />
                </div>
                <small>{exportMode === 'blend'
                  ? '从本机缓存构建并验证 .blend；未解析资源留在报告。'
                  : '按所选范围生成审计数据包；水体选项仅作为 metadata 保存。'}</small>
              </label>

              <div className="setting-block">
                <span className="setting-label"><Box size={12} />导出分组 · 全部或独立多选</span>
                <button
                  className={`export-all-toggle ${allGroupsSelected ? 'active' : ''}`}
                  type="button"
                  aria-pressed={allGroupsSelected}
                  disabled={launchState === 'running'}
                  onClick={selectAllGroups}
                >
                  <span>{allGroupsSelected && <Check size={9} />}</span>
                  <strong>全部分组</strong>
                  <small>默认 · 含未接入项报告</small>
                </button>
                <div className="export-grid">
                  {EXPORT_GROUPS.map(([id, label, code]) => {
                    const active = !allGroupsSelected && selectedGroups.includes(id)
                    const note = EXPORT_GROUP_NOTES[id]
                    return (
                      <button
                        key={id}
                        type="button"
                        className={active ? 'active' : ''}
                        aria-pressed={active}
                        disabled={launchState === 'running'}
                        onClick={() => toggleGroup(id)}
                      >
                        <span>{active && <Check size={9} />}</span>
                        <strong>{label}</strong>
                        <small>{note ? `${code} · ${note}` : code}</small>
                      </button>
                    )
                  })}
                </div>
              </div>
            </SettingSection>

            <SettingSection title="区块与构建策略" code="CHUNK / FX / WATER">
              <div className="field-pair">
                <label>
                  <span><SquareDashedMousePointer size={12} />区块打包方式</span>
                  <div className="select-shell">
                    <select value={chunkMode} onChange={(event) => updateChunkMode(event.target.value as ChunkMode)}>
                      <option value="per_sector">每 sector 独立</option>
                      <option value="cluster_4x4_sectors">4 × 4 sectors 稳定分组</option>
                      <option value="merged_selection">合并选区</option>
                    </select>
                    <ChevronDown size={13} />
                  </div>
                </label>
                <label>
                  <span><Waves size={12} />水体模式 · {exportMode === 'blend' ? '场景自动绑定待接入' : '仅记录 metadata'}</span>
                  <div className="select-shell">
                    <select value={waterMode} onChange={(event) => setWaterMode(event.target.value)} disabled={exportMode === 'blend' || launchState === 'running'}>
                      <option value="stable_eevee">stable_eevee</option>
                      <option value="flowmap_fresnel">flowmap_fresnel</option>
                    </select>
                    <ChevronDown size={13} />
                  </div>
                  {exportMode === 'blend' && <small>地图水体自动绑定尚未接入；当前会把它列入待处理报告。</small>}
                </label>
              </div>
              <label className="full-field">
                <span><Settings2 size={12} />特效选择策略</span>
                <div className="select-shell">
                  <select value={effectsMode} disabled aria-label="特效选择策略：按锚点保留完整系统">
                    <option value="full_system_by_anchor">按锚点保留完整系统</option>
                  </select>
                  <ChevronDown size={13} />
                </div>
              </label>
              <label className="full-field">
                <span>输出标签</span>
                <input value={outputLabel} onChange={(event) => setOutputLabel(event.target.value)} />
              </label>
              <div className="strategy-note">
                <i />
                <span>{CHUNK_MODE_LABELS[chunkMode]}</span>
                <button type="button" onClick={() => handleSelectionChange(null)} disabled={!selection}><X size={12} />清除框选</button>
              </div>
            </SettingSection>

            <div className={`settings-state ${isReady ? 'ready' : ''}`}>
              <i />
              <span>{readyReason}</span>
            </div>
          </div>
        </section>

        <section
          className={`launch-pod ${isReady ? 'ready' : ''} ${armReveal ? 'arm-reveal' : ''} state-${launchState}`}
          aria-label="提取动作"
          style={{ viewTransitionName: 'persistent-launch' }}
        >
          <div className="launch-overline">
            <span>REGION / SECTOR EXPORT</span>
            <i />
            <strong>{launchState === 'running' ? `${Math.round(progress)}%` : isReady ? 'ARMED' : 'LOCKED'}</strong>
          </div>

          <button className="launch-button" type="button" onClick={launchVisualTask} disabled={!isReady || launchState === 'running'}>
            <span className="launch-base" />
            <span className="launch-tracer tracer-a" />
            <span className="launch-tracer tracer-b" />
            <span className="launch-tracer tracer-c" />
            <span className="launch-title">{buttonTitle}</span>
            <span className="launch-subtitle">
              {selection ? `${selection.sectors.length} SECTORS / ${selection.batches.length} BATCHES / ${workload}` : 'SOURCE · REGION · OUTPUT'}
            </span>
            <Play className="launch-arrow" size={27} fill="currentColor" />
          </button>

          <div className="chunk-progress" aria-label="区块导出预览">
            <div className="chunk-progress-head">
              <span>{selection ? `${selection.sectors.length} SECTORS / ${coverageSummary.unmapped} UNMAPPED` : 'SECTOR128 SELECTION / READY'}</span>
              {launchState === 'running' && <button type="button" onClick={cancelVisualTask}>取消</button>}
            </div>
            <div className="chunk-mini-grid">
              {chunkPreview.length ? chunkPreview.map((chunk, index) => {
                const threshold = ((index + 1) / chunkPreview.length) * 100
                const active = launchState === 'completed' || progress >= threshold
                return (
                  <span key={`${chunk.x}-${chunk.z}`} className={active ? 'active' : ''} title={`${chunk.id} · ${chunk.levelId} · ${chunk.bindingStatus} · X ${chunk.x} / Z ${chunk.z}`}>
                    {chunk.id}
                  </span>
                )
              }) : Array.from({ length: 12 }, (_, index) => <span key={index}>-</span>)}
            </div>
            <div className="phase-track">
              {JOB_PHASES.map(([code, label], index) => (
                <div key={code} className={launchState !== 'idle' && index <= activePhase ? 'active' : ''}>
                  <i />
                  <span>{code}</span>
                  <small>{label}</small>
                </div>
              ))}
            </div>
          </div>

          <p>
            {launchState === 'running'
              ? jobMessage || '真实任务正在运行'
              : launchState === 'failed'
                ? jobError || '后端任务失败，请检查本机日志'
                : isReady
                  ? exportMode === 'blend'
                    ? '场景由本机首次准备缓存构建；未解析资源和待接入组件会保留在报告。'
                    : '将按所选分组生成审计数据包；预览图层开关会影响导出内容。'
                  : sourceReady
                    ? '完成地图框选和导出设置后解锁。'
                    : '先在提取设置中完成首次运行数据准备；地图仍可浏览和框选。'}
          </p>
        </section>
      </main>

      <footer className="footer-strip">
        <strong>ENDFIELD / ATLAS</strong>
        <i />
        <span>{map.title} · {sourceReady ? 'FIRST RUN READY' : firstRunRunning ? 'DATA PREPARING' : 'SETUP REQUIRED'} · SECTOR128 RESOLVED · {exportMode === 'blend' ? 'BLENDER SCENE + DATA PACKAGE' : 'DATA PACKAGE EXPORT'}</span>
      </footer>

      <LegalArchive />

      <AnimatePresence>
        {toast && (
          <motion.div
            className="toast"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
          >
            <CircleDotDashed size={14} />
            {toast}
          </motion.div>
        )}
      </AnimatePresence>

      <ExtractionCompleteOverlay
        visible={completionVisible}
        mapId={mapId}
        mapName={map.chineseName}
        sectorCount={selection?.sectors.length ?? 0}
        exportMode={completedExportMode}
        blenderBuild={completedBlenderBuild}
        onFinished={dismissCompletion}
      />
    </div>
  )
}
