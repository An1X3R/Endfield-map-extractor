export type MapId = 'map01' | 'map02'
export type ChunkMode = 'per_sector' | 'cluster_4x4_sectors' | 'merged_selection'

export type BoundsXZ = {
  xmin: number
  xmax: number
  zmin: number
  zmax: number
}

export type RuntimeLevel = {
  levelId: string
  idNum: number
  worldRect: BoundsXZ
  hTileGrid: { x: number; z: number }
  ownershipSectorCount: number
}

export type RuntimeMap = {
  mapId: MapId
  domainId: string
  worldBounds: BoundsXZ
  sideSectors: number
  sectorSizeMetres: 128
  ownershipGridOrder: 'rows south-to-north; columns west-to-east'
  ownershipGrid: Array<Array<string | null>>
  levels: RuntimeLevel[]
}

export type RuntimeManifest = {
  format: 'EndfieldRegionMapRuntimeManifest/1'
  resolverVersion: 'endfield-sector128-v1'
  maps: Record<MapId, RuntimeMap>
}

export type SectorSelection = {
  key: string
  sectorX: number
  sectorZ: number
  unityBoundsXZ: BoundsXZ
  blenderBoundsXY: { xmin: number; xmax: number; ymin: number; ymax: number }
  levelId: string | null
  hTile: { levelId: string; tileX: number; tileY: number } | null
  coverage: 'mapped' | 'unmapped'
}

export type ExportBatch = {
  id: string
  mode: ChunkMode
  sectorKeys: string[]
  selectedBounds: BoundsXZ
  alignedClusterBounds: BoundsXZ | null
}

export type ChunkSelection = {
  format: 'EndfieldChunkSelection/1'
  resolverVersion: 'endfield-sector128-v1'
  mapId: MapId
  requestedBounds: BoundsXZ
  canonicalBounds: BoundsXZ
  sectorSizeMetres: 128
  sectors: SectorSelection[]
  batches: ExportBatch[]
}

export type ScreenPoint = { x: number; y: number }
export type ScreenViewport = { x: number; y: number; width: number; height: number }

const GRID = 128

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value))

const assertFiniteBounds = (bounds: BoundsXZ) => {
  const values = [bounds.xmin, bounds.xmax, bounds.zmin, bounds.zmax]
  if (!values.every(Number.isFinite)) throw new Error('Selection bounds must be finite')
  if (bounds.xmin === bounds.xmax || bounds.zmin === bounds.zmax) {
    throw new Error('Selection must have non-zero area')
  }
}

const unionBounds = (items: SectorSelection[]): BoundsXZ => ({
  xmin: Math.min(...items.map((item) => item.unityBoundsXZ.xmin)),
  xmax: Math.max(...items.map((item) => item.unityBoundsXZ.xmax)),
  zmin: Math.min(...items.map((item) => item.unityBoundsXZ.zmin)),
  zmax: Math.max(...items.map((item) => item.unityBoundsXZ.zmax)),
})

export function screenDragToWorldBounds(
  map: RuntimeMap,
  start: ScreenPoint,
  end: ScreenPoint,
  viewport: ScreenViewport,
): BoundsXZ {
  if (viewport.width <= 0 || viewport.height <= 0) throw new Error('Invalid viewport')
  const left = clamp(Math.min(start.x, end.x), viewport.x, viewport.x + viewport.width)
  const right = clamp(Math.max(start.x, end.x), viewport.x, viewport.x + viewport.width)
  const top = clamp(Math.min(start.y, end.y), viewport.y, viewport.y + viewport.height)
  const bottom = clamp(Math.max(start.y, end.y), viewport.y, viewport.y + viewport.height)
  const u0 = (left - viewport.x) / viewport.width
  const u1 = (right - viewport.x) / viewport.width
  const v0 = (top - viewport.y) / viewport.height
  const v1 = (bottom - viewport.y) / viewport.height
  const worldWidth = map.worldBounds.xmax - map.worldBounds.xmin
  const worldHeight = map.worldBounds.zmax - map.worldBounds.zmin
  return {
    xmin: map.worldBounds.xmin + u0 * worldWidth,
    xmax: map.worldBounds.xmin + u1 * worldWidth,
    zmin: map.worldBounds.zmax - v1 * worldHeight,
    zmax: map.worldBounds.zmax - v0 * worldHeight,
  }
}

export function snapSelection(map: RuntimeMap, requested: BoundsXZ): BoundsXZ {
  assertFiniteBounds(requested)
  const normalized: BoundsXZ = {
    xmin: Math.min(requested.xmin, requested.xmax),
    xmax: Math.max(requested.xmin, requested.xmax),
    zmin: Math.min(requested.zmin, requested.zmax),
    zmax: Math.max(requested.zmin, requested.zmax),
  }
  const clipped: BoundsXZ = {
    xmin: clamp(normalized.xmin, map.worldBounds.xmin, map.worldBounds.xmax),
    xmax: clamp(normalized.xmax, map.worldBounds.xmin, map.worldBounds.xmax),
    zmin: clamp(normalized.zmin, map.worldBounds.zmin, map.worldBounds.zmax),
    zmax: clamp(normalized.zmax, map.worldBounds.zmin, map.worldBounds.zmax),
  }
  if (clipped.xmin >= clipped.xmax || clipped.zmin >= clipped.zmax) {
    throw new Error('Selection is outside the map world bounds')
  }
  return {
    xmin: Math.max(map.worldBounds.xmin, Math.floor(clipped.xmin / GRID) * GRID),
    xmax: Math.min(map.worldBounds.xmax, Math.ceil(clipped.xmax / GRID) * GRID),
    zmin: Math.max(map.worldBounds.zmin, Math.floor(clipped.zmin / GRID) * GRID),
    zmax: Math.min(map.worldBounds.zmax, Math.ceil(clipped.zmax / GRID) * GRID),
  }
}

export function sectorToImageBounds(
  map: RuntimeMap,
  sectorX: number,
  sectorZ: number,
  imageWidth: number,
  imageHeight: number,
) {
  const world = {
    xmin: sectorX * GRID,
    xmax: (sectorX + 1) * GRID,
    zmin: sectorZ * GRID,
    zmax: (sectorZ + 1) * GRID,
  }
  const sx = imageWidth / (map.worldBounds.xmax - map.worldBounds.xmin)
  const sz = imageHeight / (map.worldBounds.zmax - map.worldBounds.zmin)
  return {
    xmin: (world.xmin - map.worldBounds.xmin) * sx,
    xmax: (world.xmax - map.worldBounds.xmin) * sx,
    ymin: (map.worldBounds.zmax - world.zmax) * sz,
    ymax: (map.worldBounds.zmax - world.zmin) * sz,
  }
}

function resolveSector(map: RuntimeMap, sectorX: number, sectorZ: number): SectorSelection {
  const originSectorX = map.worldBounds.xmin / GRID
  const originSectorZ = map.worldBounds.zmin / GRID
  const column = sectorX - originSectorX
  const row = sectorZ - originSectorZ
  const levelId = map.ownershipGrid[row]?.[column] ?? null
  const level = levelId ? map.levels.find((item) => item.levelId === levelId) ?? null : null
  const unityBoundsXZ = {
    xmin: sectorX * GRID,
    xmax: (sectorX + 1) * GRID,
    zmin: sectorZ * GRID,
    zmax: (sectorZ + 1) * GRID,
  }
  const hTile = level
    ? {
        levelId: level.levelId,
        tileX: sectorX - level.worldRect.xmin / GRID + 1,
        tileY: sectorZ - level.worldRect.zmin / GRID + 1,
      }
    : null
  return {
    key: `sector:${sectorX}:${sectorZ}`,
    sectorX,
    sectorZ,
    unityBoundsXZ,
    blenderBoundsXY: {
      xmin: unityBoundsXZ.xmin,
      xmax: unityBoundsXZ.xmax,
      ymin: -unityBoundsXZ.zmax,
      ymax: -unityBoundsXZ.zmin,
    },
    levelId,
    hTile,
    coverage: level ? 'mapped' : 'unmapped',
  }
}

function makeBatches(sectors: SectorSelection[], mode: ChunkMode): ExportBatch[] {
  if (mode === 'merged_selection') {
    return [{
      id: 'merged:selection',
      mode,
      sectorKeys: sectors.map((item) => item.key),
      selectedBounds: unionBounds(sectors),
      alignedClusterBounds: null,
    }]
  }
  if (mode === 'per_sector') {
    return sectors.map((item) => ({
      id: item.key,
      mode,
      sectorKeys: [item.key],
      selectedBounds: item.unityBoundsXZ,
      alignedClusterBounds: null,
    }))
  }
  const groups = new Map<string, SectorSelection[]>()
  for (const sector of sectors) {
    const clusterX = Math.floor(sector.sectorX / 4)
    const clusterZ = Math.floor(sector.sectorZ / 4)
    const id = `cluster4:${clusterX}:${clusterZ}`
    groups.set(id, [...(groups.get(id) ?? []), sector])
  }
  return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([id, items]) => {
    const [, clusterXText, clusterZText] = id.split(':')
    const clusterX = Number(clusterXText)
    const clusterZ = Number(clusterZText)
    return {
      id,
      mode,
      sectorKeys: items.map((item) => item.key),
      selectedBounds: unionBounds(items),
      alignedClusterBounds: {
        xmin: clusterX * 4 * GRID,
        xmax: (clusterX + 1) * 4 * GRID,
        zmin: clusterZ * 4 * GRID,
        zmax: (clusterZ + 1) * 4 * GRID,
      },
    }
  })
}

export function resolveSelection(
  manifest: RuntimeManifest,
  mapId: MapId,
  requestedBounds: BoundsXZ,
  mode: ChunkMode = 'per_sector',
): ChunkSelection {
  const map = manifest.maps[mapId]
  const canonicalBounds = snapSelection(map, requestedBounds)
  const sectors: SectorSelection[] = []
  for (let sectorZ = canonicalBounds.zmin / GRID; sectorZ < canonicalBounds.zmax / GRID; sectorZ += 1) {
    for (let sectorX = canonicalBounds.xmin / GRID; sectorX < canonicalBounds.xmax / GRID; sectorX += 1) {
      sectors.push(resolveSector(map, sectorX, sectorZ))
    }
  }
  return {
    format: 'EndfieldChunkSelection/1',
    resolverVersion: manifest.resolverVersion,
    mapId,
    requestedBounds,
    canonicalBounds,
    sectorSizeMetres: GRID,
    sectors,
    batches: makeBatches(sectors, mode),
  }
}
