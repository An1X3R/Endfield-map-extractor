import type { FirstRunCanonicalRequest, FirstRunRequest, FirstRunStatus } from '../types'

export function firstRunRequestMatchesPrepared(request: FirstRunRequest, prepared: FirstRunCanonicalRequest | undefined): boolean {
  return Boolean(prepared && request.game_root === prepared.game_root && request.export_root === prepared.export_root &&
    request.cache_root === prepared.cache_root && request.run_name === prepared.run_name &&
    (request.blender_exe ?? '') === (prepared.blender_exe ?? ''))
}

export function canStartFirstRunRequest(status: FirstRunStatus | null, request: FirstRunRequest): boolean {
  if (!status || status.state === 'queued' || status.state === 'running') return false
  return status.actions.can_start || status.ready && !firstRunRequestMatchesPrepared(request, status.request)
}
