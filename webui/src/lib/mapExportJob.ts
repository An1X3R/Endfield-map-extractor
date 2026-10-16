import type { BlenderBuildResult, ExportMode } from '../types'
import { isJsonObject } from './backendClient'

export type ExportJob = {
  job_id: string
  state: 'queued' | 'preparing' | 'extracting' | 'building' | 'auditing' | 'completed' | 'failed' | 'cancelled'
  phase: string
  progress: number
  cancel_requested?: boolean
  result?: {
    package?: { output_dir?: string; status?: string; exportMode?: ExportMode; warnings?: string[] }
    blenderBuild?: BlenderBuildResult
  } | null
  error?: { message?: string; type?: string } | null
}

export type ExportJobEventsResponse = {
  events: Array<{ sequence?: number; message?: string }>
}

export type ExportJobValidation = {
  status: string
  message?: string
  error?: { message?: string }
}

const isBlenderBuildResult = (value: unknown): value is BlenderBuildResult =>
  isJsonObject(value) && typeof value.status === 'string' &&
  (value.scheduled === undefined || typeof value.scheduled === 'boolean') &&
  (value.pending === undefined || typeof value.pending === 'number') &&
  (value.scenes === undefined || Array.isArray(value.scenes) && value.scenes.every((scene) =>
    isJsonObject(scene) && typeof scene.path === 'string' && typeof scene.audit === 'string' &&
    typeof scene.plan === 'string' && typeof scene.bytes === 'number' &&
    typeof scene.sha256 === 'string' && typeof scene.instances === 'number' &&
    typeof scene.pending === 'number'
  ))

export const isExportJob = (value: unknown): value is ExportJob => {
  const states = ['queued', 'preparing', 'extracting', 'building', 'auditing', 'completed', 'failed', 'cancelled']
  if (!isJsonObject(value) || typeof value.job_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value.job_id) || typeof value.state !== 'string' ||
    !states.includes(value.state) || typeof value.phase !== 'string' || typeof value.progress !== 'number' || !Number.isFinite(value.progress) || value.progress < 0 || value.progress > 1 ||
    (value.cancel_requested !== undefined && typeof value.cancel_requested !== 'boolean')) return false
  if (value.result !== undefined && value.result !== null) {
    if (!isJsonObject(value.result)) return false
    if (value.result.package !== undefined && value.result.package !== null) {
      const resultPackage = value.result.package
      if (!isJsonObject(resultPackage) ||
        (resultPackage.output_dir !== undefined && typeof resultPackage.output_dir !== 'string') ||
        (resultPackage.status !== undefined && typeof resultPackage.status !== 'string') ||
        (resultPackage.exportMode !== undefined && resultPackage.exportMode !== 'blend' && resultPackage.exportMode !== 'data_package') ||
        (resultPackage.warnings !== undefined && (!Array.isArray(resultPackage.warnings) || !resultPackage.warnings.every((item) => typeof item === 'string')))) return false
    }
    if (value.result.blenderBuild !== undefined && value.result.blenderBuild !== null && !isBlenderBuildResult(value.result.blenderBuild)) return false
  }
  if (value.error !== undefined && value.error !== null && (!isJsonObject(value.error) ||
    (value.error.message !== undefined && typeof value.error.message !== 'string') ||
    (value.error.type !== undefined && typeof value.error.type !== 'string'))) return false
  return true
}

export const isExportJobEventsResponse = (value: unknown): value is ExportJobEventsResponse =>
  isJsonObject(value) && Array.isArray(value.events) && value.events.every((event) =>
    isJsonObject(event) &&
    (event.sequence === undefined || typeof event.sequence === 'number') &&
    (event.message === undefined || typeof event.message === 'string')
  )

export const isExportJobValidation = (value: unknown): value is ExportJobValidation =>
  isJsonObject(value) && typeof value.status === 'string' &&
  (value.message === undefined || typeof value.message === 'string') &&
  (value.error === undefined || isJsonObject(value.error) &&
    (value.error.message === undefined || typeof value.error.message === 'string'))
