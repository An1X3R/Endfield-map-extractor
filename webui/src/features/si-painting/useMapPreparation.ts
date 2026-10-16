import { useCallback, useEffect, useRef, useState } from 'react'
import { isJsonObject, postJson, requestJson } from '../../lib/backendClient'

export type MapPreparationJob = {
  format: 'EndfieldLevelMapPreparationJob/1'
  job_id: string; level_id: string; catalog_id: string
  state: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  phase: string; progress: number; message: string; cancel_requested: boolean
  error: { type: string; message: string } | null
  result: { level_id: string; reused: boolean; manifest: string; runtime: string } | null
}
export type MapPreparationCatalog = {
  format: 'EndfieldLevelMapCatalog/1'; catalogId: string
  entries: { levelId: string; mapId: string; artOwner: string; tileCount: number; needInverseXZ: boolean; status: string }[]
  preparedLevelIds: string[]; activeJob: MapPreparationJob | null
}
const levelId = (value: unknown): value is string => typeof value === 'string' && /^map\d{2}_lv\d{3}$/.test(value)
const digest = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
export const mapPreparationTerminal = (job: MapPreparationJob): boolean => ['completed', 'failed', 'cancelled'].includes(job.state)
export const isMapPreparationJob = (value: unknown): value is MapPreparationJob => isJsonObject(value) &&
  value.format === 'EndfieldLevelMapPreparationJob/1' && typeof value.job_id === 'string' && /^[a-f0-9]{32}$/.test(value.job_id) &&
  levelId(value.level_id) && digest(value.catalog_id) && typeof value.state === 'string' &&
  ['queued', 'running', 'completed', 'failed', 'cancelled'].includes(value.state) && typeof value.phase === 'string' &&
  typeof value.message === 'string' && typeof value.progress === 'number' && Number.isFinite(value.progress) && value.progress >= 0 && value.progress <= 1 &&
  typeof value.cancel_requested === 'boolean' && (value.error === null || isJsonObject(value.error) && typeof value.error.type === 'string' && typeof value.error.message === 'string') &&
  (value.result === null || isJsonObject(value.result) && value.result.level_id === value.level_id && typeof value.result.reused === 'boolean' && typeof value.result.manifest === 'string' && typeof value.result.runtime === 'string') &&
  (value.state !== 'completed' || value.result !== null && value.progress === 1) && (value.state !== 'failed' || value.error !== null)
export const isMapPreparationCatalog = (value: unknown): value is MapPreparationCatalog => {
  if (!isJsonObject(value) || value.format !== 'EndfieldLevelMapCatalog/1' || !digest(value.catalogId) ||
    !Array.isArray(value.entries) || value.entries.length === 0 || !Array.isArray(value.preparedLevelIds)) return false
  const entries = value.entries
  return entries.every(entry => isJsonObject(entry) && levelId(entry.levelId) && entry.mapId === entry.levelId.split('_')[0] && entry.artOwner === entry.mapId &&
    Number.isSafeInteger(entry.tileCount) && Number(entry.tileCount) > 0 && typeof entry.needInverseXZ === 'boolean' &&
    entry.status === (entry.needInverseXZ ? 'unsupported_inverse_xz' : 'discovered')) &&
  new Set(entries.map(entry => entry.levelId)).size === entries.length &&
  value.preparedLevelIds.every(id => levelId(id) && entries.some(entry => entry.levelId === id)) &&
  (value.activeJob === null || isMapPreparationJob(value.activeJob) && value.activeJob.catalog_id === value.catalogId)
}

export function useMapPreparation(enabled: boolean, preview: boolean, onPrepared: () => void) {
  const [catalog, setCatalog] = useState<MapPreparationCatalog | null>(null)
  const [selectedLevel, setSelectedLevel] = useState('')
  const [job, setJob] = useState<MapPreparationJob | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [revision, setRevision] = useState(0)
  const active = useRef<MapPreparationJob | null>(null)
  const mutation = useRef(false)
  const generation = useRef(0)
  const busy = submitting || Boolean(job && !mapPreparationTerminal(job))

  useEffect(() => {
    if (!enabled) return
    let disposed = false
    setLoading(true)
    setError(null)
    void requestJson('/api/v2/level-map-preparation', { method: 'GET' }, isMapPreparationCatalog).then(next => {
      if (disposed) return
      setCatalog(next)
      setSelectedLevel(previous => next.entries.some(entry => entry.levelId === previous) ? previous : next.entries.find(entry => !entry.needInverseXZ)?.levelId ?? '')
      if (next.activeJob && !mapPreparationTerminal(next.activeJob)) { active.current = next.activeJob; setJob(next.activeJob) }
    }).catch((cause: unknown) => { if (!disposed) setError(cause instanceof Error ? cause.message : String(cause)) })
      .finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [enabled, revision])

  useEffect(() => {
    if (!job || mapPreparationTerminal(job) || error || cancelling) return
    let disposed = false
    const currentGeneration = generation.current
    const timer = window.setTimeout(() => {
      void requestJson(`/api/v2/level-map-preparation/${job.job_id}`, { method: 'GET' }, isMapPreparationJob).then(next => {
        if (next.job_id !== job.job_id || next.catalog_id !== job.catalog_id || next.level_id !== job.level_id) throw new Error('地图准备状态返回了不同的任务标识。')
        if (disposed || currentGeneration !== generation.current) return
        active.current = next; setJob(next)
        if (next.state === 'completed') { onPrepared(); setRevision(value => value + 1) }
      }).catch((cause: unknown) => { if (!disposed && currentGeneration === generation.current) setError(`地图任务状态暂不可确认，请重连或取消。${cause instanceof Error ? cause.message : String(cause)}`) })
    }, 700)
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [job, error, cancelling, onPrepared])

  const start = async (): Promise<void> => {
    if (mutation.current || active.current && !mapPreparationTerminal(active.current)) return
    mutation.current = true; setSubmitting(true); setError(null)
    try {
      if (preview) throw new Error('开发预览不执行真实地图准备。')
      if (!enabled || !catalog || !catalog.entries.some(entry => entry.levelId === selectedLevel && !entry.needInverseXZ)) throw new Error('请先读取当前来源的可准备地图目录。')
      const next = await postJson('/api/v2/level-map-preparation', { format: 'EndfieldLevelMapPreparationRequest/1', catalog_id: catalog.catalogId, level_id: selectedLevel }, isMapPreparationJob)
      if (next.level_id !== selectedLevel || next.catalog_id !== catalog.catalogId) throw new Error('地图准备响应与所选地图来源不一致。')
      active.current = next; setJob(next)
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
    finally { mutation.current = false; setSubmitting(false) }
  }
  const cancel = async (): Promise<void> => {
    if (mutation.current || !job || mapPreparationTerminal(job) || job.cancel_requested) return
    mutation.current = true; generation.current += 1; setCancelling(true)
    try {
      const next = await postJson(`/api/v2/level-map-preparation/${job.job_id}/cancel`, {}, isMapPreparationJob)
      if (next.job_id !== job.job_id || next.catalog_id !== job.catalog_id || next.level_id !== job.level_id) throw new Error('地图取消响应返回了不同的任务标识。')
      active.current = next; setJob(next); setError(null)
      if (next.state === 'completed') onPrepared()
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) }
    finally { mutation.current = false; setCancelling(false) }
  }
  const refresh = useCallback((): void => { setError(null); setRevision(value => value + 1) }, [])
  const reconnect = (): void => setError(null)
  return { catalog, selectedLevel, setSelectedLevel, job, error, loading, busy, cancelling, submitting, start, cancel, refresh, reconnect }
}
