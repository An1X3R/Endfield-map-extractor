import { useCallback, useEffect, useRef, useState } from 'react'
import { BackendRequestError } from '../../lib/backendClient'
import {
  getInstancedSceneCatalog,
  getInstancedSceneDetail,
  getInstancedSceneJob,
  getInstancedSceneJobEvents,
  getInstancedScenePreview,
  hasSameInstancedSceneRequestSemantics,
  isInstancedSceneDevelopmentPreview,
  isInstancedSceneExportRequest,
  requestInstancedSceneJobCancellation,
  submitInstancedSceneExport,
  validateInstancedSceneExport,
} from './api'
import type {
  InstancedSceneCancelAcknowledgement,
  InstancedSceneCatalog,
  InstancedSceneDiagnostic,
  InstancedSceneDetail,
  InstancedSceneDevelopmentPreview,
  InstancedSceneExportMode,
  InstancedSceneExportRequest,
  InstancedSceneJob,
  InstancedSceneJobEvent,
  InstancedScenePreview,
  InstancedSceneSourcePaths,
} from './types'

export type InstancedScenesConnection = 'idle' | 'loading' | 'ready' | 'unavailable' | 'disconnected'

export type UseInstancedScenesOptions = {
  active: boolean
  firstRunReady: boolean
  firstRunBusy: boolean
  mapJobBusy: boolean
  firstRunIdentity: string | null
  source: InstancedSceneSourcePaths
  developmentPreview: InstancedSceneDevelopmentPreview | null
  onBusyChange: (busy: boolean) => void
  onCatalogCountChange: (count: number | null) => void
}

export type InstancedScenesController = {
  connection: InstancedScenesConnection
  catalog: InstancedSceneCatalog | null
  selectedSceneId: string | null
  detail: InstancedSceneDetail | null
  preview: InstancedScenePreview | null
  detailLoading: boolean
  previewLoading: boolean
  error: string | null
  diagnostic: InstancedSceneDiagnostic | null
  job: InstancedSceneJob | null
  events: InstancedSceneJobEvent[]
  activeJobId: string | null
  operationRequest: InstancedSceneExportRequest | null
  operationPending: boolean
  cancelRequested: boolean
  recoveryNeedsDiscard: boolean
  availableExportModes: InstancedSceneExportMode[]
  canStartExport: boolean
  canCancel: boolean
  selectScene: (sceneId: string) => Promise<void>
  refreshCatalog: () => Promise<void>
  startExport: (request: InstancedSceneExportRequest) => Promise<void>
  cancelExport: () => Promise<void>
  reconnect: () => Promise<void>
  discardStaleRecovery: () => void
}

const isTerminal = (job: InstancedSceneJob): boolean =>
  job.state === 'completed' || job.state === 'partial' || job.state === 'failed' || job.state === 'cancelled'

const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error)

const diagnosticFor = (error: unknown): InstancedSceneDiagnostic => {
  if (error instanceof BackendRequestError) {
    return {
      kind: error.kind,
      method: error.method,
      path: error.path,
      status: error.status,
      responseBody: error.responseBody,
      detail: error.message,
    }
  }
  return {
    kind: 'application',
    method: null,
    path: null,
    status: null,
    responseBody: null,
    detail: errorMessage(error),
  }
}

const userFacingError = (error: unknown): string => {
  if (!(error instanceof BackendRequestError)) return errorMessage(error)
  if (error.kind === 'request') return '无法连接后端，请确认 WebUI 服务仍在运行后重试。'
  if (error.kind === 'http') return `后端接口返回 HTTP ${error.status ?? '未知'}，请确认独立场景接口已部署并检查请求设置。`
  if (error.kind === 'invalid_json') return '后端响应不是有效 JSON，请检查服务端日志。'
  if (error.kind === 'invalid_response') return '后端响应不符合独立场景接口契约，操作已停止。'
  return '请求配置无效，接口操作未执行。'
}

const errorConnection = (error: unknown): 'unavailable' | 'disconnected' =>
  error instanceof BackendRequestError && error.kind === 'request' ? 'disconnected' : 'unavailable'

export const isRecoveryContextReady = (
  options: Pick<UseInstancedScenesOptions, 'firstRunReady' | 'firstRunBusy' | 'firstRunIdentity' | 'source' | 'developmentPreview'>,
  request: InstancedSceneExportRequest | null,
): boolean => options.developmentPreview === null && options.firstRunReady && !options.firstRunBusy &&
  options.firstRunIdentity !== null && options.source.game_root.trim().length > 0 && options.source.cache_root.trim().length > 0 &&
  request !== null && (request.export_mode !== 'blend' || options.source.blender_exe.trim().length > 0)

const mergeEvents = (
  current: InstancedSceneJobEvent[],
  incoming: InstancedSceneJobEvent[],
): InstancedSceneJobEvent[] => {
  const bySequence = new Map<number, InstancedSceneJobEvent>()
  for (const event of current) bySequence.set(event.sequence, event)
  for (const event of incoming) bySequence.set(event.sequence, event)
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence).slice(-80)
}

const validationMessage = (issues: Array<{ code: string; message: string; field: string | null }>): string =>
  issues.map((issue) => issue.field ? `${issue.field}: ${issue.message} (${issue.code})` : `${issue.message} (${issue.code})`).join('\n')

type PersistedSceneOperation = {
  format: 'InstancedSceneOperation/1'
  job_id: string
  request: InstancedSceneExportRequest
}

const PERSISTED_OPERATION_KEY = 'endfield.atlas.instanced-scene-operation.v1'

const readPersistedSceneOperation = (): PersistedSceneOperation | null => {
  const serialized = window.sessionStorage.getItem(PERSISTED_OPERATION_KEY)
  if (serialized === null) return null
  let value: unknown
  try {
    value = JSON.parse(serialized) as unknown
  } catch (error) {
    throw new Error('已保存的独立场景任务记录不是有效 JSON。', { cause: error })
  }
  if (typeof value !== 'object' || value === null ||
    !('format' in value) || value.format !== 'InstancedSceneOperation/1' ||
    !('job_id' in value) || typeof value.job_id !== 'string' ||
    !('request' in value) || !isInstancedSceneExportRequest(value.request) ||
    value.job_id !== value.request.request_id) {
    throw new Error('已保存的独立场景任务记录与当前请求契约不匹配。')
  }
  return value as PersistedSceneOperation
}

const persistSceneOperation = (request: InstancedSceneExportRequest): PersistedSceneOperation => {
  const operation: PersistedSceneOperation = {
    format: 'InstancedSceneOperation/1',
    job_id: request.request_id,
    request,
  }
  let serialized: string
  try {
    serialized = JSON.stringify(operation)
  } catch (error) {
    throw new Error('独立场景任务无法序列化为可恢复记录。', { cause: error })
  }
  window.sessionStorage.setItem(PERSISTED_OPERATION_KEY, serialized)
  return operation
}

const clearPersistedSceneOperation = (): void => {
  window.sessionStorage.removeItem(PERSISTED_OPERATION_KEY)
}

const availableModes = (
  catalog: InstancedSceneCatalog | null,
  scene: InstancedSceneCatalog['scenes'][number] | null,
  detail: InstancedSceneDetail | null,
): InstancedSceneExportMode[] => {
  if (!catalog || !scene || !detail || detail.scene_id !== scene.scene_id ||
    detail.catalog_id !== catalog.catalog_id || detail.first_run_identity !== catalog.first_run_identity ||
    !scene.export_capability.ready || !detail.export_capability.ready) return []
  return catalog.capabilities.export_modes.filter((mode) =>
    scene.export_capability.export_modes.includes(mode) && detail.export_capability.export_modes.includes(mode))
}

export function useInstancedScenes(options: UseInstancedScenesOptions): InstancedScenesController {
  const optionsRef = useRef(options)
  optionsRef.current = options

  const [connection, setConnection] = useState<InstancedScenesConnection>('idle')
  const [catalog, setCatalog] = useState<InstancedSceneCatalog | null>(null)
  const [selectedSceneId, setSelectedSceneId] = useState<string | null>(null)
  const [detail, setDetail] = useState<InstancedSceneDetail | null>(null)
  const [preview, setPreview] = useState<InstancedScenePreview | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [diagnostic, setDiagnostic] = useState<InstancedSceneDiagnostic | null>(null)
  const [job, setJob] = useState<InstancedSceneJob | null>(null)
  const [events, setEvents] = useState<InstancedSceneJobEvent[]>([])
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [operationRequest, setOperationRequest] = useState<InstancedSceneExportRequest | null>(null)
  const [operationPending, setOperationPending] = useState(false)
  const [cancelRequested, setCancelRequested] = useState(false)
  const [recoveryNeedsDiscard, setRecoveryNeedsDiscard] = useState(false)

  const catalogRef = useRef<InstancedSceneCatalog | null>(null)
  const detailRef = useRef<InstancedSceneDetail | null>(null)
  const selectedSceneIdRef = useRef<string | null>(null)
  const catalogGeneration = useRef(0)
  const sceneGeneration = useRef(0)
  const jobGeneration = useRef(0)
  const cancelGeneration = useRef(0)
  const eventCursor = useRef(0)
  const activeJobIdRef = useRef<string | null>(null)
  const operationRequestRef = useRef<InstancedSceneExportRequest | null>(null)
  const restorePending = useRef(false)
  const recoveryStartedFor = useRef<string | null>(null)
  const operationPendingRef = useRef(false)
  const catalogAbort = useRef<AbortController | null>(null)
  const sceneAbort = useRef<AbortController | null>(null)
  const launchAbort = useRef<AbortController | null>(null)
  const jobAbort = useRef<AbortController | null>(null)
  const cancelAbort = useRef<AbortController | null>(null)
  const jobTimer = useRef<number | null>(null)

  const reportFailure = useCallback((failure: unknown, summary: string | null): void => {
    setDiagnostic(diagnosticFor(failure))
    setError(summary ?? userFacingError(failure))
  }, [])

  catalogRef.current = catalog
  detailRef.current = detail
  selectedSceneIdRef.current = selectedSceneId
  activeJobIdRef.current = activeJobId
  operationRequestRef.current = operationRequest
  operationPendingRef.current = operationPending

  useEffect(() => {
    let savedOperation: PersistedSceneOperation | null
    try {
      savedOperation = readPersistedSceneOperation()
    } catch (error) {
      reportFailure(error, '无法读取可恢复的独立场景任务；请查看技术诊断。')
      return
    }
    if (savedOperation === null || activeJobIdRef.current === savedOperation.job_id) return
    activeJobIdRef.current = savedOperation.job_id
    operationRequestRef.current = savedOperation.request
    selectedSceneIdRef.current = savedOperation.request.scene_id
    restorePending.current = true
    setOperationRequest(savedOperation.request)
    setActiveJobId(savedOperation.job_id)
    setSelectedSceneId(savedOperation.request.scene_id)
    setConnection('disconnected')
    setError(`发现可恢复任务 ${savedOperation.job_id}；打开独立场景工作台后将尝试重连。`)
  }, [reportFailure])

  const clearJobTimer = useCallback(() => {
    if (jobTimer.current !== null) {
      window.clearTimeout(jobTimer.current)
      jobTimer.current = null
    }
  }, [])

  const abortJobPoll = useCallback(() => {
    clearJobTimer()
    jobAbort.current?.abort()
    jobAbort.current = null
  }, [clearJobTimer])

  const clearActiveOperation = useCallback((jobId: string): void => {
    if (activeJobIdRef.current !== jobId) return
    clearPersistedSceneOperation()
    activeJobIdRef.current = null
    operationRequestRef.current = null
    restorePending.current = false
    recoveryStartedFor.current = null
    setRecoveryNeedsDiscard(false)
    setActiveJobId(null)
    setOperationRequest(null)
    setCancelRequested(false)
  }, [])

  const pollJob = useCallback(async (
    jobId: string,
    after: number,
    generation: number,
  ): Promise<void> => {
    if (generation !== jobGeneration.current || activeJobIdRef.current !== jobId) return
    const request = operationRequestRef.current
    if (!request || request.request_id !== jobId) {
      const requestError = new Error(`任务 ${jobId} 缺少匹配的不可变导出请求，已停止状态轮询。`)
      setConnection('unavailable')
      reportFailure(requestError, requestError.message)
      return
    }
    const controller = new AbortController()
    jobAbort.current?.abort()
    jobAbort.current = controller
    try {
      const [nextJob, eventPage] = await Promise.all([
        getInstancedSceneJob(jobId, request, controller.signal),
        getInstancedSceneJobEvents(jobId, after, controller.signal),
      ])
      if (controller.signal.aborted || generation !== jobGeneration.current || activeJobIdRef.current !== jobId) return

      setJob(nextJob)
      setEvents((current) => mergeEvents(current, eventPage.events))
      eventCursor.current = Math.max(eventCursor.current, eventPage.next_after)
      setCancelRequested(nextJob.cancel_requested)
      setConnection('ready')
      setError(null)
      setDiagnostic(null)

      if (isTerminal(nextJob)) {
        clearJobTimer()
        jobAbort.current = null
        clearActiveOperation(jobId)
        return
      }

      jobTimer.current = window.setTimeout(
        () => void pollJob(jobId, eventCursor.current, generation),
        700,
      )
    } catch (pollError) {
      if (controller.signal.aborted || generation !== jobGeneration.current || activeJobIdRef.current !== jobId) return
      jobAbort.current = null
      clearJobTimer()
      setConnection('disconnected')
      reportFailure(
        pollError,
        `任务状态连接中断，任务编号 ${jobId} 已保留；可重连或继续请求取消。${userFacingError(pollError)}`,
      )
    }
  }, [clearActiveOperation, clearJobTimer, reportFailure])

  const refreshCatalog = useCallback(async (): Promise<void> => {
    const currentOptions = optionsRef.current
    if (!currentOptions.active) return

    const generation = ++catalogGeneration.current
    catalogAbort.current?.abort()
    const controller = new AbortController()
    catalogAbort.current = controller
    setConnection('loading')
    setError(null)
    setDiagnostic(null)

    try {
      const developmentPreview = currentOptions.developmentPreview
      if (developmentPreview !== null) {
        if (!isInstancedSceneDevelopmentPreview(developmentPreview)) {
          throw new Error('开发预览数据不符合场景接口约定，请检查本地预览配置。')
        }
        if (generation !== catalogGeneration.current) return
        setCatalog(developmentPreview.catalog)
        const currentSelection = selectedSceneIdRef.current
        const nextSelection = currentSelection && developmentPreview.catalog.scenes.some((scene) => scene.scene_id === currentSelection)
          ? currentSelection
          : null
        selectedSceneIdRef.current = nextSelection
        setSelectedSceneId(nextSelection)
        setConnection('ready')
        setError(null)
        setDiagnostic(null)
        return
      }

      const nextCatalog = await getInstancedSceneCatalog(currentOptions.firstRunIdentity, controller.signal)
      if (controller.signal.aborted || generation !== catalogGeneration.current) return
      setCatalog(nextCatalog)
      const currentSelection = selectedSceneIdRef.current
      const nextSelection = currentSelection && nextCatalog.scenes.some((scene) => scene.scene_id === currentSelection)
        ? currentSelection
        : null
      selectedSceneIdRef.current = nextSelection
      setSelectedSceneId(nextSelection)
      setConnection(nextCatalog.availability.ready ? 'ready' : 'unavailable')
      if (nextCatalog.availability.ready) {
        setError(null)
        setDiagnostic(null)
      } else {
        const availabilityError = new Error(nextCatalog.availability.reason || 'The instanced scene catalog is unavailable.')
        reportFailure(availabilityError, availabilityError.message)
      }
    } catch (catalogError) {
      if (controller.signal.aborted || generation !== catalogGeneration.current) return
      setConnection(errorConnection(catalogError))
      reportFailure(catalogError, null)
      throw new Error(userFacingError(catalogError), { cause: catalogError })
    } finally {
      if (catalogAbort.current === controller) catalogAbort.current = null
    }
  }, [reportFailure])

  const selectScene = useCallback(async (sceneId: string): Promise<void> => {
    const currentCatalog = catalogRef.current
    if (!currentCatalog || !currentCatalog.scenes.some((scene) => scene.scene_id === sceneId)) {
      const selectionError = new Error(`场景 ${sceneId} 不在当前目录中，请刷新目录后重试。`)
      reportFailure(selectionError, selectionError.message)
      throw selectionError
    }
    selectedSceneIdRef.current = sceneId
    setSelectedSceneId(sceneId)
    setDetail(null)
    setPreview(null)
    setError(null)
    setDiagnostic(null)
  }, [reportFailure])

  useEffect(() => {
    if (!options.active) return
    void refreshCatalog().catch(() => undefined)
    return () => {
      catalogGeneration.current += 1
      catalogAbort.current?.abort()
      catalogAbort.current = null
    }
  }, [options.active, options.firstRunReady, options.firstRunIdentity, options.developmentPreview, refreshCatalog])

  useEffect(() => {
    if (!options.active || selectedSceneId === null) {
      sceneGeneration.current += 1
      sceneAbort.current?.abort()
      sceneAbort.current = null
      setDetail(null)
      setPreview(null)
      setDetailLoading(false)
      setPreviewLoading(false)
      return
    }

    const currentOptions = optionsRef.current
    const generation = ++sceneGeneration.current
    sceneAbort.current?.abort()
    const controller = new AbortController()
    sceneAbort.current = controller
    setDetailLoading(true)
    setPreviewLoading(true)
    setDetail(null)
    setPreview(null)

    if (currentOptions.developmentPreview !== null) {
      const fixture = currentOptions.developmentPreview
      if (!isInstancedSceneDevelopmentPreview(fixture)) {
        setDetailLoading(false)
        setPreviewLoading(false)
        setError('开发预览数据不符合场景接口约定，请检查本地预览配置。')
        setDiagnostic({ kind: 'application', method: null, path: null, status: null, responseBody: null, detail: '开发预览数据不符合场景接口约定，请检查本地预览配置。' })
        return () => controller.abort()
      }
      const nextDetail = fixture.details.find((entry) => entry.scene_id === selectedSceneId) ?? null
      const nextPreview = fixture.previews.find((entry) => entry.scene_id === selectedSceneId) ?? null
      setDetail(nextDetail)
      setPreview(nextPreview)
      setDetailLoading(false)
      setPreviewLoading(false)
      setError(nextDetail && nextPreview ? null : `开发预览缺少场景 ${selectedSceneId} 的详情或预览数据。`)
      setDiagnostic(nextDetail && nextPreview ? null : {
        kind: 'application',
        method: null,
        path: null,
        status: null,
        responseBody: null,
        detail: `开发预览缺少场景 ${selectedSceneId} 的详情或预览数据。`,
      })
      return () => controller.abort()
    }

    const currentCatalog = catalogRef.current
    if (!currentCatalog?.scenes.some((scene) => scene.scene_id === selectedSceneId)) {
      setDetailLoading(false)
      setPreviewLoading(false)
      return () => controller.abort()
    }

    const loadScene = async (): Promise<void> => {
      try {
        const [nextDetail, nextPreview] = await Promise.all([
          getInstancedSceneDetail(selectedSceneId, currentCatalog.catalog_id, currentCatalog.first_run_identity, controller.signal),
          getInstancedScenePreview(selectedSceneId, currentCatalog.catalog_id, currentCatalog.first_run_identity, controller.signal),
        ])
        if (controller.signal.aborted || generation !== sceneGeneration.current) return
        setDetail(nextDetail)
        setPreview(nextPreview)
        setConnection(currentCatalog.availability.ready ? 'ready' : 'unavailable')
        setError(null)
        setDiagnostic(null)
      } catch (sceneError) {
        if (controller.signal.aborted || generation !== sceneGeneration.current) return
        setConnection(errorConnection(sceneError))
        reportFailure(sceneError, null)
      } finally {
        if (generation === sceneGeneration.current) {
          setDetailLoading(false)
          setPreviewLoading(false)
          if (sceneAbort.current === controller) sceneAbort.current = null
        }
      }
    }

    void loadScene()
    return () => {
      controller.abort()
      if (sceneAbort.current === controller) sceneAbort.current = null
    }
  }, [options.active, options.developmentPreview, options.firstRunIdentity, selectedSceneId, catalog, reportFailure])

  useEffect(() => {
    options.onCatalogCountChange(catalog?.availability.ready ? catalog.scenes.length : null)
  }, [catalog, options.onCatalogCountChange])

  const busy = operationPending || activeJobId !== null
  useEffect(() => {
    options.onBusyChange(busy)
  }, [busy, options.onBusyChange])

  const startExport = useCallback(async (request: InstancedSceneExportRequest): Promise<void> => {
    const currentOptions = optionsRef.current
    const currentCatalog = catalogRef.current
    const currentDetail = detailRef.current
    const currentSceneId = selectedSceneIdRef.current
    const rejectStart = (message: string): never => {
      reportFailure(new Error(message), message)
      throw new Error(message)
    }

    if (!currentOptions.active) return rejectStart('请先打开独立场景工作区，再提交导出。')
    if (currentOptions.developmentPreview !== null) return rejectStart('开发预览仅供查看，不能提交真实导出。')
    if (!currentOptions.firstRunReady) return rejectStart('请先完成首次准备，再导出独立场景。')
    if (currentOptions.firstRunIdentity === null) return rejectStart('首次准备身份尚不可用，请刷新首次准备状态。')
    if (currentOptions.firstRunBusy) return rejectStart('首次准备仍在运行，请完成后再导出。')
    if (currentOptions.mapJobBusy) return rejectStart('地图导出任务仍在运行，请完成后再导出独立场景。')
    if (operationPendingRef.current || activeJobIdRef.current !== null) return rejectStart('已有独立场景导出任务，请等待任务结束。')
    if (!currentCatalog) return rejectStart('独立场景目录尚未加载，请刷新目录后重试。')
    if (!currentCatalog.availability.ready) return rejectStart(currentCatalog.availability.reason || '独立场景目录当前不可用。')
    if (currentCatalog.first_run_identity !== currentOptions.firstRunIdentity) return rejectStart('当前目录属于旧的首次准备身份，请刷新目录后重试。')
    if (!currentSceneId) return rejectStart('请先选择一个独立场景。')

    const selectedSummary = currentCatalog.scenes.find((scene) => scene.scene_id === currentSceneId)
    if (!selectedSummary) return rejectStart(`场景 ${currentSceneId} 已不在当前目录中，请刷新目录后重试。`)
    if (!selectedSummary.export_capability.ready) return rejectStart(selectedSummary.export_capability.reason || '该场景当前不具备导出条件。')
    if (!currentDetail || currentDetail.scene_id !== currentSceneId ||
      currentDetail.catalog_id !== currentCatalog.catalog_id ||
      currentDetail.first_run_identity !== currentCatalog.first_run_identity) {
      return rejectStart('场景详情与当前目录或首次准备身份不匹配，请刷新目录后重试。')
    }
    if (!currentDetail.export_capability.ready) return rejectStart(currentDetail.export_capability.reason || '场景详情显示该场景尚不具备导出条件。')
    if (request.scene_id !== currentSceneId) return rejectStart('导出请求中的场景编号与当前选择不一致，请重新选择场景。')
    if (request.catalog_id !== currentCatalog.catalog_id) return rejectStart('导出请求使用了旧目录编号，请刷新目录并重试。')
    if (request.first_run_identity !== currentOptions.firstRunIdentity) return rejectStart('导出请求中的首次准备身份已过期，请刷新首次准备状态。')
    if (!isInstancedSceneExportRequest(request)) return rejectStart('导出请求缺少有效的 UUID request_id 或必要字段。')
    if (!availableModes(currentCatalog, selectedSummary, currentDetail).includes(request.export_mode)) {
      return rejectStart(`当前场景不支持 ${request.export_mode} 导出模式。`)
    }
    setError(null)
    setDiagnostic(null)
    setOperationPending(true)
    operationPendingRef.current = true
    setJob(null)
    setEvents([])
    eventCursor.current = 0
    setCancelRequested(false)
    abortJobPoll()
    const generation = ++jobGeneration.current
    const controller = new AbortController()
    launchAbort.current?.abort()
    launchAbort.current = controller

    try {
      const validation = await validateInstancedSceneExport(request, controller.signal)
      if (generation !== jobGeneration.current) return
      if (validation.status !== 'valid') {
        throw new Error(validationMessage(validation.issues) || '独立场景导出请求未通过后端校验。')
      }
      const savedOperation = persistSceneOperation(validation.canonical_request)
      operationRequestRef.current = savedOperation.request
      activeJobIdRef.current = savedOperation.job_id
      setOperationRequest(savedOperation.request)
      setActiveJobId(savedOperation.job_id)
      const submittedJob = await submitInstancedSceneExport(savedOperation.request, controller.signal)
      if (generation !== jobGeneration.current) return
      setJob(submittedJob)
      setConnection('ready')
      if (isTerminal(submittedJob)) {
        setOperationPending(false)
        operationPendingRef.current = false
        clearActiveOperation(submittedJob.job_id)
        return
      }
      setCancelRequested(submittedJob.cancel_requested)
      setOperationPending(false)
      operationPendingRef.current = false
      void pollJob(submittedJob.job_id, 0, generation)
    } catch (startError) {
      if (generation !== jobGeneration.current) return
      setOperationPending(false)
      operationPendingRef.current = false
      const preservedJobId = operationRequestRef.current?.request_id
      if (preservedJobId && activeJobIdRef.current === preservedJobId) {
        setConnection('disconnected')
        reportFailure(startError, `提交响应未确认；任务编号 ${preservedJobId} 与原请求已保留，可重连恢复或安全重试。${userFacingError(startError)}`)
      } else {
        setConnection(errorConnection(startError))
        reportFailure(startError, null)
      }
      throw new Error(userFacingError(startError), { cause: startError })
    } finally {
      if (launchAbort.current === controller) launchAbort.current = null
      if (generation === jobGeneration.current && activeJobIdRef.current === null) {
        setOperationPending(false)
        operationPendingRef.current = false
      }
    }
  }, [abortJobPoll, clearActiveOperation, pollJob, reportFailure])

  const cancelExport = useCallback(async (): Promise<void> => {
    const jobId = activeJobIdRef.current
    const currentJob = job
    if (optionsRef.current.developmentPreview !== null) {
      const cancelError = new Error('开发预览仅供查看，不能请求取消真实导出。')
      reportFailure(cancelError, cancelError.message)
      throw cancelError
    }
    if (!jobId || !currentJob || isTerminal(currentJob)) {
      const cancelError = new Error('当前没有可取消的独立场景导出任务。')
      reportFailure(cancelError, cancelError.message)
      throw cancelError
    }
    if (cancelAbort.current !== null) {
      const cancelError = new Error(`任务 ${jobId} 的取消请求仍在处理中。`)
      reportFailure(cancelError, cancelError.message)
      throw new Error(userFacingError(cancelError), { cause: cancelError })
    }

    const generation = jobGeneration.current
    const cancelOperation = ++cancelGeneration.current
    const controller = new AbortController()
    cancelAbort.current = controller
    setOperationPending(true)
    operationPendingRef.current = true
    setError(null)
    setDiagnostic(null)
    try {
      const acknowledgement: InstancedSceneCancelAcknowledgement = await requestInstancedSceneJobCancellation(jobId, controller.signal)
      if (controller.signal.aborted || generation !== jobGeneration.current || activeJobIdRef.current !== jobId) return
      setCancelRequested(acknowledgement.state === 'accepted')
      setJob((current) => current?.job_id === jobId
        ? { ...current, cancel_requested: acknowledgement.state === 'accepted' }
        : current)
      setOperationPending(false)
      operationPendingRef.current = false
      clearJobTimer()
      jobAbort.current?.abort()
      void pollJob(jobId, eventCursor.current, generation)
    } catch (cancelError) {
      if (controller.signal.aborted || generation !== jobGeneration.current) return
      if (activeJobIdRef.current !== jobId) return
      setOperationPending(false)
      operationPendingRef.current = false
      reportFailure(cancelError, null)
      throw new Error(userFacingError(cancelError), { cause: cancelError })
    } finally {
      if (cancelAbort.current === controller) cancelAbort.current = null
      if (cancelOperation === cancelGeneration.current && generation === jobGeneration.current) {
        setOperationPending(false)
        operationPendingRef.current = false
      }
    }
  }, [clearJobTimer, job, pollJob, reportFailure])

  const reconnect = useCallback(async (): Promise<void> => {
    const jobId = activeJobIdRef.current
    const request = operationRequestRef.current
    if (!jobId) {
      const reconnectError = new Error('没有保留的独立场景任务编号可供重连。')
      reportFailure(reconnectError, reconnectError.message)
      throw reconnectError
    }
    if (!request || request.request_id !== jobId) {
      const requestError = new Error(`任务 ${jobId} 缺少匹配的不可变导出请求，无法安全重连。`)
      reportFailure(requestError, requestError.message)
      throw requestError
    }
    if (operationPendingRef.current) {
      const reconnectError = new Error(`任务 ${jobId} 的导出或取消请求仍在处理中，请稍候。`)
      reportFailure(reconnectError, reconnectError.message)
      throw reconnectError
    }
    if (optionsRef.current.developmentPreview !== null) {
      const reconnectError = new Error('开发预览没有真实后端任务，不能重连。')
      reportFailure(reconnectError, reconnectError.message)
      throw reconnectError
    }
    const currentOptions = optionsRef.current
    if (!isRecoveryContextReady(currentOptions, request)) {
      const recoveryError = new Error(
        '当前首次准备状态或源路径尚未就绪；为避免旧任务访问当前缓存，已停止重连。完成准备后可重试。',
      )
      setConnection('unavailable')
      reportFailure(recoveryError, recoveryError.message)
      throw recoveryError
    }
    const currentContextRequest: InstancedSceneExportRequest = {
      ...request,
      first_run_identity: currentOptions.firstRunIdentity,
      source: currentOptions.source,
    }
    if (!hasSameInstancedSceneRequestSemantics(request, currentContextRequest)) {
      const recoveryError = new Error(
        '保留任务属于不同的首次准备身份或源路径。为避免访问当前缓存或重提交旧请求，已停止重连；可恢复原路径后重试，或显式丢弃本地恢复记录。丢弃不会取消旧服务端任务或删除其输出。',
      )
      setRecoveryNeedsDiscard(true)
      setConnection('unavailable')
      reportFailure(recoveryError, recoveryError.message)
      throw recoveryError
    }
    setRecoveryNeedsDiscard(false)

    abortJobPoll()
    const generation = ++jobGeneration.current
    const controller = new AbortController()
    launchAbort.current?.abort()
    launchAbort.current = controller
    setConnection('loading')
    setError(null)
    setDiagnostic(null)
    setOperationPending(true)
    operationPendingRef.current = true
    try {
      let recoveredJob: InstancedSceneJob
      try {
        recoveredJob = await getInstancedSceneJob(jobId, request, controller.signal)
      } catch (lookupError) {
        const jobMissing = lookupError instanceof BackendRequestError &&
          lookupError.kind === 'http' && lookupError.status === 404
        if (!jobMissing) throw lookupError
        recoveredJob = await submitInstancedSceneExport(request, controller.signal)
      }
      if (controller.signal.aborted || generation !== jobGeneration.current || activeJobIdRef.current !== jobId) return
      setJob(recoveredJob)
      setConnection('ready')
      setError(null)
      setDiagnostic(null)
      setEvents([])
      eventCursor.current = 0
      if (isTerminal(recoveredJob)) {
        clearActiveOperation(jobId)
        setOperationPending(false)
        operationPendingRef.current = false
        return
      }
      setCancelRequested(recoveredJob.cancel_requested)
      setOperationPending(false)
      operationPendingRef.current = false
      void pollJob(jobId, 0, generation)
    } catch (reconnectError) {
      if (controller.signal.aborted || generation !== jobGeneration.current) return
      setConnection(errorConnection(reconnectError))
      reportFailure(
        reconnectError,
        `任务编号 ${jobId} 与原请求仍已保留；可再次重连或以相同 request_id 安全重试。${userFacingError(reconnectError)}`,
      )
      throw new Error(userFacingError(reconnectError), { cause: reconnectError })
    } finally {
      if (launchAbort.current === controller) launchAbort.current = null
      if (generation === jobGeneration.current) {
        setOperationPending(false)
        operationPendingRef.current = false
      }
    }
  }, [abortJobPoll, clearActiveOperation, pollJob, reportFailure])

  const discardStaleRecovery = useCallback((): void => {
    const jobId = activeJobIdRef.current
    if (!recoveryNeedsDiscard || !jobId || operationRequestRef.current === null) {
      throw new Error('没有需要显式丢弃的过期独立场景恢复记录。')
    }
    abortJobPoll()
    launchAbort.current?.abort()
    launchAbort.current = null
    jobGeneration.current += 1
    setOperationPending(false)
    operationPendingRef.current = false
    setJob(null)
    setEvents([])
    clearActiveOperation(jobId)
    catalogRef.current = null
    detailRef.current = null
    selectedSceneIdRef.current = null
    setCatalog(null)
    setDetail(null)
    setPreview(null)
    setSelectedSceneId(null)
    setConnection('idle')
    setDiagnostic(null)
    setError(null)
  }, [abortJobPoll, clearActiveOperation, recoveryNeedsDiscard])

  useEffect(() => {
    const jobId = activeJobId
    if (!options.active || !restorePending.current || !jobId || recoveryStartedFor.current === jobId ||
      !isRecoveryContextReady(options, operationRequest)) return
    recoveryStartedFor.current = jobId
    restorePending.current = false
    void reconnect().catch(() => undefined)
  }, [activeJobId, options.active, options.firstRunReady, options.firstRunBusy, options.firstRunIdentity,
    options.source.game_root, options.source.cache_root, options.source.blender_exe, options.developmentPreview,
    operationRequest, reconnect])

  useEffect(() => () => {
    catalogGeneration.current += 1
    sceneGeneration.current += 1
    jobGeneration.current += 1
    catalogAbort.current?.abort()
    sceneAbort.current?.abort()
    launchAbort.current?.abort()
    jobAbort.current?.abort()
    cancelAbort.current?.abort()
    cancelGeneration.current += 1
    clearJobTimer()
  }, [clearJobTimer])

  const selectedSummary = catalog?.scenes.find((scene) => scene.scene_id === selectedSceneId) ?? null
  const supportedExportModes = availableModes(catalog, selectedSummary, detail)
  const canStartExport = options.active && options.firstRunReady && !options.firstRunBusy && !options.mapJobBusy &&
    options.developmentPreview === null && connection === 'ready' && catalog?.availability.ready === true &&
    catalog.first_run_identity === options.firstRunIdentity &&
    selectedSummary !== null && detail?.scene_id === selectedSceneId && supportedExportModes.length > 0 &&
    !detailLoading && !operationPending && activeJobId === null
  const canCancel = options.developmentPreview === null && activeJobId !== null && job !== null && !isTerminal(job)

  return {
    connection,
    catalog,
    selectedSceneId,
    detail,
    preview,
    detailLoading,
    previewLoading,
    error,
    diagnostic,
    job,
    events,
    activeJobId,
    operationRequest,
    operationPending,
    cancelRequested,
    recoveryNeedsDiscard,
    availableExportModes: supportedExportModes,
    canStartExport,
    canCancel,
    selectScene,
    refreshCatalog,
    startExport,
    cancelExport,
    reconnect,
    discardStaleRecovery,
  }
}
