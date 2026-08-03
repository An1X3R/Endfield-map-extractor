import { resolveRuntimeAssetUrl } from './regionMapRuntime'
import type { MapId } from '../types'

export type RuntimeLayerId = 'instances' | 'water' | 'effects' | 'lights' | 'roads'

export type RuntimeLayerRecord = {
  recordType?: string
  mapId?: MapId
  domainId?: string
  bindingStatus?: 'mapped' | 'candidate' | 'unmapped' | string
  sectorKeys?: string[]
  positionUnity?: { x: number; y: number | null; z: number }
  positionBlender?: { x: number; y: number | null; z: number }
  boundsUnity?: {
    xmin: number | null
    xmax: number | null
    ymin: number | null
    ymax: number | null
    zmin: number | null
    zmax: number | null
  } | null
  geometry?: {
    type?: string
    coordinatesUnityXZ?: unknown
    worldRectUnityXZ?: RuntimeLayerRecord['boundsUnity']
    projectionPlane?: string
    footprintMeaning?: string
    flowDirectionUnityXZ?: unknown
  }
  displayClass?: string
  category?: string
  effectClass?: string
  effectGroup?: string
  waterType?: string
  geometryStatus?: string
  worldResolution?: string
  spatialLevelId?: string
  sourceLevelId?: string
  spatialLevelIds?: string[]
  anchorId?: string
  anchorSelectionPolicy?: string
  fullSystemGroup?: string
  instanceId?: string
  effectId?: string
  waterId?: string
  assetId?: string
  prefabName?: string
  surfaceY?: number | null
  confidence?: number
  levelBindingStatus?: string
}

export type RuntimeLayerResponse = {
  format: 'EndfieldMapLayerResponse/1'
  datasetFingerprint: string
  manifestFormat: string
  generatorVersion: string
  resolverVersion: string
  mapId: MapId
  layer: RuntimeLayerId
  meta: {
    scanStatus: string
    recordCount: number
    pendingCount: number
    geometryTypes: string[]
    bindingCounts?: Record<string, number>
    assetResolutionCounts?: Record<string, number> | null
    assetResolutionInstanceCounts?: Record<string, number> | null
    assetPendingCount?: number | null
    assetPendingInstanceCount?: number | null
    [key: string]: unknown
  }
  filter: {
    sectorKeys: string[]
    sampled: boolean
    limit: number
    [key: string]: unknown
  }
  returnedCount: number
  totalMatchingHint: number
  truncated: boolean
  records: RuntimeLayerRecord[]
}

const responseCache = new Map<string, RuntimeLayerResponse>()
let activeDatasetFingerprint: string | null = null

const buildLayerUrl = (mapId: MapId, layer: RuntimeLayerId, sectorKeys: string[], limit: number) => {
  const params = new URLSearchParams()
  for (const sectorKey of sectorKeys.slice(0, 256)) params.append('sectorKey', sectorKey)
  params.set('limit', String(Math.min(5000, Math.max(1, limit))))
  return resolveRuntimeAssetUrl(`/api/v3/maps/${mapId}/layers/${layer}?${params.toString()}`)
}

export async function fetchRuntimeLayer({
  mapId,
  layer,
  sectorKeys = [],
  limit = 1800,
  signal,
}: {
  mapId: MapId
  layer: RuntimeLayerId
  sectorKeys?: string[]
  limit?: number
  signal?: AbortSignal
}) {
  const key = `${mapId}:${layer}:${sectorKeys.slice(0, 256).join(',')}:${limit}`
  const cached = responseCache.get(key)
  if (cached) return cached

  const response = await fetch(buildLayerUrl(mapId, layer, sectorKeys, limit), {
    signal,
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  })
  if (!response.ok) throw new Error(`Layer endpoint returned ${response.status}`)
  const payload = (await response.json()) as RuntimeLayerResponse
  const validFingerprint = /^[0-9A-F]{64}$/.test(payload.datasetFingerprint)
  if (activeDatasetFingerprint === null && validFingerprint) {
    activeDatasetFingerprint = payload.datasetFingerprint
  }
  if (
    payload.format !== 'EndfieldMapLayerResponse/1' ||
    !validFingerprint ||
    payload.datasetFingerprint !== activeDatasetFingerprint ||
    payload.mapId !== mapId ||
    payload.layer !== layer
  ) {
    throw new Error('Unsupported Stage119 layer response')
  }
  responseCache.set(key, payload)
  while (responseCache.size > 64) responseCache.delete(responseCache.keys().next().value as string)
  return payload
}

export function clearRuntimeLayerCache() {
  responseCache.clear()
  activeDatasetFingerprint = null
}
