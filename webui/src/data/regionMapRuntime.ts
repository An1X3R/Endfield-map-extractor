import runtimeManifestJson from './region_map_runtime_manifest.json'
import type {
  MapId,
  RuntimeManifest,
  RuntimeMap,
} from '../lib/mapChunking'

export type RuntimeOverviewAsset = {
  apiUrl: string
  sha256: string
  pixelWidth: number
  pixelHeight: number
}

export type RuntimeMapWithOverview = RuntimeMap & {
  overview: {
    imageOrientation: '+Z north/up; -X west/left'
    assetPolicy: string
    clean: RuntimeOverviewAsset
    sectorOverlay: RuntimeOverviewAsset
  }
}

export type RegionMapRuntimeManifest = Omit<RuntimeManifest, 'maps'> & {
  coordinateConvention: {
    bounds: string
    world: string
    image: string
    streamingSector: string
    blender: string
    hTile: string
  }
  sourceEvidence: Record<string, string>
  maps: Record<MapId, RuntimeMapWithOverview>
}

const manifest = runtimeManifestJson as RegionMapRuntimeManifest

if (
  manifest.format !== 'EndfieldRegionMapRuntimeManifest/1' ||
  manifest.resolverVersion !== 'endfield-sector128-v1'
) {
  throw new Error('Unsupported Endfield region-map runtime manifest')
}

export const REGION_MAP_RUNTIME_MANIFEST = manifest

export const getRuntimeMap = (mapId: MapId) => manifest.maps[mapId]

export const shouldRequestRuntimeOverview = true

export const resolveRuntimeAssetUrl = (apiUrl: string) => {
  const configuredOrigin = import.meta.env.VITE_MAP_API_ORIGIN?.trim()
  if (!configuredOrigin) return apiUrl
  return `${configuredOrigin.replace(/\/$/, '')}${apiUrl}`
}
