import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BackendRequestError, isJsonObject } from '../../lib/backendClient'
import {
  getCharacterAnimations,
  getCharacterCatalog,
  getCharacterDetail,
  getCharacterJob,
  getCharacterJobEvents,
  isTerminalCharacterJob,
  parseCharacterExportRequestForStorage,
  parseCharacterPreparationRequestForStorage,
  requestCharacterCancellation,
  submitCharacterExport,
  submitCharacterPreparation,
  validateCharacterExport,
  validateCharacterPreparation,
} from './api'
import type {
  CharacterAnimation,
  CharacterAnimationCatalog,
  CharacterCancelAcknowledgement,
  CharacterCatalog,
  CharacterDetail,
  CharacterDiagnostic,
  CharacterExportRequest,
  CharacterJob,
  CharacterJobEvent,
  CharacterJobKind,
  CharacterPreparationRequest,
  CharacterPreparationSource,
  CharacterRequestValidation,
  CharacterSummary,
  CharacterVariant,
} from './types'

export type OperatorsConnection = 'idle' | 'loading' | 'ready' | 'unavailable' | 'disconnected'

export type CharacterOption = {
  id: string
  name: string
  en: string
  code: string
  type: 'playable' | 'independent_npc'
  ready: boolean
  reason: string | null
  variants: CharacterVariant[]
  capabilities: CharacterSummary['capabilities']
}

export type UseOperatorsOptions = {
  enabled: boolean
  active: boolean
  selectionId: string | null
  variantId: string | null
  preparationSource: CharacterPreparationSource | null
}

export type OperatorsController = {
  connection: OperatorsConnection
  catalog: CharacterCatalog | null
  characterOptions: CharacterOption[]
  selected: CharacterSummary | null
  detail: CharacterDetail | null
  selectedVariant: CharacterVariant | null
  animationCatalog: CharacterAnimationCatalog | null
  actions: CharacterAnimation[]
  catalogLoading: boolean
  detailLoading: boolean
  actionsLoading: boolean
  hasMoreActions: boolean
  error: string | null
  diagnostic: CharacterDiagnostic | null
  validation: CharacterRequestValidation | null
  job: CharacterJob | null
  events: CharacterJobEvent[]
  activeJobId: string | null
  operationKind: CharacterJobKind | null
  operationRequest: CharacterPreparationRequest | CharacterExportRequest | null
  operationPending: boolean
  cancelRequested: boolean
  cancelPending: boolean
  operationBusy: boolean
  canStartExport: boolean
  canPrepare: boolean
  canCancel: boolean
  refreshCatalog: () => Promise<void>
  loadMoreActions: () => Promise<void>
  validateExport: (request: CharacterExportRequest) => Promise<CharacterRequestValidation | null>
  startExport: (request: CharacterExportRequest) => Promise<void>
  validatePreparation: (request: CharacterPreparationRequest) => Promise<CharacterRequestValidation | null>
  prepare: (request: CharacterPreparationRequest) => Promise<void>
  cancel: () => Promise<CharacterCancelAcknowledgement | null>
  reconnect: () => Promise<void>
}

type StoredCharacterOperation = {
  format: 'CharacterOperation/1'
  kind: CharacterJobKind
  request: CharacterPreparationRequest | CharacterExportRequest
}

const OPERATION_STORAGE_KEY = 'endfield.character.operation.v1'
const OPERATION_POLL_INTERVAL = 1200
const OPERATION_RETRY_INTERVAL = 2500
const EVENT_PAGE_LIMIT = 250
const TRANSIENT_HTTP_STATUSES = [502, 503, 504] as const

const errorMessage = (error: unknown): string => error instanceof Error ? error.message : String(error)

const diagnosticFor = (error: unknown): CharacterDiagnostic => {
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

const isTransientBackendFailure = (error: unknown): boolean =>
  error instanceof BackendRequestError && (error.kind === 'request' ||
    (error.kind === 'http' && TRANSIENT_HTTP_STATUSES.includes(error.status as typeof TRANSIENT_HTTP_STATUSES[number])))

const errorConnection = (error: unknown): 'unavailable' | 'disconnected' =>
  isTransientBackendFailure(error) ? 'disconnected' : 'unavailable'

const mergeEvents = (current: CharacterJobEvent[], incoming: CharacterJobEvent[]): CharacterJobEvent[] => {
  const bySequence = new Map<number, CharacterJobEvent>()
  current.forEach((event) => bySequence.set(event.sequence, event))
  incoming.forEach((event) => bySequence.set(event.sequence, event))
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence).slice(-120)
}

const operationKindForRequest = (request: StoredCharacterOperation['request']): CharacterJobKind =>
  request.format === 'CharacterPreparationRequest/1' ? 'preparation' : 'export'

const readStoredOperation = (): StoredCharacterOperation | null => {
  if (typeof window === 'undefined') return null
  const serialized = window.sessionStorage.getItem(OPERATION_STORAGE_KEY)
  if (serialized === null) return null
  let value: unknown
  try {
    value = JSON.parse(serialized) as unknown
  } catch (error) {
    throw new Error('Saved character task is not valid JSON.', { cause: error })
  }
  if (!isJsonObject(value) || value.format !== 'CharacterOperation/1' ||
    (value.kind !== 'preparation' && value.kind !== 'export')) {
    throw new Error('Saved character task does not match the current request contract.')
  }
  const request = value.kind === 'preparation'
    ? parseCharacterPreparationRequestForStorage(value.request)
    : parseCharacterExportRequestForStorage(value.request)
  if (operationKindForRequest(request) !== value.kind) {
    throw new Error('Saved character task kind differs from its request format.')
  }
  return { format: 'CharacterOperation/1', kind: value.kind, request }
}

const persistOperation = (operation: StoredCharacterOperation): void => {
  if (typeof window === 'undefined') throw new Error('Character tasks require a browser session for recovery.')
  let serialized: string
  try {
    serialized = JSON.stringify(operation)
  } catch (error) {
    throw new Error('Character task could not be saved for recovery.', { cause: error })
  }
  window.sessionStorage.setItem(OPERATION_STORAGE_KEY, serialized)
}

const pathsMatch = (left: CharacterPreparationSource, right: CharacterPreparationSource): boolean =>
  left.game_root === right.game_root && left.blender_exe === right.blender_exe &&
  left.cache_root === right.cache_root && left.output_root === right.output_root

const assertPreparationSource = (
  request: CharacterPreparationRequest,
  currentSource: CharacterPreparationSource | null,
): void => {
  if (currentSource !== null && !pathsMatch(request.source, currentSource)) {
    throw new Error('Preparation request paths differ from the paths currently shown in the form.')
  }
}

const assertExportContext = (
  request: CharacterExportRequest,
  catalog: CharacterCatalog | null,
  selected: CharacterSummary | null,
  detail: CharacterDetail | null,
  variant: CharacterVariant | null,
  animationCatalog: CharacterAnimationCatalog | null,
): void => {
  if (catalog === null || !catalog.availability.ready || catalog.source_identity === null ||
    selected === null || detail === null || variant === null || detail.selection_id !== selected.selection_id ||
    detail.catalog_id !== catalog.catalog_id) {
    throw new Error('Load an available character catalog, detail, and model variant before exporting.')
  }
  if (request.catalog_id !== catalog.catalog_id || request.source_identity !== catalog.source_identity ||
    request.selection_id !== selected.selection_id || request.variant_id !== variant.variant_id) {
    throw new Error('Export request identity differs from the current character and variant selection.')
  }
  const actionsAreCurrent = animationCatalog !== null && animationCatalog.selection_id === selected.selection_id &&
    animationCatalog.variant_id === variant.variant_id && animationCatalog.source_identity === catalog.source_identity
  const currentAnimations = actionsAreCurrent ? animationCatalog.animations : []
  const currentCpuReferences = actionsAreCurrent ? animationCatalog.source_cpu_action_references : undefined
  if (!selected.capabilities.blend_export ||
    !catalog.profiles.some((profile) => profile.profile_id === request.export_profile_id)) {
    throw new Error(selected.unavailable_reason ?? 'The current character has no ready export profile.')
  }
  const options = request.options
  if (options?.raw_expressions === true && !selected.capabilities.raw_expressions) {
    throw new Error(selected.entry_kind === 'independent_npc'
      ? selected.expression_unavailable_reason
      : 'Raw expressions are unavailable for this character.')
  }
  if (options?.basic_mmd === true && !selected.capabilities.basic_mmd) {
    throw new Error(selected.entry_kind === 'independent_npc'
      ? selected.expression_unavailable_reason
      : 'Basic MMD expressions are unavailable for this character.')
  }
  const animation = options?.animation
  if (options?.native_expression_replay_id !== undefined) {
    if (!selected.capabilities.raw_expressions || animation === undefined || !('action_id' in animation)) {
      throw new Error('Native expression replay requires an available character action.')
    }
    const actionId = animation.action_id
    const nativeReplayId = options.native_expression_replay_id
    const action = currentAnimations.find((item) => item.action_id === actionId)
    if (!action?.native_expression_replays?.some((replay) => replay.replay_id === nativeReplayId)) {
      throw new Error('Native expression replay is not listed for the selected action.')
    }
  }
  if (animation !== undefined) {
    if ('action_id' in animation) {
      if (!selected.capabilities.animation) throw new Error('Animation export is unavailable for this character variant.')
      const actionId = animation.action_id
      const action = currentAnimations.find((item) => item.action_id === actionId)
      if (action === undefined) throw new Error('Selected action is not present in the loaded action catalog pages.')
      if (animation.replay_id !== undefined &&
        !action.ordinary_replays?.some((replay) => replay.replay_id === animation.replay_id)) {
        throw new Error('Ordinary replay is not listed for the selected action.')
      }
      if (animation.finalik_replay_id !== undefined &&
        !action.finalik_replays?.some((replay) => replay.replay_id === animation.finalik_replay_id)) {
        throw new Error('FinalIK replay is not listed for the selected action.')
      }
    } else {
      const references = currentCpuReferences
      const referenceId = animation.reference_id
      if (selected.entry_kind !== 'independent_npc' || references?.status !== 'exact_avatar_base_clip_references' ||
        references.request_available !== true ||
        !references.actions.some((action) => action.reference_id === referenceId && action.request_available)) {
        throw new Error('Selected CPU action reference is not available for export.')
      }
    }
  }
}

const submitOperation = (
  operation: StoredCharacterOperation,
  signal: AbortSignal,
): Promise<CharacterJob> => operation.kind === 'preparation'
  ? submitCharacterPreparation(operation.request as CharacterPreparationRequest, signal)
  : submitCharacterExport(operation.request as CharacterExportRequest, signal)

const validateOperation = (
  operation: StoredCharacterOperation,
  signal: AbortSignal,
): Promise<CharacterRequestValidation> => operation.kind === 'preparation'
  ? validateCharacterPreparation(operation.request as CharacterPreparationRequest, signal)
  : validateCharacterExport(operation.request as CharacterExportRequest, signal)

export function useOperators(options: UseOperatorsOptions): OperatorsController {
  const optionsRef = useRef(options)
  optionsRef.current = options

  const [connection, setConnection] = useState<OperatorsConnection>('idle')
  const [catalog, setCatalog] = useState<CharacterCatalog | null>(null)
  const [detail, setDetail] = useState<CharacterDetail | null>(null)
  const [animationCatalog, setAnimationCatalog] = useState<CharacterAnimationCatalog | null>(null)
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [actionsLoading, setActionsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [diagnostic, setDiagnostic] = useState<CharacterDiagnostic | null>(null)
  const [validation, setValidation] = useState<CharacterRequestValidation | null>(null)
  const [job, setJob] = useState<CharacterJob | null>(null)
  const [events, setEvents] = useState<CharacterJobEvent[]>([])
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [operationKind, setOperationKind] = useState<CharacterJobKind | null>(null)
  const [operationRequest, setOperationRequest] = useState<CharacterPreparationRequest | CharacterExportRequest | null>(null)
  const [operationPending, setOperationPending] = useState(false)
  const [cancelRequested, setCancelRequested] = useState(false)
  const [cancelPending, setCancelPending] = useState(false)

  const catalogRef = useRef<CharacterCatalog | null>(null)
  const detailRef = useRef<CharacterDetail | null>(null)
  const animationCatalogRef = useRef<CharacterAnimationCatalog | null>(null)
  const jobRef = useRef<CharacterJob | null>(null)
  const operationRef = useRef<StoredCharacterOperation | null>(null)
  const activeJobIdRef = useRef<string | null>(null)
  const jobKindRef = useRef<CharacterJobKind | null>(null)
  const catalogGeneration = useRef(0)
  const detailGeneration = useRef(0)
  const actionGeneration = useRef(0)
  const jobGeneration = useRef(0)
  const eventCursor = useRef(0)
  const catalogAbort = useRef<AbortController | null>(null)
  const detailAbort = useRef<AbortController | null>(null)
  const actionAbort = useRef<AbortController | null>(null)
  const launchAbort = useRef<AbortController | null>(null)
  const jobAbort = useRef<AbortController | null>(null)
  const cancelAbort = useRef<AbortController | null>(null)
  const jobTimer = useRef<number | null>(null)
  const reconnectRef = useRef<(() => Promise<void>) | null>(null)
  const cancelInFlight = useRef(false)
  const cancelResolution = useRef<'posting' | 'accepted' | 'uncertain' | null>(null)
  const preparationRefreshJobId = useRef<string | null>(null)
  const restoreAttempted = useRef(false)

  catalogRef.current = catalog
  detailRef.current = detail
  animationCatalogRef.current = animationCatalog
  jobRef.current = job

  const selected = useMemo<CharacterSummary | null>(() => {
    if (catalog === null || options.selectionId === null) return null
    const summary = catalog.characters.find((character) => character.selection_id === options.selectionId) ?? null
    if (summary === null) return null
    return detail?.catalog_id === catalog.catalog_id && detail.selection_id === summary.selection_id ? detail : summary
  }, [catalog, detail, options.selectionId])

  const selectedVariant = useMemo(() => {
    if (detail === null || options.variantId === null) return null
    return detail.variants.find((variant) => variant.variant_id === options.variantId) ?? null
  }, [detail, options.variantId])

  const characterOptions = useMemo<CharacterOption[]>(() => catalog?.characters.map((character) => {
    const current = detail?.catalog_id === catalog.catalog_id && detail.selection_id === character.selection_id
      ? detail
      : character
    const code = current.entry_kind === 'playable' ? current.character_id : current.npc_id
    return {
      id: current.selection_id,
      name: current.name ?? code,
      en: current.english_name ?? '',
      code,
      type: current.entry_kind,
      ready: current.capabilities.blend_export,
      reason: current.unavailable_reason,
      variants: current.variants,
      capabilities: current.capabilities,
    }
  }) ?? [], [catalog, detail])

  const refreshCatalog = useCallback(async (): Promise<void> => {
    if (!optionsRef.current.enabled || !optionsRef.current.active) return
    catalogAbort.current?.abort()
    const controller = new AbortController()
    catalogAbort.current = controller
    const generation = ++catalogGeneration.current
    setCatalogLoading(true)
    setConnection('loading')
    setError(null)
    setDiagnostic(null)
    try {
      const nextCatalog = await getCharacterCatalog(controller.signal)
      if (controller.signal.aborted || generation !== catalogGeneration.current) return
      catalogRef.current = nextCatalog
      setCatalog(nextCatalog)
      setConnection(nextCatalog.availability.ready ? 'ready' : 'unavailable')
      if (!nextCatalog.availability.ready) setError(nextCatalog.availability.reason)
    } catch (caught) {
      if (controller.signal.aborted || generation !== catalogGeneration.current) return
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
      setConnection(errorConnection(caught))
    } finally {
      if (generation === catalogGeneration.current) setCatalogLoading(false)
    }
  }, [])

  const selectedIdForDetail = selected?.selection_id ?? null
  useEffect(() => {
    detailAbort.current?.abort()
    const generation = ++detailGeneration.current
    setDetail(null)
    detailRef.current = null
    setDetailLoading(false)
    if (!options.enabled || !options.active || catalog === null || selectedIdForDetail === null || !catalog.availability.ready) return
    const controller = new AbortController()
    detailAbort.current = controller
    setDetailLoading(true)
    setError(null)
    getCharacterDetail(selectedIdForDetail, catalog.catalog_id, controller.signal)
      .then((nextDetail) => {
        if (controller.signal.aborted || generation !== detailGeneration.current) return
        detailRef.current = nextDetail
        setDetail(nextDetail)
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || generation !== detailGeneration.current) return
        setError(errorMessage(caught))
        setDiagnostic(diagnosticFor(caught))
      })
      .finally(() => {
        if (generation === detailGeneration.current) setDetailLoading(false)
      })
  }, [catalog, options.active, options.enabled, selectedIdForDetail])

  const catalogIdForActions = catalog?.catalog_id ?? null
  const sourceIdentityForActions = catalog?.source_identity ?? null
  const catalogReadyForActions = catalog?.availability.ready === true
  const selectedIdForActions = selected?.selection_id ?? null
  const detailSelectionIdForActions = detail?.selection_id ?? null
  const detailCatalogIdForActions = detail?.catalog_id ?? null
  const detailEntryKindForActions = detail?.entry_kind ?? null
  const detailSourceKindForActions = detail?.entry_kind === 'independent_npc' ? detail.source_kind : null
  const variantIdForActions = options.variantId
  useEffect(() => {
    actionAbort.current?.abort()
    const generation = ++actionGeneration.current
    setAnimationCatalog(null)
    animationCatalogRef.current = null
    setActionsLoading(false)
    if (!options.enabled || !options.active || !catalogReadyForActions || catalogIdForActions === null ||
      sourceIdentityForActions === null || detailRef.current === null || detailCatalogIdForActions !== catalogIdForActions ||
      selectedIdForActions === null || detailSelectionIdForActions !== selectedIdForActions || variantIdForActions === null) return
    const selectedVariantForRefresh = detailRef.current.variants.find((variant) => variant.variant_id === variantIdForActions)
    if (selectedVariantForRefresh === undefined) return
    const controller = new AbortController()
    actionAbort.current = controller
    setActionsLoading(true)
    getCharacterAnimations(
      selectedIdForActions,
      selectedVariantForRefresh.variant_id,
      sourceIdentityForActions,
      0,
      100,
      controller.signal,
    ).then(async (page) => {
      if (controller.signal.aborted || generation !== actionGeneration.current) return
      animationCatalogRef.current = page
      setAnimationCatalog(page)
      if (detailEntryKindForActions !== 'independent_npc' || detailSourceKindForActions !== 'config_assembled') return
      const refreshedDetail = await getCharacterDetail(selectedIdForActions, catalogIdForActions, controller.signal)
      if (controller.signal.aborted || generation !== actionGeneration.current) return
      detailRef.current = refreshedDetail
      setDetail(refreshedDetail)
    }).catch((caught: unknown) => {
      if (controller.signal.aborted || generation !== actionGeneration.current) return
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
    }).finally(() => {
      if (generation === actionGeneration.current) setActionsLoading(false)
    })
  }, [catalogIdForActions, catalogReadyForActions, detailCatalogIdForActions, detailEntryKindForActions,
    detailSelectionIdForActions, detailSourceKindForActions, options.active, options.enabled, selectedIdForActions,
    sourceIdentityForActions, variantIdForActions])

  const persistAndSetOperation = useCallback((operation: StoredCharacterOperation): void => {
    persistOperation(operation)
    operationRef.current = operation
    setOperationKind(operation.kind)
    setOperationRequest(operation.request)
    setActiveJobId(operation.request.request_id)
    activeJobIdRef.current = operation.request.request_id
    jobKindRef.current = operation.kind
    setEvents([])
    eventCursor.current = 0
    setJob(null)
    jobRef.current = null
    setCancelRequested(false)
  }, [])

  const synchronizeOperation = useCallback(async (
    operation: StoredCharacterOperation,
    generation: number,
    initialJob: CharacterJob | null,
  ): Promise<void> => {
    if (typeof window === 'undefined' || generation !== jobGeneration.current) return
    const controller = new AbortController()
    jobAbort.current?.abort()
    jobAbort.current = controller
    try {
      const currentJob = initialJob ?? await getCharacterJob(
        operation.kind,
        operation.request.request_id,
        operation.request.request_id,
        controller.signal,
      )
      if (controller.signal.aborted || generation !== jobGeneration.current) return
      jobRef.current = currentJob
      setJob(currentJob)
      setCancelRequested(currentJob.cancel_requested)
      if (isTerminalCharacterJob(currentJob)) {
        setOperationPending(false)
        if (currentJob.kind === 'preparation' && (currentJob.state === 'completed' || currentJob.state === 'partial') &&
          optionsRef.current.enabled && optionsRef.current.active && preparationRefreshJobId.current !== currentJob.job_id) {
          preparationRefreshJobId.current = currentJob.job_id
          void refreshCatalog()
        }
      }
      if (cancelInFlight.current && cancelResolution.current !== 'posting' &&
        (currentJob.cancel_requested || isTerminalCharacterJob(currentJob) || cancelResolution.current === 'uncertain')) {
        cancelInFlight.current = false
        cancelResolution.current = null
        setCancelPending(false)
      }
      setError(null)
      setDiagnostic(null)
      const nextEvents: CharacterJobEvent[] = []
      let cursor = eventCursor.current
      let hasMore = true
      while (hasMore && !controller.signal.aborted && generation === jobGeneration.current) {
        const page = await getCharacterJobEvents(operation.kind, currentJob.job_id, cursor, EVENT_PAGE_LIMIT, controller.signal)
        nextEvents.push(...page.events)
        cursor = page.next_after
        hasMore = page.has_more
      }
      if (controller.signal.aborted || generation !== jobGeneration.current) return
      eventCursor.current = cursor
      setEvents((current) => mergeEvents(current, nextEvents))
      if (isTerminalCharacterJob(currentJob)) {
        setOperationPending(false)
        return
      }
      jobTimer.current = window.setTimeout(() => {
        void synchronizeOperation(operation, generation, null)
      }, OPERATION_POLL_INTERVAL)
    } catch (caught) {
      if (controller.signal.aborted || generation !== jobGeneration.current) return
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
      setConnection(errorConnection(caught))
      if (isTransientBackendFailure(caught)) {
        jobTimer.current = window.setTimeout(() => {
          void synchronizeOperation(operation, generation, null)
        }, OPERATION_RETRY_INTERVAL)
      }
    }
  }, [refreshCatalog])

  const beginPolling = useCallback((operation: StoredCharacterOperation, initialJob: CharacterJob): void => {
    if (typeof window === 'undefined') return
    if (jobTimer.current !== null) window.clearTimeout(jobTimer.current)
    jobAbort.current?.abort()
    const generation = ++jobGeneration.current
    operationRef.current = operation
    activeJobIdRef.current = initialJob.job_id
    jobKindRef.current = operation.kind
    setOperationKind(operation.kind)
    setOperationRequest(operation.request)
    setActiveJobId(initialJob.job_id)
    setJob(initialJob)
    jobRef.current = initialJob
    setCancelRequested(initialJob.cancel_requested)
    if (cancelInFlight.current && cancelResolution.current !== 'posting' &&
      (initialJob.cancel_requested || isTerminalCharacterJob(initialJob) || cancelResolution.current === 'uncertain')) {
      cancelInFlight.current = false
      cancelResolution.current = null
      setCancelPending(false)
    }
    eventCursor.current = 0
    setEvents([])
    void synchronizeOperation(operation, generation, initialJob)
  }, [synchronizeOperation])

  const reconnect = useCallback(async (): Promise<void> => {
    if (typeof window === 'undefined' || !optionsRef.current.enabled) return
    launchAbort.current?.abort()
    if (jobTimer.current !== null) window.clearTimeout(jobTimer.current)
    jobTimer.current = null
    jobAbort.current?.abort()
    jobGeneration.current += 1
    const controller = new AbortController()
    launchAbort.current = controller
    setError(null)
    setDiagnostic(null)
    let hasSavedOperation = false
    try {
      const saved = readStoredOperation()
      if (saved === null) return
      hasSavedOperation = true
      persistAndSetOperation(saved)
      setOperationPending(true)
      let currentJob: CharacterJob
      try {
        currentJob = await getCharacterJob(saved.kind, saved.request.request_id, saved.request.request_id, controller.signal)
      } catch (caught) {
        if (!(caught instanceof BackendRequestError) || caught.kind !== 'http' || caught.status !== 404) throw caught
        currentJob = await submitOperation(saved, controller.signal)
      }
      if (controller.signal.aborted) return
      setOperationPending(false)
      beginPolling(saved, currentJob)
    } catch (caught) {
      if (controller.signal.aborted) return
      setOperationPending(false)
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
      if (caught instanceof BackendRequestError) setConnection(errorConnection(caught))
      if (hasSavedOperation && isTransientBackendFailure(caught)) {
        jobTimer.current = window.setTimeout(() => {
          const recover = reconnectRef.current
          if (recover !== null) void recover()
        }, OPERATION_RETRY_INTERVAL)
      }
    }
  }, [beginPolling, persistAndSetOperation])
  reconnectRef.current = reconnect

  const validateExport = useCallback(async (
    value: CharacterExportRequest,
  ): Promise<CharacterRequestValidation | null> => {
    if (!optionsRef.current.enabled || !optionsRef.current.active) return null
    launchAbort.current?.abort()
    const controller = new AbortController()
    launchAbort.current = controller
    setValidation(null)
    setError(null)
    setDiagnostic(null)
    try {
      const request = parseCharacterExportRequestForStorage(value)
      assertExportContext(
        request,
        catalogRef.current,
        selected,
        detailRef.current,
        selectedVariant,
        animationCatalogRef.current,
      )
      const result = await validateCharacterExport(request, controller.signal)
      if (controller.signal.aborted) return null
      setValidation(result)
      return result
    } catch (caught) {
      if (controller.signal.aborted) return null
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
      return null
    }
  }, [selected, selectedVariant])

  const startExport = useCallback(async (value: CharacterExportRequest): Promise<void> => {
    if (!optionsRef.current.enabled || !optionsRef.current.active) return
    if (operationPending || (jobRef.current !== null && !isTerminalCharacterJob(jobRef.current))) {
      setError('Wait for the current character task to finish before starting another one.')
      return
    }
    launchAbort.current?.abort()
    const controller = new AbortController()
    launchAbort.current = controller
    setOperationPending(true)
    setError(null)
    setDiagnostic(null)
    let operationPersisted = false
    try {
      const request = parseCharacterExportRequestForStorage(value)
      assertExportContext(request, catalogRef.current, selected, detailRef.current, selectedVariant, animationCatalogRef.current)
      const operation: StoredCharacterOperation = { format: 'CharacterOperation/1', kind: 'export', request }
      const result = await validateCharacterExport(request, controller.signal)
      if (controller.signal.aborted) return
      setValidation(result)
      persistAndSetOperation(operation)
      operationPersisted = true
      const submitted = await submitCharacterExport(request, controller.signal)
      if (controller.signal.aborted) return
      setOperationPending(false)
      beginPolling(operation, submitted)
    } catch (caught) {
      if (controller.signal.aborted) return
      setOperationPending(false)
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
      if (operationPersisted && isTransientBackendFailure(caught)) {
        void reconnect()
      }
    }
  }, [beginPolling, operationPending, persistAndSetOperation, reconnect, selected, selectedVariant])

  const validatePreparation = useCallback(async (
    value: CharacterPreparationRequest,
  ): Promise<CharacterRequestValidation | null> => {
    if (!optionsRef.current.enabled || !optionsRef.current.active) return null
    launchAbort.current?.abort()
    const controller = new AbortController()
    launchAbort.current = controller
    setValidation(null)
    setError(null)
    setDiagnostic(null)
    try {
      const request = parseCharacterPreparationRequestForStorage(value)
      assertPreparationSource(request, optionsRef.current.preparationSource)
      const result = await validateCharacterPreparation(request, controller.signal)
      if (controller.signal.aborted) return null
      setValidation(result)
      return result
    } catch (caught) {
      if (controller.signal.aborted) return null
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
      return null
    }
  }, [])

  const prepare = useCallback(async (value: CharacterPreparationRequest): Promise<void> => {
    if (!optionsRef.current.enabled || !optionsRef.current.active) return
    if (operationPending || (jobRef.current !== null && !isTerminalCharacterJob(jobRef.current))) {
      setError('Wait for the current character task to finish before starting another one.')
      return
    }
    launchAbort.current?.abort()
    const controller = new AbortController()
    launchAbort.current = controller
    setOperationPending(true)
    setValidation(null)
    setError(null)
    setDiagnostic(null)
    let operationPersisted = false
    try {
      const request = parseCharacterPreparationRequestForStorage(value)
      assertPreparationSource(request, optionsRef.current.preparationSource)
      const operation: StoredCharacterOperation = { format: 'CharacterOperation/1', kind: 'preparation', request }
      const result = await validateCharacterPreparation(request, controller.signal)
      if (controller.signal.aborted) return
      setValidation(result)
      persistAndSetOperation(operation)
      operationPersisted = true
      const submitted = await submitCharacterPreparation(request, controller.signal)
      if (controller.signal.aborted) return
      setOperationPending(false)
      beginPolling(operation, submitted)
    } catch (caught) {
      if (controller.signal.aborted) return
      setOperationPending(false)
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
      if (operationPersisted && isTransientBackendFailure(caught)) {
        void reconnect()
      }
    }
  }, [beginPolling, operationPending, persistAndSetOperation, reconnect])

  const loadMoreActions = useCallback(async (): Promise<void> => {
    if (!optionsRef.current.enabled || !optionsRef.current.active) return
    const currentCatalog = catalogRef.current
    const currentDetail = detailRef.current
    const currentActions = animationCatalogRef.current
    const currentSelectionId = optionsRef.current.selectionId
    const currentVariantId = optionsRef.current.variantId
    if (actionsLoading || currentCatalog === null || currentCatalog.source_identity === null || currentDetail === null ||
      currentActions === null || currentSelectionId !== currentDetail.selection_id || currentVariantId !== currentActions.variant_id ||
      currentActions.animations.length >= currentActions.total) return
    actionAbort.current?.abort()
    const controller = new AbortController()
    actionAbort.current = controller
    const generation = ++actionGeneration.current
    setActionsLoading(true)
    try {
      const page = await getCharacterAnimations(
        currentDetail.selection_id,
        currentActions.variant_id,
        currentCatalog.source_identity,
        currentActions.animations.length,
        100,
        controller.signal,
      )
      if (controller.signal.aborted || generation !== actionGeneration.current) return
      const current = animationCatalogRef.current
      if (current === null || current.selection_id !== page.selection_id || current.variant_id !== page.variant_id ||
        current.source_identity !== page.source_identity) return
      const seen = new Set(current.animations.map((action) => action.action_id))
      const merged = [...current.animations, ...page.animations.filter((action) => !seen.has(action.action_id))]
      const nextCatalog = {
        ...page,
        offset: 0,
        animations: merged,
        limitations: [...new Set([...current.limitations, ...page.limitations])],
      }
      animationCatalogRef.current = nextCatalog
      setAnimationCatalog(nextCatalog)
    } catch (caught) {
      if (controller.signal.aborted || generation !== actionGeneration.current) return
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
    } finally {
      if (generation === actionGeneration.current) setActionsLoading(false)
    }
  }, [actionsLoading])

  const cancel = useCallback(async (): Promise<CharacterCancelAcknowledgement | null> => {
    if (!optionsRef.current.enabled || cancelInFlight.current) return null
    const currentJob = jobRef.current
    const currentKind = jobKindRef.current
    if (currentJob === null || currentKind === null || isTerminalCharacterJob(currentJob) ||
      currentJob.cancel_requested || cancelRequested || cancelPending) return null
    cancelAbort.current?.abort()
    const controller = new AbortController()
    cancelAbort.current = controller
    cancelInFlight.current = true
    cancelResolution.current = 'posting'
    setCancelPending(true)
    setCancelRequested(true)
    setError(null)
    setDiagnostic(null)
    try {
      const acknowledgement = await requestCharacterCancellation(currentKind, currentJob.job_id, controller.signal)
      if (controller.signal.aborted) return null
      cancelResolution.current = 'accepted'
      setCancelRequested(acknowledgement.state === 'accepted' || currentJob.cancel_requested)
      const operation = operationRef.current
      const generation = jobGeneration.current
      if (operation !== null && activeJobIdRef.current === currentJob.job_id) {
        if (typeof window !== 'undefined' && jobTimer.current !== null) window.clearTimeout(jobTimer.current)
        jobTimer.current = null
        void synchronizeOperation(operation, generation, null)
      }
      return acknowledgement
    } catch (caught) {
      if (controller.signal.aborted) return null
      cancelResolution.current = 'uncertain'
      setError(errorMessage(caught))
      setDiagnostic(diagnosticFor(caught))
      const operation = operationRef.current
      const generation = jobGeneration.current
      if (operation !== null && activeJobIdRef.current === currentJob.job_id) {
        void synchronizeOperation(operation, generation, null)
      } else {
        cancelInFlight.current = false
        cancelResolution.current = null
        setCancelPending(false)
        setCancelRequested(currentJob.cancel_requested)
      }
      return null
    }
  }, [cancelPending, cancelRequested, synchronizeOperation])

  useEffect(() => {
    if (!options.enabled || !options.active) {
      catalogAbort.current?.abort()
      detailAbort.current?.abort()
      actionAbort.current?.abort()
      setCatalogLoading(false)
      setDetailLoading(false)
      setActionsLoading(false)
    }
    if (!options.enabled) {
      launchAbort.current?.abort()
      jobAbort.current?.abort()
      cancelAbort.current?.abort()
      if (typeof window !== 'undefined' && jobTimer.current !== null) window.clearTimeout(jobTimer.current)
      jobTimer.current = null
      jobGeneration.current += 1
      restoreAttempted.current = false
      cancelInFlight.current = false
      cancelResolution.current = null
      setOperationPending(false)
      setCancelPending(false)
      return
    }
    if (options.active) void refreshCatalog()
    if (!restoreAttempted.current) {
      restoreAttempted.current = true
      void reconnect()
    }
  }, [options.active, options.enabled, reconnect, refreshCatalog])

  useEffect(() => () => {
    catalogAbort.current?.abort()
    detailAbort.current?.abort()
    actionAbort.current?.abort()
    launchAbort.current?.abort()
    jobAbort.current?.abort()
    cancelAbort.current?.abort()
    if (typeof window !== 'undefined' && jobTimer.current !== null) window.clearTimeout(jobTimer.current)
    restoreAttempted.current = false
  }, [])

  const loadMoreActionsRef = useRef(loadMoreActions)
  loadMoreActionsRef.current = loadMoreActions
  const actionCount = animationCatalog?.animations.length ?? 0
  const totalActionCount = animationCatalog?.total ?? 0
  const actions = animationCatalog?.animations ?? []
  const hasMoreActions = actionCount < totalActionCount
  const operationBusy = operationPending || (activeJobId !== null && operationRequest !== null &&
    (job === null || job.job_id !== activeJobId || !isTerminalCharacterJob(job)))
  const canStartExport = options.enabled && options.active && !operationBusy && catalog?.availability.ready === true && selected !== null && detail !== null &&
    selectedVariant !== null && detail.catalog_id === catalog.catalog_id && detail.selection_id === selected.selection_id &&
    selected.capabilities.blend_export && catalog.profiles.some((profile) => profile.profile_id === 'hgrp-preview')
  const source = options.preparationSource
  const canPrepare = options.enabled && options.active && !operationBusy && source !== null && [source.game_root, source.blender_exe, source.cache_root, source.output_root]
    .every((value) => value.trim().length > 0)
  const canCancel = options.enabled && job !== null && !isTerminalCharacterJob(job) && !job.cancel_requested &&
    !cancelRequested && !cancelPending

  return {
    connection,
    catalog,
    characterOptions,
    selected,
    detail,
    selectedVariant,
    animationCatalog,
    actions,
    catalogLoading,
    detailLoading,
    actionsLoading,
    hasMoreActions,
    error,
    diagnostic,
    validation,
    job,
    events,
    activeJobId,
    operationKind,
    operationRequest,
    operationPending,
    cancelRequested,
    cancelPending,
    operationBusy,
    canStartExport,
    canPrepare,
    canCancel,
    refreshCatalog,
    loadMoreActions: () => loadMoreActionsRef.current(),
    validateExport,
    startExport,
    validatePreparation,
    prepare,
    cancel,
    reconnect,
  }
}
