import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BackendRequestError, isJsonObject, postJson, requestJson } from '../../lib/backendClient'
import { isExportJob, isExportJobValidation } from '../../lib/mapExportJob'
import type { ExportJob } from '../../lib/mapExportJob'
import { describeSelection, isLevelMapRegistry, selectionBounds } from './levelMaps'
import type { LevelMap } from './levelMaps'
import type { PaintingResult, PaintingSelection, PaintingSettings } from './types'
import { useMapPreparation } from './useMapPreparation'

export type PaintingSource = { game: string; output: string; cache: string; blender: string }
const terminal = (job: ExportJob): boolean => ['completed', 'failed', 'cancelled'].includes(job.state)

export function paintingRequest(map: LevelMap, settings: PaintingSettings, selection: PaintingSelection, source: PaintingSource) {
  return {
    format: 'EndfieldWebUIExportJob/2', export_mode: settings.exportMode, map_id: map.mapId,
    selection: { type: 'world_bounds', ...selectionBounds(map, selection) }, batch_mode: settings.chunkMode,
    layers: { ...settings.layers, roads: settings.layers.instances, vegetation: settings.layers.instances, lights: settings.layers.effects },
    export_groups: settings.groups, effects: { selection_mode: 'full_system_by_anchor' }, water_mode: 'stable_eevee',
    source: { game_root: source.game, cache_root: source.cache, ...(source.blender.trim() ? { blender_exe: source.blender.trim() } : {}) },
    output: { root: source.output, label: settings.outputName },
  }
}

export function paintingResult(job: ExportJob, selectionLabel: string): PaintingResult | null {
  if (!terminal(job)) return null
  const build = job.result?.blenderBuild
  const pending = build?.pending ?? build?.scenes?.reduce((sum, scene) => sum + scene.pending, 0)
  const packageResult = job.result?.package
  const partial = build?.status === 'partial' || (pending !== undefined && pending > 0) ||
    packageResult?.status === 'partial' || Boolean(packageResult?.warnings?.length) ||
    (packageResult?.exportMode === 'blend' && !build?.scenes?.length) || !packageResult
  const outcome = job.state === 'failed' ? 'failure' : job.state === 'cancelled' ? 'cancel' : partial ? 'partial' : 'success'
  return {
    outcome, selectionLabel,
    subtitle: outcome === 'success' ? '导出任务完成 · 以产物报告为准' : outcome === 'partial' ? '已有产物 · 部分内容待处理' : outcome === 'cancel' ? '已在安全阶段取消' : '后端未能完成本次导出',
    primary: job.state === 'completed' ? packageResult?.output_dir ?? '后端未返回产物路径' : job.error?.message ?? job.state,
    secondary: job.state !== 'completed' ? '已写入的日志与部分产物保留' : pending === undefined ? '未返回待处理项统计，请检查导出报告' : `${pending} 项待处理；不代表完整运行时还原`,
    terminal: `JOB / ${job.job_id}\nSTATE / ${job.state.toUpperCase()}${packageResult?.warnings?.length ? '\n' + packageResult.warnings.join('\n') : ''}`,
  }
}

export function usePaintingRuntime(source: PaintingSource, sourceReady: boolean, preview: boolean) {
  const [map, setMap] = useState<LevelMap | null>(null)
  const [loading, setLoading] = useState(true)
  const [mapError, setMapError] = useState<string | null>(null)
  const [mapChecked, setMapChecked] = useState(false)
  const [mapMissing, setMapMissing] = useState(false)
  const [revision, setRevision] = useState(0)
  const [job, setJob] = useState<ExportJob | null>(null)
  const [jobError, setJobError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [submittedSelection, setSubmittedSelection] = useState('')
  const submittingRef = useRef(false)
  const cancellingRef = useRef(false)
  const activeJob = useRef<ExportJob | null>(null)
  const operationGeneration = useRef(0)
  const reload = useCallback((): void => setRevision(value => value + 1), [])
  const preparation = useMapPreparation(sourceReady && mapChecked && mapMissing && !loading, preview, reload)
  const busy = submitting || preparation.busy || Boolean(job && !terminal(job))

  useEffect(() => {
    let disposed = false
    setLoading(true)
    setMapChecked(false)
    setMapMissing(false)
    setMap(null)
    setMapError(null)
    void requestJson('/api/v2/level-maps', { method: 'GET' }, isLevelMapRegistry).then(registry => {
      if (disposed) return
      const entry = registry.entries.find(entry => entry.levelId === 'map02_lv010')
      setMapMissing(!entry)
      setMap(entry ?? null)
    }).catch((cause: unknown) => {
      if (disposed) return
      if (cause instanceof BackendRequestError && cause.status === 409 && cause.responseBody) {
        try {
          const body: unknown = JSON.parse(cause.responseBody)
          if (isJsonObject(body) && body.error === 'level_maps_not_prepared') {
            setMapMissing(true)
            return
          }
        } catch (error) {
          if (!(error instanceof SyntaxError)) throw error
        }
      }
      setMapError(cause instanceof Error ? cause.message : String(cause))
    }).finally(() => { if (!disposed) { setLoading(false); setMapChecked(true) } })
    return () => { disposed = true }
  }, [revision, sourceReady])

  useEffect(() => {
    if (!job || terminal(job) || jobError || cancelling) return
    let disposed = false
    const generation = operationGeneration.current
    const timer = window.setTimeout(() => {
      void requestJson(`/api/v2/export-jobs/${encodeURIComponent(job.job_id)}`, { method: 'GET' }, isExportJob)
        .then(next => {
          if (next.job_id !== job.job_id) throw new Error(`任务标识不匹配：请求 ${job.job_id}，返回 ${next.job_id}`)
          if (!disposed && generation === operationGeneration.current) { activeJob.current = next; setJob(next) }
        })
        .catch((cause: unknown) => { if (!disposed && generation === operationGeneration.current) setJobError(`任务状态暂不可确认，请重连或请求取消。${cause instanceof Error ? cause.message : String(cause)}`) })
    }, 700)
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [job, jobError, cancelling])

  const submit = async (settings: PaintingSettings, selection: PaintingSelection): Promise<void> => {
    if (busy || submittingRef.current || activeJob.current && !terminal(activeJob.current)) return
    submittingRef.current = true
    setSubmitting(true)
    setJob(null)
    setJobError(null)
    try {
      if (preview) throw new Error('开发预览不执行真实提取任务。')
      if (!map || !sourceReady) throw new Error('请先完成场景首次准备并加载画境地图。')
      if (map.levelId !== 'map02_lv010' || map.mapId !== 'map02' || map.artOwner !== 'map02') throw new Error('祀的画地图标识与武陵资源域不匹配。')
      const request = paintingRequest(map, settings, selection, source)
      const validation = await postJson('/api/v2/export-jobs/validate', request, isExportJobValidation)
      if (validation.status !== 'valid') throw new Error(validation.message ?? validation.error?.message ?? '画境导出请求未通过后端校验')
      setSubmittedSelection(describeSelection(map, selection))
      const next = await postJson('/api/v2/export-jobs', request, isExportJob)
      activeJob.current = next
      setJob(next)
    } catch (cause) {
      setJobError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }
  const cancel = async (): Promise<void> => {
    if (!job || terminal(job) || job.cancel_requested || cancellingRef.current) return
    cancellingRef.current = true
    operationGeneration.current += 1
    setCancelling(true)
    try {
      const next = await postJson(`/api/v2/export-jobs/${encodeURIComponent(job.job_id)}/cancel`, {}, isExportJob)
      if (next.job_id !== job.job_id) throw new Error(`取消响应任务标识不匹配：请求 ${job.job_id}，返回 ${next.job_id}`)
      activeJob.current = next
      setJob(next)
      setJobError(null)
    } catch (cause) {
      setJobError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      cancellingRef.current = false
      setCancelling(false)
    }
  }
  const reconnect = (): void => setJobError(null)
  const result = useMemo(() => job ? paintingResult(job, submittedSelection) : null, [job, submittedSelection])
  return { map, loading, mapMissing, mapError, reload, job, jobError, busy, cancelling, submit, cancel, reconnect, result, sourceReady, preview, preparation }
}

export type PaintingRuntime = ReturnType<typeof usePaintingRuntime>
