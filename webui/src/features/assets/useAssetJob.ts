import { useCallback, useEffect, useRef, useState } from 'react'
import { BackendRequestError } from '../../lib/backendClient'
import { cancelAssetJob, createAssetExportRequest, getAssetJob, isTerminalAssetJob, parseAssetExportRequest, submitAssetRequest, validateAssetRequest } from './api'
import type { AssetExportRequest, AssetExportSelection, AssetJob, AssetKind } from './types'

type RestoredOperation = { request: AssetExportRequest | null; error: string | null }
export type AssetJobController = {
  job: AssetJob | null; busy: boolean; pending: boolean; error: string | null
  cancelPending: boolean; cancelRequested: boolean
  start: (selection: AssetExportSelection) => Promise<void>
  cancel: () => Promise<void>; clear: () => void; reconnect: () => void
}
const restoreOperation = (key: string, kinds: readonly AssetKind[]): RestoredOperation => {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return { request: null, error: null }
    const request = parseAssetExportRequest(JSON.parse(raw))
    if (!kinds.includes(request.kind)) throw new TypeError('Saved asset request does not belong to this workspace')
    return { request, error: null }
  } catch (error) {
    return { request: null, error: error instanceof Error ? error.message : String(error) }
  }
}
export const useAssetJob = (allowedKinds: readonly AssetKind[]): AssetJobController => {
  const kindsKey = [...new Set(allowedKinds)].sort().join('.')
  const storageKey = `endfield.asset.operation.v1.${kindsKey}`
  const [restored] = useState(() => restoreOperation(storageKey, allowedKinds))
  const [request, setRequest] = useState<AssetExportRequest | null>(restored.request)
  const [job, setJob] = useState<AssetJob | null>(null)
  const [pending, setPending] = useState(false)
  const [cancelPending, setCancelPending] = useState(false)
  const [cancelRequested, setCancelRequested] = useState(false)
  const [error, setError] = useState<string | null>(restored.error)
  const [revision, setRevision] = useState(0)
  const locked = useRef(restored.request !== null)
  const mounted = useRef(true)
  const inFlight = useRef<AbortController | null>(null)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; inFlight.current?.abort() }
  }, [])
  const busy = pending || request !== null && (job === null || !isTerminalAssetJob(job))
  useEffect(() => {
    if (!request || pending) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | null = null
    const poll = async (): Promise<void> => {
      try {
        const next = await getAssetJob(request.request_id, controller.signal)
        if (controller.signal.aborted) return
        if (JSON.stringify(next.request) !== JSON.stringify(request)) throw new TypeError('Saved asset request differs from the backend job')
        setJob(next)
        setError(null)
        if (isTerminalAssetJob(next)) {
          localStorage.removeItem(storageKey)
          setRequest(null)
          locked.current = false
          return
        }
        timer = setTimeout(() => { void poll() }, 1200)
      } catch (error) {
        if (!controller.signal.aborted) setError(error instanceof Error ? error.message : String(error))
      }
    }
    void poll()
    return () => { controller.abort(); if (timer !== null) clearTimeout(timer) }
  }, [request, pending, storageKey, revision])
  const start = useCallback(async (selection: AssetExportSelection): Promise<void> => {
    if (locked.current) return
    if (!kindsKey.split('.').includes(selection.kind)) throw new TypeError('Asset kind does not belong to this workspace')
    locked.current = true
    setPending(true)
    setError(null)
    setJob(null)
    setCancelRequested(false)
    const controller = new AbortController()
    inFlight.current = controller
    let submissionStarted = false
    try {
      const operation = createAssetExportRequest(selection)
      await validateAssetRequest(operation, controller.signal)
      if (!mounted.current) return
      localStorage.setItem(storageKey, JSON.stringify(operation))
      setRequest(operation)
      submissionStarted = true
      const next = await submitAssetRequest(operation, controller.signal)
      if (mounted.current) setJob(next)
    } catch (error) {
      if (mounted.current) setError(error instanceof Error ? error.message : String(error))
      const rejected = error instanceof BackendRequestError && error.kind === 'http' && error.status !== null && error.status >= 400 && error.status < 500
      if (!submissionStarted || rejected) {
        locked.current = false
        if (rejected) {
          localStorage.removeItem(storageKey)
          if (mounted.current) setRequest(null)
        }
      }
    } finally {
      inFlight.current = null
      if (mounted.current) setPending(false)
    }
  }, [kindsKey, storageKey])
  const cancel = useCallback(async (): Promise<void> => {
    if (!request || pending || cancelPending || job !== null && isTerminalAssetJob(job)) return
    setCancelPending(true)
    const controller = new AbortController()
    try {
      await cancelAssetJob(request.request_id, controller.signal)
      if (!mounted.current) return
      setCancelRequested(true)
      setError(null)
      setRevision(value => value + 1)
    } catch (error) {
      if (mounted.current) setError(error instanceof Error ? error.message : String(error))
    } finally {
      if (mounted.current) setCancelPending(false)
    }
  }, [request, pending, cancelPending, job])
  const clear = useCallback((): void => {
    if (locked.current) return
    localStorage.removeItem(storageKey)
    setRequest(null)
    setJob(null)
    setError(null)
    setCancelRequested(false)
  }, [storageKey])
  const reconnect = useCallback((): void => { setError(null); setRevision(value => value + 1) }, [])
  return { job, busy, pending, error, cancelPending, cancelRequested, start, cancel, clear, reconnect }
}
