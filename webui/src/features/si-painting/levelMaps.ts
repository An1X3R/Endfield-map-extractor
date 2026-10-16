import { isJsonObject } from '../../lib/backendClient'
import type { BoundsXZ, MapId } from '../../lib/mapChunking'
import type { PaintingSelection } from './types'

type Matrix3 = [[number, number, number], [number, number, number], [number, number, number]]
export type LevelMap = {
  levelId: string
  mapId: MapId
  artOwner: string
  worldBounds: BoundsXZ
  sourceMapBounds: BoundsXZ
  worldToPixel: Matrix3
  pixelToWorld: Matrix3
  selectionGrid: {
    cellSizeMetres: number
    origin: { x: number; z: number }
    columns: number
    rows: number
    indexBase: 0
    cellIdBase: 1
    rowDirection: 'south-to-north'
    columnDirection: 'west-to-east'
  }
  image: { url: string; pixelWidth: number; pixelHeight: number; sha256: string; bytes: number }
}
export type LevelMapRegistry = { format: 'EndfieldLevelMaps/1'; status: 'prepared'; entries: LevelMap[] }

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const positiveInteger = (value: unknown): value is number => finite(value) && Number.isSafeInteger(value) && value > 0
const bounds = (value: unknown): value is BoundsXZ => isJsonObject(value) &&
  finite(value.xmin) && finite(value.xmax) && finite(value.zmin) && finite(value.zmax) &&
  value.xmax > value.xmin && value.zmax > value.zmin
const matrix = (value: unknown): value is Matrix3 => Array.isArray(value) && value.length === 3 &&
  value.every(row => Array.isArray(row) && row.length === 3 && row.every(finite)) &&
  value[2][0] === 0 && value[2][1] === 0 && value[2][2] === 1
const close = (a: number, b: number): boolean => Math.abs(a - b) < 1e-5

export function transformPoint(matrix: Matrix3, x: number, y: number): { x: number; y: number } {
  return { x: matrix[0][0] * x + matrix[0][1] * y + matrix[0][2], y: matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] }
}

function isLevelMap(value: unknown): value is LevelMap {
  if (!isJsonObject(value) || typeof value.levelId !== 'string' || !/^map\d{2}_lv\d{3}$/.test(value.levelId) ||
    (value.mapId !== 'map01' && value.mapId !== 'map02') || value.levelId.split('_')[0] !== value.mapId || value.artOwner !== value.mapId ||
    !bounds(value.worldBounds) || !bounds(value.sourceMapBounds) || !matrix(value.worldToPixel) || !matrix(value.pixelToWorld) ||
    !isJsonObject(value.selectionGrid) || !isJsonObject(value.image)) return false
  const grid = value.selectionGrid
  const image = value.image
  if (grid.format !== 'EndfieldLevelMapSelectionGrid/1' || grid.levelId !== value.levelId || grid.mapId !== value.mapId || grid.artOwner !== value.artOwner ||
    !isJsonObject(grid.origin) || !finite(grid.origin.x) || !finite(grid.origin.z) || !positiveInteger(grid.cellSizeMetres) ||
    !positiveInteger(grid.columns) || !positiveInteger(grid.rows) || grid.indexBase !== 0 || grid.cellIdBase !== 1 ||
    grid.rowDirection !== 'south-to-north' || grid.columnDirection !== 'west-to-east' ||
    grid.cellIdFormula !== '1 + row * columns + column' || !bounds(grid.worldBounds) ||
    image.url !== `/level-maps/${value.levelId}/image` || !positiveInteger(image.pixelWidth) || !positiveInteger(image.pixelHeight) ||
    !positiveInteger(image.bytes) || typeof image.sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(image.sha256)) return false
  const world = value.worldBounds
  // Selection math assumes north-up, axis-aligned map images.
  if (value.worldToPixel[0][0] <= 0 || value.worldToPixel[1][1] >= 0 ||
    value.worldToPixel[0][1] !== 0 || value.worldToPixel[1][0] !== 0) return false
  if (!(['xmin', 'xmax', 'zmin', 'zmax'] as const).every(key => close(world[key], (grid.worldBounds as BoundsXZ)[key])) ||
    !close(grid.origin.x, world.xmin) || !close(grid.origin.z, world.zmin) ||
    !close(grid.columns * grid.cellSizeMetres, world.xmax - world.xmin) || !close(grid.rows * grid.cellSizeMetres, world.zmax - world.zmin)) return false
  const nw = transformPoint(value.worldToPixel, world.xmin, world.zmax)
  const se = transformPoint(value.worldToPixel, world.xmax, world.zmin)
  if (!close(nw.x, 0) || !close(nw.y, 0) || !close(se.x, image.pixelWidth) || !close(se.y, image.pixelHeight)) return false
  const pixelToWorld = value.pixelToWorld
  const worldToPixel = value.worldToPixel
  return [[0, 0], [image.pixelWidth, 0], [0, image.pixelHeight], [image.pixelWidth, image.pixelHeight]].every(([x, y]) => {
    const point = transformPoint(pixelToWorld, x, y)
    const restored = transformPoint(worldToPixel, point.x, point.y)
    return close(x, restored.x) && close(y, restored.y)
  })
}

export const isLevelMapRegistry = (value: unknown): value is LevelMapRegistry => isJsonObject(value) &&
  value.format === 'EndfieldLevelMaps/1' && value.status === 'prepared' && Array.isArray(value.entries) && value.entries.every(isLevelMap) &&
  new Set(value.entries.map(entry => entry.levelId)).size === value.entries.length

export function selectionBounds(map: LevelMap, selection: PaintingSelection): BoundsXZ {
  const { u, v, width, height } = selection
  if (![u, v, width, height].every(Number.isFinite) || u < 0 || v < 0 || width <= 0 || height <= 0 || u + width > 1.000001 || v + height > 1.000001) {
    throw new RangeError('画境选区必须位于地图内且面积大于零')
  }
  const a = transformPoint(map.pixelToWorld, u * map.image.pixelWidth, v * map.image.pixelHeight)
  const b = transformPoint(map.pixelToWorld, (u + width) * map.image.pixelWidth, (v + height) * map.image.pixelHeight)
  const grid = map.selectionGrid
  const low = (value: number, origin: number): number => origin + Math.floor((value - origin) / grid.cellSizeMetres + 1e-7) * grid.cellSizeMetres
  const high = (value: number, origin: number): number => origin + Math.ceil((value - origin) / grid.cellSizeMetres - 1e-7) * grid.cellSizeMetres
  const result = {
    xmin: Math.max(map.worldBounds.xmin, low(Math.min(a.x, b.x), grid.origin.x)),
    xmax: Math.min(map.worldBounds.xmax, high(Math.max(a.x, b.x), grid.origin.x)),
    zmin: Math.max(map.worldBounds.zmin, low(Math.min(a.y, b.y), grid.origin.z)),
    zmax: Math.min(map.worldBounds.zmax, high(Math.max(a.y, b.y), grid.origin.z)),
  }
  if (!bounds(result)) throw new RangeError('画境选区换算后必须是有限、非空的世界坐标范围')
  return result
}

export function snapPaintingSelection(map: LevelMap, selection: PaintingSelection): PaintingSelection {
  const bounds = selectionBounds(map, selection)
  const a = transformPoint(map.worldToPixel, bounds.xmin, bounds.zmax)
  const b = transformPoint(map.worldToPixel, bounds.xmax, bounds.zmin)
  return { u: a.x / map.image.pixelWidth, v: a.y / map.image.pixelHeight, width: (b.x - a.x) / map.image.pixelWidth, height: (b.y - a.y) / map.image.pixelHeight }
}

export function describeSelection(map: LevelMap, selection: PaintingSelection): string {
  const bounds = selectionBounds(map, selection)
  const count = Math.round((bounds.xmax - bounds.xmin) * (bounds.zmax - bounds.zmin) / map.selectionGrid.cellSizeMetres ** 2)
  return `${count} 区块 · X ${bounds.xmin}…${bounds.xmax} / Z ${bounds.zmin}…${bounds.zmax}`
}
