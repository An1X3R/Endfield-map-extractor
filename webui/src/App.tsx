import {
  startTransition,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  ViewTransition,
} from 'react'
import type { ComponentType, CSSProperties, ReactNode } from 'react'
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
import { LAYER_LABELS, MAPS } from './data/maps'
import { AmbientContours } from './components/AmbientContours'
import { ExtractionCompleteOverlay } from './components/ExtractionCompleteOverlay'
import { ExportGroupSelector } from './components/ExportGroupSelector'
import { MapViewport } from './components/MapViewport'
import { captureCanvasMap, capturePaintingMap } from './lib/mapSnapshot'
import type { CanvasMapSnapshot } from './lib/mapSnapshot'
import { RegionCartouche } from './components/RegionCartouche'
import { REGION_MAP_RUNTIME_MANIFEST } from './data/regionMapRuntime'
import { BackendRequestError, isJsonObject, postJson, requestJson } from './lib/backendClient'
import { resolveSelection } from './lib/mapChunking'
import { isExportJob, isExportJobEventsResponse, isExportJobValidation } from './lib/mapExportJob'
import { canStartFirstRunRequest, firstRunRequestMatchesPrepared } from './lib/firstRunActions'
import type { ExportJob } from './lib/mapExportJob'
import { useDraggablePanel } from './hooks/useDraggablePanel'
import { InstancedSceneWorkspace } from './features/instanced-scenes/InstancedSceneWorkspace'
import { OperatorWorkspace } from './features/operators/OperatorWorkspace'
import type { OperatorDevelopmentPreview } from './features/operators/developmentPreview'
import type { InstancedSceneDevelopmentPreview, InstancedSceneSourcePaths } from './features/instanced-scenes/types'
import { PaintingWorkspace } from './features/si-painting/PaintingWorkspace'
import { usePaintingRuntime } from './features/si-painting/usePaintingRuntime'
import './layout.css'
import './instanced-navigation.css'
import './features/operators/navigation.css'
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

const DEFAULT_SOURCE_PATHS = {
  game: '',
  output: '',
  cache: '',
  blender: '',
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
  dependencies: '检查运行依赖',
  preflight: '只读边界预检',
  helper_dependencies: '校验已准备的工具',
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

const FIRST_RUN_HELPER_PHASE_LABELS: Record<string, string> = {
  load_unpack: '读取与解包',
  object_parse: '对象解析',
  mesh_export: '网格导出',
  texture_export: '纹理转换',
  read: '读取数据',
}

const FIRST_RUN_ERROR_LABELS: Record<string, string> = {
  invalid_request: '目录或请求格式无效',
  first_run_paths_incomplete: '目录校验尚未通过',
  already_running: '数据准备任务正在运行',
  first_run_required: '需要先完成首次数据准备',
  already_ready: '相同数据准备已经完成',
  dependency_install_failed: '运行依赖安装失败',
  python_dependencies_not_prepared: '运行依赖尚未准备，请按错误详情中的命令完成环境准备',
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

const RECONSTRUCTABLE_BLEND_GROUPS = ['building', 'prop', 'vegetation', 'terrain', 'road', 'unknown', 'water', 'effects', 'particle', 'lighting']


type LaunchState = 'idle' | 'running' | 'completed' | 'cancelled' | 'failed'

const isStringRecord = (value: unknown): value is Record<string, string> =>
  isJsonObject(value) && Object.values(value).every((item) => typeof item === 'string')

const isFirstRunErrorValue = (value: unknown): value is FirstRunError =>
  isJsonObject(value) &&
  typeof value.code === 'string' &&
  typeof value.message === 'string' &&
  typeof value.retryable === 'boolean' &&
  (value.details === undefined || isJsonObject(value.details))

const isFirstRunStatus = (value: unknown): value is FirstRunStatus => {
  if (!isJsonObject(value)) return false
  const states = ['idle', 'queued', 'running', 'partial', 'failed', 'cancelled', 'completed']
  const actions = value.actions
  const capabilities = value.capabilities
  if (
    value.format !== 'EndfieldWebUIFirstRunStatus/1' ||
    typeof value.state !== 'string' || !states.includes(value.state) ||
    typeof value.phase !== 'string' || typeof value.progress !== 'number' ||
    value.progress_scope !== 'phase' ||
    (value.phase_index !== null && typeof value.phase_index !== 'number') ||
    typeof value.phase_count !== 'number' || typeof value.message !== 'string' ||
    typeof value.ready !== 'boolean' || typeof value.cancel_requested !== 'boolean' ||
    typeof value.resume_available !== 'boolean' || !isJsonObject(actions) ||
    typeof actions.can_validate !== 'boolean' || typeof actions.can_start !== 'boolean' ||
    typeof actions.can_resume !== 'boolean' || typeof actions.can_cancel !== 'boolean' ||
    !isJsonObject(value.defaults) || !isJsonObject(capabilities) ||
    typeof capabilities.map_dataset !== 'string' ||
    !isStringRecord(capabilities.asset_resolution) ||
    typeof capabilities.blender_build !== 'string' ||
    typeof capabilities.profile_scan !== 'string'
  ) return false
  if (value.request !== undefined && (
    !isJsonObject(value.request) ||
    value.request.format !== 'EndfieldWebUIFirstRunRequest/1' ||
    typeof value.request.game_root !== 'string' ||
    typeof value.request.export_root !== 'string' ||
    typeof value.request.cache_root !== 'string' ||
    typeof value.request.run_name !== 'string' ||
    (value.request.blender_exe !== undefined && typeof value.request.blender_exe !== 'string')
  )) return false
  if (value.request_identity !== undefined && (
    !isJsonObject(value.request_identity) ||
    typeof value.request_identity.run_name !== 'string' ||
    typeof value.request_identity.sha256 !== 'string'
  )) return false
  if (value.details !== undefined && !isJsonObject(value.details)) return false
  if (value.result !== undefined && !isJsonObject(value.result)) return false
  if (value.error !== undefined && value.error !== null && !isFirstRunErrorValue(value.error)) return false
  if (value.disposition !== undefined && value.disposition !== 'already_ready') return false
  return true
}

const isFirstRunValidation = (value: unknown): value is FirstRunValidation => {
  if (!isJsonObject(value) || value.format !== 'EndfieldWebUIFirstRunValidation/1') return false
  if (
    (value.status !== 'valid' && value.status !== 'incomplete') ||
    value.game_read_only !== true || !isJsonObject(value.checks) ||
    !Object.values(value.checks).every((item) => typeof item === 'boolean') ||
    !Array.isArray(value.missing) || !value.missing.every((item) => typeof item === 'string') ||
    !isJsonObject(value.canonical) ||
    value.canonical.format !== 'EndfieldWebUIFirstRunRequest/1' ||
    typeof value.canonical.game_root !== 'string' ||
    typeof value.canonical.export_root !== 'string' ||
    typeof value.canonical.cache_root !== 'string' ||
    typeof value.canonical.run_name !== 'string' ||
    !isJsonObject(value.space) || !isJsonObject(value.prerequisites) ||
    typeof value.space_warning !== 'boolean'
  ) return false
  for (const key of ['cache', 'export'] as const) {
    const space = value.space[key]
    if (
      !isJsonObject(space) ||
      (space.status !== 'baseline_estimate' && space.status !== 'unknown') ||
      typeof space.path !== 'string' || typeof space.required_bytes !== 'number' ||
      (space.free_bytes !== null && typeof space.free_bytes !== 'number') ||
      typeof space.warning !== 'boolean'
    ) return false
  }
  return typeof value.prerequisites.git_available === 'boolean' &&
    typeof value.prerequisites.dotnet_available === 'boolean' &&
    typeof value.prerequisites.network_required_for_initial_helper_checkout === 'boolean' &&
    typeof value.prerequisites.helper_commit_is_pinned === 'boolean'
}

const isFirstRunEventsResponse = (value: unknown): value is { events?: FirstRunEvent[]; next_after?: number } =>
  isJsonObject(value) &&
  (value.events === undefined || Array.isArray(value.events) && value.events.every((event) =>
    isJsonObject(event) && typeof event.sequence === 'number' && typeof event.phase === 'string' &&
    typeof event.progress === 'number' && event.progress_scope === 'phase' &&
    (event.phase_index === null || typeof event.phase_index === 'number') &&
    typeof event.phase_count === 'number' &&
    (event.message === undefined || typeof event.message === 'string')
  )) &&
  (value.next_after === undefined || typeof value.next_after === 'number')

const isPathPickerResponse = (value: unknown): value is PathPickerResponse =>
  isJsonObject(value) &&
  value.format === 'EndfieldPathPickerResponse/1' &&
  (value.status === 'selected' || value.status === 'cancelled' || value.status === 'unavailable') &&
  (value.kind === undefined || value.kind === 'game' || value.kind === 'output' || value.kind === 'cache' || value.kind === 'blender') &&
  (value.path === undefined || typeof value.path === 'string') &&
  (value.valid === undefined || typeof value.valid === 'boolean') &&
  (value.writable === undefined || typeof value.writable === 'boolean') &&
  (value.writableParent === undefined || typeof value.writableParent === 'boolean') &&
  (value.error === undefined || typeof value.error === 'string')

const firstRunApiErrorFrom = (error: unknown): FirstRunApiError | null => {
  if (!(error instanceof BackendRequestError) || error.responseBody === null) return null
  let payload: unknown
  try {
    payload = JSON.parse(error.responseBody) as unknown
  } catch {
    return null
  }
  if (!isJsonObject(payload)) return null
  const apiError: FirstRunApiError = {}
  if (payload.format === 'EndfieldWebUIFirstRunError/1') apiError.format = payload.format
  if (isFirstRunErrorValue(payload.error)) apiError.error = payload.error
  if (isFirstRunStatus(payload.firstRun)) apiError.firstRun = payload.firstRun
  return apiError.format || apiError.error || apiError.firstRun ? apiError : null
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
        <input id={id} data-testid={`path-${id}`} value={value} readOnly />
        <button type="button" data-testid={`path-picker-${id}`} onClick={onChoose} aria-label={`选择${label}`} disabled={busy}>
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
          <a href="https://github.com/An1X3R/Endfield-map-extractor/blob/main/webui/CONTENT-LICENSE.md" target="_blank" rel="noreferrer">
            MIT License · 原创内容许可范围
          </a>
          <span>仅适用于项目自身拥有权利的内容</span>
        </div>
      </ViewTransition>
    </motion.section>
  )
}

export default function App() {
  const reduceMotion = useReducedMotion()
  const [mapId, setMapId] = useState<MapId>('map01')
  const [workspaceMode, setWorkspaceMode] = useState<'maps' | 'instanced' | 'operators' | 'painting'>(() => new URLSearchParams(window.location.search).get('painting-preview') === '1' ? 'painting' : 'maps')
  const [workspaceTransition, setWorkspaceTransition] = useState<{ phase: 'retract' | 'deploy'; target: 'maps' | 'instanced' | 'operators' | 'painting' } | null>(null)
  const [workspaceTransitionTitle, setWorkspaceTransitionTitle] = useState<string | null>(null)
  const [instancedWorkspaceHeight, setInstancedWorkspaceHeight] = useState(0)
  const [paintingBusy, setPaintingBusy] = useState(false)
  const [mapHandoffBusy, setMapHandoffBusy] = useState(false)
  const [paintingEntryBusy, setPaintingEntryBusy] = useState(false)
  const mapNavigationBusy = mapHandoffBusy || paintingEntryBusy
  const [paintingPreviousMap, setPaintingPreviousMap] = useState<CanvasMapSnapshot | null>(null)
  const [paintingExitSnapshot, setPaintingExitSnapshot] = useState<SVGSVGElement | null>(null)
  const [paintingResetRevision, setPaintingResetRevision] = useState(0)
  const [paintingPreviewRevision, setPaintingPreviewRevision] = useState(0)
  const [instancedJobBusy, setInstancedJobBusy] = useState(false)
  const [instancedCatalogCount, setInstancedCatalogCount] = useState<number | null>(null)
  const [instancedDevelopmentPreview, setInstancedDevelopmentPreview] = useState<InstancedSceneDevelopmentPreview | null>(null)
  const [operatorDevelopmentPreview, setOperatorDevelopmentPreview] = useState<OperatorDevelopmentPreview | null>(null)
  const [operatorCatalogCount, setOperatorCatalogCount] = useState<number | null>(null)
  const [operatorTask, setOperatorTask] = useState<string | null>(null)
  const [operatorResetRevision, setOperatorResetRevision] = useState(0)
  const [operatorTaskReturnRevision, setOperatorTaskReturnRevision] = useState(0)
  const [operatorJobBusy, setOperatorJobBusy] = useState(false)
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
  const [devCompletionPreview, setDevCompletionPreview] = useState<{
    mapId: MapId
    Component: ComponentType<{ mapId: MapId; onFinished: () => void }>
  } | null>(null)
  const [completedExportMode, setCompletedExportMode] = useState<ExportMode>('blend')
  const [completedBlenderBuild, setCompletedBlenderBuild] = useState<BlenderBuildResult | null>(null)
  const [completedOutputDirectory, setCompletedOutputDirectory] = useState<string | null>(null)
  const [progress, setProgress] = useState(0)
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [jobMessage, setJobMessage] = useState('')
  const [jobError, setJobError] = useState('')
  const [armReveal, setArmReveal] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  const [overviewSource, setOverviewSource] = useState<'loading' | 'dev-override' | 'runtime-config' | 'fallback'>('loading')
  const [transitionDirection, setTransitionDirection] = useState<'forward' | 'back'>('forward')
  const DevCompletionPreview = devCompletionPreview?.Component
  const workspaceRef = useRef<HTMLElement | null>(null)
  const topbarRef = useRef<HTMLElement | null>(null)
  const workspaceTransitionTimers = useRef<number[]>([])
  const jobTimer = useRef<number | null>(null)
  const toastTimer = useRef<number | null>(null)
  const firstRunPollTimer = useRef<number | null>(null)
  const firstRunEventCursor = useRef(0)
  const exportEventCursor = useRef(0)
  const exportPollGeneration = useRef(0)
  const exportPollRef = useRef<((jobId: string, after: number, generation: number) => void) | null>(null)
  const exportConnectionLost = useRef(false)
  const exportCancelling = useRef(false)
  const [exportCancelRequested, setExportCancelRequested] = useState(false)
  const firstRunHydrated = useRef(false)
  const scenePreviewInitialized = useRef(false)
  const operatorPreviewInitialized = useRef(false)
  const previousReady = useRef(false)
  const launchStateRef = useRef(launchState)
  const panelDrag = useDraggablePanel(workspaceRef, launchState === 'running')
  const map = MAPS[mapId]
  const preparedRequest = firstRunStatus?.request
  const preparedPathsMatch = Boolean(
    preparedRequest &&
      preparedRequest.game_root === sourcePaths.game &&
      preparedRequest.export_root === sourcePaths.output &&
      preparedRequest.cache_root === sourcePaths.cache,
  )
  const sourceReady = Boolean(firstRunStatus?.ready)
  const paintingRuntime = usePaintingRuntime(sourcePaths, sourceReady && preparedPathsMatch, import.meta.env.DEV && new URLSearchParams(window.location.search).get('painting-preview') === '1')
  const instancedSource = useMemo<InstancedSceneSourcePaths>(() => ({
    game_root: sourcePaths.game,
    cache_root: sourcePaths.cache,
    blender_exe: sourcePaths.blender,
  }), [sourcePaths.game, sourcePaths.cache, sourcePaths.blender])
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

  useLayoutEffect(() => {
    launchStateRef.current = launchState
  }, [launchState])

  const handleOverviewSourceChange = useCallback((source: 'loading' | 'dev-override' | 'runtime-config' | 'fallback') => {
    setOverviewSource(source)
  }, [])

  const notify = useCallback((message: string) => {
    setToast(message)
    if (toastTimer.current) window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 2100)
  }, [])

  const handleInstancedBusyChange = useCallback((busy: boolean) => {
    setInstancedJobBusy(busy)
    if (busy) setWorkspaceMode('instanced')
  }, [])

  useLayoutEffect(() => {
    const updateHeight = (): void => {
      const workspace = workspaceRef.current
      if (!workspace) return
      const documentTop = workspace.getBoundingClientRect().top + window.scrollY
      setInstancedWorkspaceHeight(Math.max(340, window.innerHeight - documentTop - 40))
    }
    updateHeight()
    const observer = new ResizeObserver(updateHeight)
    if (topbarRef.current) observer.observe(topbarRef.current)
    window.addEventListener('resize', updateHeight)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', updateHeight)
    }
  }, [])

  useEffect(() => () => {
    workspaceTransitionTimers.current.forEach((timer) => window.clearTimeout(timer))
  }, [])

  const switchWorkspace = (target: 'maps' | 'instanced' | 'operators' | 'painting', mapTitle: string): void => {
    if (target === workspaceMode || workspaceTransition || mapNavigationBusy) return
    if (launchState === 'running' || instancedJobBusy || operatorJobBusy || paintingBusy) return
    if (target === 'painting') {
      const canvas = workspaceMode === 'maps' ? workspaceRef.current?.querySelector('.pixi-host canvas') : null
      setPaintingPreviousMap(canvas instanceof HTMLCanvasElement ? captureCanvasMap(canvas) : null)
    }
    if (workspaceMode === 'painting' && target === 'maps') {
      const stage = workspaceRef.current?.querySelector<HTMLElement>('.si-map-stage')
      if (!stage) throw new Error('Cannot leave painting: map stage is missing')
      setPaintingExitSnapshot(capturePaintingMap(stage))
    }
    if (target === 'operators' || workspaceMode === 'operators' ||
      (target === 'painting' && workspaceMode === 'maps') || (workspaceMode === 'painting' && target === 'maps')) {
      setWorkspaceMode(target)
      return
    }
    if (reduceMotion) {
      setWorkspaceMode(target)
      return
    }
    setWorkspaceTransition({ phase: 'retract', target })
    const finalTitle = target === 'instanced' ? 'INSTANCED\nSCENES' : target === 'painting' ? '' : mapTitle
    const matrix = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/+'
    const timers = [0, 30, 60, 90, 120].map((elapsed) => window.setTimeout(() => {
      const locked = Math.floor(finalTitle.length * elapsed / 150)
      setWorkspaceTransitionTitle(Array.from(finalTitle, (character, index) =>
        character === '\n' || character === ' ' || index < locked
          ? character
          : matrix[(index * 7 + elapsed / 30) % matrix.length],
      ).join(''))
    }, 150 + elapsed))
    timers.push(window.setTimeout(() => {
      setWorkspaceMode(target)
      setWorkspaceTransition({ phase: 'deploy', target })
    }, 240))
    timers.push(window.setTimeout(() => setWorkspaceTransitionTitle(null), 360))
    timers.push(window.setTimeout(() => {
      setWorkspaceTransition(null)
      workspaceTransitionTimers.current = []
    }, 600))
    workspaceTransitionTimers.current = timers
  }

  const loadInstancedDevelopmentPreview = useCallback(async (): Promise<void> => {
    if (!import.meta.env.DEV) throw new Error('Instanced scene fixtures are only available in development.')
    try {
      const { createInstancedSceneDevelopmentPreview } = await import('./features/instanced-scenes/developmentPreview')
      setInstancedDevelopmentPreview(createInstancedSceneDevelopmentPreview())
      setWorkspaceMode('instanced')
    } catch (error) {
      notify(`独立场景开发预览加载失败：${error instanceof Error ? error.message : String(error)}`)
    }
  }, [notify])

  useEffect(() => {
    if (import.meta.env.DEV && !scenePreviewInitialized.current && new URLSearchParams(window.location.search).get('scene-preview') === '1') {
      scenePreviewInitialized.current = true
      void loadInstancedDevelopmentPreview()
    }
  }, [loadInstancedDevelopmentPreview])

  const loadOperatorDevelopmentPreview = useCallback(async (): Promise<void> => {
    if (!import.meta.env.DEV) throw new Error('Operator fixtures are only available in development.')
    if (operatorJobBusy) { notify('角色任务运行中，请等待后端终态后再进入开发预览。'); return }
    try {
      const { createOperatorDevelopmentPreview } = await import('./features/operators/developmentPreview')
      setOperatorDevelopmentPreview(createOperatorDevelopmentPreview())
      setWorkspaceMode('operators')
    } catch (error) {
      notify(`角色开发预览加载失败：${error instanceof Error ? error.message : String(error)}`)
    }
  }, [notify, operatorJobBusy])

  useEffect(() => {
    if (import.meta.env.DEV && !operatorPreviewInitialized.current && new URLSearchParams(window.location.search).get('operator-preview') === '1') {
      operatorPreviewInitialized.current = true
      void loadOperatorDevelopmentPreview()
    }
  }, [loadOperatorDevelopmentPreview])

  const hydrateFirstRunPaths = useCallback((status: FirstRunStatus) => {
    if (firstRunHydrated.current) return
    firstRunHydrated.current = true
    const paths = status.defaults
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
      const payload = await requestJson('/api/v2/first-run', { method: 'GET' }, isFirstRunStatus)
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
    const payload = await requestJson(
      `/api/v2/first-run/events?after=${firstRunEventCursor.current}&limit=100`,
      { method: 'GET' },
      isFirstRunEventsResponse,
    )
    const events = payload.events ?? []
    if (events.length) {
      setFirstRunEvents((current) => [...current, ...events].slice(-24))
    }
    firstRunEventCursor.current = payload.next_after ?? events.at(-1)?.sequence ?? firstRunEventCursor.current
  }, [])

  const stopJob = useCallback(() => {
    exportPollGeneration.current += 1
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
      try {
        await Promise.all([loadFirstRunStatus(true), loadFirstRunEvents()])
      } catch (error) {
        setFirstRunError(error instanceof Error ? error.message : String(error))
      }
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
    if (launchStateRef.current === 'running') return
    setSelection(next)
    setLaunchState('idle')
    setProgress(0)
  }, [])

  const handleMapChange = (nextMap: MapId) => {
    if (mapNavigationBusy) return
    if (instancedJobBusy) {
      notify('独立场景任务仍在运行；请先请求安全取消')
      return
    }
    switchWorkspace('maps', MAPS[nextMap].title)
    if (nextMap === mapId) return
    if (launchState === 'running') {
      notify('任务运行中不能切换地图；请先请求安全取消')
      return
    }
    stopJob()
    setTransitionDirection(nextMap === 'map02' ? 'forward' : 'back')
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
      const payload = await postJson('/api/v2/path-picker', { kind, current: sourcePaths[kind] }, isPathPickerResponse)
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
      const payload = await postJson('/api/v2/first-run/validate', firstRunRequest, isFirstRunValidation)
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
      const apiError = firstRunApiErrorFrom(error)
      if (apiError?.firstRun) setFirstRunStatus(apiError.firstRun)
      const message = apiError
        ? firstRunErrorMessage(apiError, '首次运行校验失败')
        : error instanceof Error ? error.message : String(error)
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
      const payload = await postJson('/api/v2/first-run', firstRunRequest, isFirstRunStatus)
      setFirstRunStatus(payload)
      if (payload.disposition === 'already_ready' || payload.ready) {
        notify('相同数据请求已经准备完成，地图导出已解锁')
      } else {
        notify(resume ? '继续准备请求已提交；将复用已保留的阶段结果' : '首次数据准备已排队；游戏目录始终保持只读')
      }
    } catch (error) {
      const apiError = firstRunApiErrorFrom(error)
      if (apiError?.firstRun) setFirstRunStatus(apiError.firstRun)
      const message = apiError
        ? firstRunErrorMessage(apiError, '无法启动首次数据准备')
        : error instanceof Error ? error.message : String(error)
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
      const payload = await postJson('/api/v2/first-run/cancel', {}, isFirstRunStatus)
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
    notify(firstRunStatus.ready ? '已载入后端已准备目录，无需重新提取必要文件' : '已载入可恢复任务的原始目录，请确认后继续准备')
  }

  const resetPrototype = () => {
    if (launchState === 'running' || instancedJobBusy) {
      notify('任务运行中不能复位；请先请求安全取消')
      return
    }
    stopJob()
    startTransition(() => {
      setWorkspaceMode('maps')
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
      return '当前分组无法生成场景，请选择可重建的导出分组'
    }
    if (!outputLabel.trim()) return '请填写输出标签'
    return '所有前置条件已完成'
  }, [allGroupsSelected, exportMode, firstRunAction, firstRunRunning, firstRunStatus, outputLabel, preparedPathsMatch, selectedGroups, selection, sourcePaths.blender, sourceReady])

  const reconstructableSelection = allGroupsSelected || selectedGroups.some((group) => RECONSTRUCTABLE_BLEND_GROUPS.includes(group))

  const isReady = Boolean(
    sourceReady &&
      preparedPathsMatch && !firstRunRunning && !firstRunAction &&
      selection &&
      Object.values(selection.canonicalBounds).every(Number.isFinite) && Object.values(layers).some(Boolean) &&
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
    if (!isReady || !selection || launchStateRef.current === 'running') return
    launchStateRef.current = 'running'
    exportCancelling.current = false
    setExportCancelRequested(false)
    setActiveJobId(null)
    stopJob()
    const generation = exportPollGeneration.current
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
      source: { game_root: sourcePaths.game, ...(sourcePaths.blender.trim() ? { blender_exe: sourcePaths.blender.trim() } : {}) },
      output: { root: sourcePaths.output, label: outputLabel },
    }

    const poll = (jobId: string, after: number, pollGeneration: number) => {
      if (pollGeneration !== exportPollGeneration.current) return
      const tick = async () => {
        try {
          const [job, eventPayload] = await Promise.all([
            requestJson(`/api/v2/export-jobs/${encodeURIComponent(jobId)}`, { method: 'GET' }, isExportJob),
            requestJson(
              `/api/v2/export-jobs/${encodeURIComponent(jobId)}/events?after=${after}`,
              { method: 'GET' },
              isExportJobEventsResponse,
            ),
          ])
          if (pollGeneration !== exportPollGeneration.current) return
          if (job.job_id !== jobId) throw new Error(`任务标识不匹配：请求 ${jobId}，返回 ${job.job_id}`)
          const events = eventPayload.events ?? []
          const newest = events.at(-1)
          const nextAfter = newest?.sequence ?? after
          exportEventCursor.current = nextAfter
          if (newest?.message) setJobMessage(newest.message)
          if (exportConnectionLost.current) {
            exportConnectionLost.current = false
            setJobError('')
            setJobMessage(newest?.message ?? '任务状态连接已恢复')
            notify('任务状态已重新连接')
          }
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
            setCompletedOutputDirectory(job.result?.package?.output_dir ?? null)
            setLaunchState('completed')
            setActiveJobId(null)
            exportPollRef.current = null
            exportConnectionLost.current = false
            const pendingCount = build?.pending ?? build?.scenes?.reduce((total, scene) => total + scene.pending, 0) ?? 0
            const hasSceneReport = Boolean(build && Array.isArray(build.scenes))
            const partialBuild = build?.status === 'partial' || pendingCount > 0
            notify(resultMode === 'blend'
              ? !hasSceneReport
                ? '导出完成，但未返回场景报告/路径'
                : partialBuild
                  ? `导出完成，有 ${pendingCount} 项待处理，详见报告`
                  : `导出完成：${build?.scenes?.length ?? 0} 批场景文件已返回`
              : `导出完成：数据包状态 ${job.result?.package?.status ?? 'completed'}`)
            return
          }
          if (job.state === 'cancelled') {
            stopJob()
            setLaunchState('cancelled')
            setActiveJobId(null)
            exportPollRef.current = null
            exportConnectionLost.current = false
            notify('任务已在阶段边界取消；已有日志和部分产物均已保留')
            return
          }
          if (job.state === 'failed') {
            stopJob()
            const message = job.error?.message ?? '未知后端错误'
            setJobError(message)
            setLaunchState('failed')
            setActiveJobId(null)
            exportPollRef.current = null
            exportConnectionLost.current = false
            notify(`导出失败：${message}`)
            return
          }
          jobTimer.current = window.setTimeout(() => poll(jobId, nextAfter, pollGeneration), 500)
        } catch (error) {
          if (pollGeneration !== exportPollGeneration.current) return
          const message = error instanceof Error ? error.message : String(error)
          setJobError(message)
          setJobMessage('状态连接中断；后台状态暂不可确认，可重连或请求取消')
          if (!exportConnectionLost.current) notify(`任务状态暂不可确认：${message}`)
          exportConnectionLost.current = true
          jobTimer.current = window.setTimeout(() => poll(jobId, exportEventCursor.current, pollGeneration), 1500)
        }
      }
      void tick()
    }
    exportPollRef.current = poll

    void (async () => {
      try {
        const validation = await postJson('/api/v2/export-jobs/validate', request, isExportJobValidation)
        if (generation !== exportPollGeneration.current) return
        if (validation.status !== 'valid') {
          throw new Error(validation.message ?? validation.error?.message ?? 'export_request_validation_failed')
        }
        const payload = await postJson('/api/v2/export-jobs', request, isExportJob)
        if (generation !== exportPollGeneration.current) return
        setActiveJobId(payload.job_id)
        exportEventCursor.current = 0
        exportConnectionLost.current = false
        notify(exportMode === 'blend' ? 'Blender 场景构建任务已提交' : '数据包导出任务已提交')
        poll(payload.job_id, 0, generation)
      } catch (error) {
        if (generation !== exportPollGeneration.current) return
        const apiError = firstRunApiErrorFrom(error)
        if (apiError?.firstRun) setFirstRunStatus(apiError.firstRun)
        if (apiError?.error?.code === 'first_run_required') {
          setDrawerCollapsed(false)
          setFirstRunError(firstRunErrorMessage(apiError, '需要先完成首次运行数据准备'))
        }
        const message = apiError?.error
          ? firstRunErrorMessage(apiError, 'export_job_submit_failed')
          : error instanceof Error ? error.message : String(error)
        setJobError(message)
        setLaunchState('failed')
        notify(`无法启动提取：${message}`)
      }
    })()
  }

  const cancelVisualTask = async () => {
    if (launchState !== 'running' || !activeJobId || exportCancelling.current) return
    exportCancelling.current = true
    setExportCancelRequested(true)
    try {
      const response = await postJson(`/api/v2/export-jobs/${encodeURIComponent(activeJobId)}/cancel`, {}, isExportJob)
      if (response.job_id !== activeJobId) throw new Error(`取消响应任务标识不匹配：请求 ${activeJobId}，返回 ${response.job_id}`)
      setJobMessage('已请求协作取消；等待当前安全阶段结束')
      notify('取消请求已提交；已经写入的结果会保留')
    } catch (error) {
      exportCancelling.current = false
      setExportCancelRequested(false)
      notify(`取消请求失败，后台状态需重新确认：${error instanceof Error ? error.message : String(error)}`)
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
      source: { game_root: sourcePaths.game, ...(sourcePaths.blender.trim() ? { blender_exe: sourcePaths.blender.trim() } : {}) },
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
      canStartFirstRunRequest(firstRunStatus, firstRunRequest) &&
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
  const helperProgress = firstRunStatus?.details?.helperProgress
  const helperCountLabel = helperProgress && (helperProgress.current !== undefined || helperProgress.total !== undefined)
    ? `${helperProgress.current ?? '—'} / ${helperProgress.total ?? '—'}`
    : null
  const helperMetrics = helperProgress
    ? [
        Number.isFinite(helperProgress.elapsedSeconds) ? `耗时 ${helperProgress.elapsedSeconds?.toFixed(1)} s` : null,
        Number.isFinite(helperProgress.peakWorkingSetBytes) ? `峰值工作集 ${formatBytes(helperProgress.peakWorkingSetBytes ?? 0)}` : null,
        Number.isFinite(helperProgress.readBytes) ? `逻辑读入 ${formatBytes(helperProgress.readBytes ?? 0)}` : null,
        Number.isFinite(helperProgress.writeBytes) ? `逻辑写出 ${formatBytes(helperProgress.writeBytes ?? 0)}` : null,
      ].filter((metric): metric is string => metric !== null)
    : []
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
          ? '导出完成 · 有待处理项'
          : completedExportMode === 'blend'
            ? '导出完成 · 场景已校验'
            : '导出完成 · 数据包已审计'
        : launchState === 'failed'
          ? '导出失败 · 检查提示'
          : launchState === 'cancelled'
            ? '任务已安全取消'
        : isReady
          ? exportMode === 'blend' ? '构建 Blender 场景' : '开始导出数据包'
          : '等待设置完成'

  return (
    <div className={`app-shell theme-${workspaceMode === 'painting' ? 'si' : mapId} mode-${workspaceMode}`} data-map-theme={workspaceMode === 'painting' ? 'si' : mapId} data-workspace-mode={workspaceMode}>
      <div className="ambient-grid" aria-hidden="true" />
      {workspaceMode !== 'painting' && <AmbientContours />}

      <header
        ref={topbarRef}
        className="topbar"
        data-active-map={workspaceMode === 'painting' ? '03 / SI' : mapId === 'map01' ? 'M01 / IV' : 'M02 / WL'}
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
          <nav className="map-switch map-switch-with-instanced map-switch-with-operators map-switch-with-painting" aria-label="选择地图、独立场景或角色档案">
            {(Object.keys(MAPS) as MapId[]).map((id) => {
              const item = MAPS[id]
              const active = workspaceMode === 'maps' && id === mapId
              return (
                <button
                  key={id}
                  type="button"
                  data-testid={`map-tab-${id}`}
                  className={active ? 'active' : ''}
                  aria-pressed={active}
                  disabled={launchState === 'running' || instancedJobBusy || paintingBusy || mapNavigationBusy || workspaceTransition !== null}
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
            <button type="button" className={`painting-navigation-tab ${workspaceMode === 'painting' ? 'active' : ''}`} data-testid="painting-tab" aria-pressed={workspaceMode === 'painting'} disabled={launchState === 'running' || instancedJobBusy || paintingBusy || mapNavigationBusy || workspaceTransition !== null} onClick={() => switchWorkspace('painting', 'SI’S PAINTING')}>
              <span className="painting-navigation-icon" aria-hidden="true"><i /><i /><i /></span><span className="map-switch-copy"><strong>SI’S PAINTING</strong><small>祀的画 / 画境</small></span><i aria-hidden="true">→</i>
            </button>
            <span className="instanced-navigation-datum" aria-hidden="true"><b>//</b><small>IDX // DISCRETE</small></span>
            <button
              type="button"
              data-testid="instanced-tab"
              className={`instanced-navigation-tab ${workspaceMode === 'instanced' ? 'active' : ''}`}
              aria-pressed={workspaceMode === 'instanced'}
              disabled={launchState === 'running' || instancedJobBusy || paintingBusy || mapNavigationBusy || workspaceTransition !== null}
              onClick={() => switchWorkspace('instanced', map.title)}
            >
              <span className="instanced-navigation-icon" aria-hidden="true"><i /><i /><i /><i /></span>
              <span className="map-switch-copy"><strong>INSTANCED</strong><small>独立场景 / {instancedCatalogCount ?? '—'}</small></span>
              <i aria-hidden="true">→</i>
            </button>
            <span className="instanced-navigation-datum operator-navigation-datum" aria-hidden="true"><b>//</b><small>CAT // ENTITY</small></span>
            <button type="button" data-testid="operators-tab" className={`operator-navigation-tab ${workspaceMode === 'operators' ? 'active' : ''}`} aria-pressed={workspaceMode === 'operators'} disabled={launchState === 'running' || instancedJobBusy || paintingBusy || mapNavigationBusy || workspaceTransition !== null} onClick={() => switchWorkspace('operators', map.title)}>
              <svg className="operator-navigation-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="7" r="4" /><path d="M4 22v-3a8 8 0 0 1 16 0v3M4 22h16" /></svg>
              <span className="map-switch-copy"><strong>OPERATORS</strong><small>角色档案 / {operatorCatalogCount ?? '--'}</small></span><i aria-hidden="true">→</i>
            </button>
          </nav>
        </div>

          <div className="top-actions">
          {operatorTask && <button type="button" className="operator-task-pill" data-testid="operator-task-pill" onClick={() => { setWorkspaceMode('operators'); setOperatorTaskReturnRevision(value => value + 1) }}>{operatorTask}</button>}
          <div className={`top-status ${sourceReady ? 'ready' : ''}`}>
              <i />
              <span>{sourceReady ? 'DATA / READY' : firstRunRunning ? 'DATA / PREPARING' : 'FIRST RUN / REQUIRED'}</span>
            </div>
          {import.meta.env.DEV && workspaceMode === 'painting' && <button type="button" className="dev-review-button" data-testid="si-preview-entry" onClick={() => setPaintingPreviewRevision(value => value + 1)}><Play size={14} /><span>完成动画</span></button>}
          {import.meta.env.DEV && workspaceMode === 'maps' && (
            <button
              className="dev-review-button"
              type="button"
              data-testid="completion-review-entry"
              aria-label="预览完成动画（开发者样例）"
              title="开发者样例，不执行导出"
              onClick={() => {
                const selectedMap = mapId
                void import('./components/completion/CompletionReview').then(({ CompletionPreview }) => {
                  setDevCompletionPreview({ mapId: selectedMap, Component: CompletionPreview })
                }).catch((error: unknown) => {
                  notify(`完成动画预览加载失败：${error instanceof Error ? error.message : String(error)}`)
                })
              }}
            >
              <Play size={14} /><span>完成动画</span>
            </button>
          )}
          {import.meta.env.DEV && workspaceMode === 'instanced' && (
            <button className="dev-review-button" type="button" data-testid="instanced-preview-entry" disabled={instancedJobBusy} onClick={() => void loadInstancedDevelopmentPreview()} title="开发样例，不执行提取"><Play size={14} /><span>开发预览</span></button>
          )}
          {import.meta.env.DEV && workspaceMode === 'operators' && <button type="button" className="dev-review-button" data-testid="operator-preview-entry" disabled={operatorJobBusy} onClick={() => void loadOperatorDevelopmentPreview()} title="开发样例，不执行提取"><Play size={14} /><span>开发预览</span></button>}
          {import.meta.env.DEV && workspaceMode === 'operators' && operatorDevelopmentPreview && <button type="button" data-testid="operator-live-entry" onClick={() => setOperatorDevelopmentPreview(null)}>返回实时目录</button>}
          <button type="button" onClick={copyRequest} hidden={workspaceMode !== 'maps'} aria-label="复制请求摘要"><Clipboard size={14} /><span>复制摘要</span></button>
          <button type="button" data-testid="reset-app" disabled={workspaceTransition !== null || paintingBusy} onClick={() => workspaceMode === 'painting' ? setPaintingResetRevision(value => value + 1) : workspaceMode === 'operators' ? setOperatorResetRevision(value => value + 1) : resetPrototype()} aria-label="复位页面"><RefreshCw size={14} /><span>复位</span></button>
        </div>
      </header>

      <main
        className={`workspace ${workspaceTransition ? `workspace-transition-${workspaceTransition.phase}` : ''} ${workspaceTransitionTitle ? 'workspace-title-rebuilding' : ''}`}
        ref={workspaceRef}
        data-transition-to={workspaceTransition?.target}
        data-testid="workspace-surface"
        inert={workspaceTransition !== null}
        style={{ '--instanced-workspace-height': `${instancedWorkspaceHeight}px` } as CSSProperties}
      >
        {workspaceTransition && <div className="workspace-transition-datum" aria-hidden="true" />}
        {workspaceTransitionTitle && <div className="workspace-transition-title" aria-hidden="true">{workspaceTransitionTitle}<span>{workspaceTransition?.target === 'instanced' ? '独立场景' : MAPS[mapId].chineseName}</span></div>}
        <div className="map-workspace-surfaces" hidden={workspaceMode !== 'maps'} inert={workspaceMode !== 'maps' || workspaceTransition !== null}>
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
            <div className={`map-transition-screen map-transition-${transitionDirection}`}>
                <MapViewport
                  incomingPainting={paintingExitSnapshot}
                  onHandoffChange={setMapHandoffBusy}
                  mapId={mapId}
                  layers={layers}
                  chunkMode={chunkMode}
                  selection={selection}
                  overviewRevision={overviewRevision}
                  onSelectionChange={handleSelectionChange}
                  onOverviewSourceChange={handleOverviewSourceChange}
                />
            </div>
            <RegionCartouche mapId={mapId} />
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
          ref={panelDrag.panelRef}
          className={`settings-drawer ${drawerCollapsed ? 'collapsed' : ''} ${panelDrag.isDragging ? 'is-dragging' : ''}`}
          aria-label="提取设置"
          style={{ viewTransitionName: 'persistent-settings' }}
        >
          <header
            ref={panelDrag.headerRef}
            className={`settings-drawer-heading ${panelDrag.isDragging ? 'is-dragging' : ''}`}
            data-testid="settings-drag"
            tabIndex={0}
            aria-label="拖动提取设置面板；方向键移动，Home 归位"
            onPointerDown={panelDrag.onPointerDown}
            onPointerMove={panelDrag.onPointerMove}
            onPointerUp={panelDrag.onPointerUp}
            onPointerCancel={panelDrag.onPointerCancel}
            onLostPointerCapture={panelDrag.onLostPointerCapture}
            onKeyDown={panelDrag.onKeyDown}
          >
            <div><small>Extraction settings</small><strong>提取设置</strong></div>
            <button className="panel-reset" type="button" data-testid="settings-reset" onClick={panelDrag.reset} disabled={launchState === 'running'}>归位</button>
            <button
              type="button"
              data-testid="settings-collapse"
              onClick={() => setDrawerCollapsed((value) => !value)}
              aria-expanded={!drawerCollapsed}
              aria-label={drawerCollapsed ? '展开提取设置' : '折叠提取设置'}
            >
              <ChevronDown size={17} />
            </button>
          </header>

          <div className="settings-scroll">
            <SettingSection key={sourceReady && preparedPathsMatch ? 'first-run-ready' : 'first-run-setup'} title="首次运行准备" code="FIRST RUN / DEPLOY" defaultOpen={!sourceReady || !preparedPathsMatch}>
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
                <p>{firstRunStatus?.message ?? (firstRunError
                  ? '后端尚未连接，请配置服务后重新连接。'
                  : '正在读取本机首次运行状态。首次准备的耗时与空间占用取决于包体、选项和磁盘性能，运行后可查看实际处理进度。')}</p>
                {helperProgress && (
                  <>
                    <div className="first-run-phase" data-testid="first-run-helper-progress">
                      <span>处理阶段 · {FIRST_RUN_HELPER_PHASE_LABELS[helperProgress.phase] ?? helperProgress.phase}</span>
                      {helperCountLabel && <strong>{helperCountLabel} <small>COUNT</small></strong>}
                    </div>
                    {helperMetrics.length > 0 && <p data-testid="first-run-helper-metrics">{helperMetrics.join(' · ')}</p>}
                  </>
                )}
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
                    {firstRunValidation.prerequisites.network_required_for_initial_helper_checkout && <span className={firstRunValidation.prerequisites.git_available ? 'ok' : 'missing'}>GIT / BUILD ONLY</span>}
                    <span className={firstRunValidation.prerequisites.dotnet_available ? 'ok' : 'missing'}>.NET</span>
                    <span className="ok">PINNED HELPER</span>
                    <span>{firstRunValidation.prerequisites.network_required_for_initial_helper_checkout ? 'HELPERS / SETUP REQUIRED' : 'PRECOMPILED / NO SOURCE DOWNLOAD'}</span>
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
                {sourceReady && firstRunStatus?.request && !firstRunRequestMatchesPrepared(firstRunRequest, firstRunStatus.request) && (
                  <button type="button" data-testid="first-run-use-prepared-paths" onClick={restoreFirstRunRequestPaths} disabled={Boolean(firstRunAction) || launchState === 'running' || instancedJobBusy || operatorJobBusy || paintingBusy}>
                    <Folder size={13} /><span>载入已准备目录</span>
                  </button>
                )}
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
                    <span>{sourceReady && firstRunRequestMatchesPrepared(firstRunRequest, firstRunStatus?.request) ? '必要文件已就绪' : '提取必要文件'}</span>
                  </button>
                )}
                {firstRunStatus?.actions.can_cancel && (
                  <button className="danger" type="button" data-testid="cancel-first-run" onClick={() => void cancelFirstRun()} disabled={firstRunAction === 'cancel'}>
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
                  <select data-testid="export-mode" value={exportMode} onChange={(event) => updateExportMode(event.target.value as ExportMode)} disabled={launchState === 'running'}>
                    <option value="blend">Blender 场景 + 数据包（默认）</option>
                    <option value="data_package">仅审计数据包</option>
                  </select>
                  <ChevronDown size={13} />
                </div>
                <small>{exportMode === 'blend'
                  ? '从本机缓存构建并校验 .blend；待处理项包含缺失资源、未支持组件与近似预览，不等于同数量的资产缺失。'
                  : '按所选范围生成审计数据包，不构建 Blender 场景；水体模式仅记录为导出参数。'}</small>
              </label>

              <div className="setting-block">
                <span className="setting-label"><Box size={12} />导出分组 · 全部或独立多选</span>
                <ExportGroupSelector groups={selectedGroups} allSelected={allGroupsSelected} exportMode={exportMode} disabled={launchState === 'running'} testIdPrefix="map" onSelectAll={selectAllGroups} onToggle={toggleGroup} />
              </div>
            </SettingSection>

            <SettingSection title="区块与构建策略" code="CHUNK / FX / WATER">
              <div className="field-pair">
                <label>
                  <span><SquareDashedMousePointer size={12} />区块打包方式</span>
                  <div className="select-shell">
                    <select data-testid="batch-mode" value={chunkMode} onChange={(event) => updateChunkMode(event.target.value as ChunkMode)} disabled={launchState === 'running'}>
                      <option value="per_sector">每 sector 独立</option>
                      <option value="cluster_4x4_sectors">4 × 4 sectors 稳定分组</option>
                      <option value="merged_selection">合并选区</option>
                    </select>
                    <ChevronDown size={13} />
                  </div>
                </label>
                <label>
                  <span><Waves size={12} />水体模式 · {exportMode === 'blend' ? '按源配置自动重建' : '仅记录导出参数'}</span>
                  <div className="select-shell">
                    <select data-testid="water-mode" value={exportMode === 'blend' ? 'automatic' : waterMode} onChange={(event) => setWaterMode(event.target.value)} disabled={exportMode === 'blend' || launchState === 'running'}>
                      {exportMode === 'blend' ? <option value="automatic">自动重建 · Eevee / Cycles</option> : <>
                        <option value="stable_eevee">稳定水面（记录参数）</option>
                        <option value="flowmap_fresnel">流动水面（记录参数）</option>
                      </>}
                    </select>
                    <ChevronDown size={13} />
                  </div>
                  {exportMode === 'blend' && <small>选择水体分组后，按真实水面与配置构建。泡沫、焦散和交互等效果尚未完整还原。</small>}
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
              {exportMode === 'blend' && <div className="full-field" data-testid="export-capability-notes">
                <span>当前效果与限制</span>
                <small>粒子与特效导出固定时刻的静态预览，仅支持部分组件。复杂环绕流带、烟雾等可能缺失或与游戏不同；动态特效仍属实验能力，尚未接入此界面。</small>
                <small>灯光亮度、启停与特殊光源仍有差异；部分扫光缺少游戏运行状态。水体、灯光与材质支持 Eevee / Cycles，但不保证两引擎或游戏画面完全一致。</small>
              </div>}
              <label className="full-field">
                <span>输出标签</span>
                <input data-testid="output-label" value={outputLabel} onChange={(event) => setOutputLabel(event.target.value)} />
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

          <button className="launch-button" type="button" data-testid="export-launch" onClick={launchVisualTask} disabled={!isReady || launchState === 'running'}>
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
              {launchState === 'running' && activeJobId && jobError && (
                <button
                  type="button"
                  data-testid="reconnect-export-job"
                  onClick={() => {
                    const jobId = activeJobId
                    if (!jobId) return
                    stopJob()
                    setJobError('')
                    setJobMessage('正在重新连接任务状态')
                    exportPollRef.current?.(jobId, exportEventCursor.current, exportPollGeneration.current)
                  }}
                >
                  <RefreshCw size={11} />重连
                </button>
              )}
              {launchState === 'running' && <button type="button" data-testid="cancel-export-job" disabled={!activeJobId || exportCancelRequested} onClick={cancelVisualTask}>{exportCancelRequested ? '正在取消' : '取消'}</button>}
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
                    ? '场景由本机缓存构建；未解析资源、未支持组件与近似效果会保留在报告。校验通过不代表完整游戏还原。'
                    : '将按所选分组生成审计数据包；预览图层开关会影响导出内容。'
                  : sourceReady
                    ? '完成地图框选和导出设置后解锁。'
                    : '先在提取设置中完成首次运行数据准备；地图仍可浏览和框选。'}
          </p>
        </section>
        </div>
        <PaintingWorkspace onHandoffChange={setPaintingEntryBusy} previousMap={paintingPreviousMap} active={workspaceMode === 'painting'} runtime={paintingRuntime} onPrepare={() => { switchWorkspace('maps', map.title); setDrawerCollapsed(false) }} resetRevision={paintingResetRevision} previewRevision={paintingPreviewRevision} onBusyChange={setPaintingBusy} />
        <InstancedSceneWorkspace
          active={workspaceMode === 'instanced'}
          firstRunReady={sourceReady && preparedPathsMatch}
          firstRunBusy={firstRunRunning}
          mapJobBusy={launchState === 'running'}
          source={instancedSource}
          outputRoot={sourcePaths.output}
          preparationIdentity={firstRunStatus?.request_identity?.sha256 ?? null}
          onRequestPreparation={() => {
            if (instancedJobBusy || workspaceTransition) return
            switchWorkspace('maps', map.title)
            const focusPreparation = (): void => {
              const panel = panelDrag.headerRef.current
              panel?.focus({ preventScroll: true })
              panelDrag.panelRef.current?.scrollIntoView({ block: 'nearest', behavior: 'instant' })
            }
            if (reduceMotion) window.requestAnimationFrame(focusPreparation)
            else workspaceTransitionTimers.current.push(window.setTimeout(focusPreparation, 380))
          }}
          onBusyChange={handleInstancedBusyChange}
          onCatalogCountChange={setInstancedCatalogCount}
          developmentPreview={instancedDevelopmentPreview}
        />
        <OperatorWorkspace active={workspaceMode === 'operators'} developmentPreview={operatorDevelopmentPreview} resetRevision={operatorResetRevision} taskReturnRevision={operatorTaskReturnRevision} onCatalogCountChange={setOperatorCatalogCount} onTaskChange={setOperatorTask} onBusyChange={setOperatorJobBusy} onNotify={notify} />
      </main>

      <footer className="footer-strip">
        <strong>ENDFIELD / ATLAS</strong>
        <i />
        <span>{workspaceMode === 'painting' ? `SI’S PAINTING · ${paintingRuntime.map ? 'MAP METADATA READY' : 'MAP PREPARATION REQUIRED'} / ${paintingRuntime.sourceReady ? '场景已准备' : '场景准备待完成'}` : workspaceMode === 'operators' ? `OPERATORS · ${operatorDevelopmentPreview ? 'DEV VISUAL PREVIEW' : operatorCatalogCount !== null ? 'CHARACTER CATALOG READY' : 'CHARACTER SETUP REQUIRED'} · ENTITY ARCHIVE / CHARACTER JOBS` : `${workspaceMode === 'instanced' ? 'INSTANCED SCENES' : map.title} · ${sourceReady ? 'FIRST RUN READY' : firstRunRunning ? 'DATA PREPARING' : 'SETUP REQUIRED'} · ${workspaceMode === 'instanced' ? 'DISCRETE SCENE INDEX / SCENE JOBS' : `SECTOR128 RESOLVED · ${exportMode === 'blend' ? 'BLENDER SCENE + DATA PACKAGE' : 'DATA PACKAGE EXPORT'}`}`}</span>
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
        outputDirectory={completedOutputDirectory}
        onFinished={dismissCompletion}
      />
      {import.meta.env.DEV && DevCompletionPreview && devCompletionPreview && (
        <DevCompletionPreview mapId={devCompletionPreview.mapId} onFinished={() => setDevCompletionPreview(null)} />
      )}
    </div>
  )
}
