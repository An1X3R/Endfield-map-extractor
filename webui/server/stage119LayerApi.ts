// @ts-nocheck
import { createHash } from 'node:crypto'
import { createReadStream, existsSync, readFileSync } from 'node:fs'
import { createGunzip } from 'node:zlib'
import { createInterface } from 'node:readline'
import path from 'node:path'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin } from 'vite'

type MapId = 'map01' | 'map02'
type LayerId = 'instances' | 'water' | 'effects' | 'lights' | 'roads'

const MAP_IDS: MapId[] = ['map01', 'map02']
const LAYERS: LayerId[] = ['instances', 'water', 'effects', 'lights', 'roads']
const EXPECTED_FORMAT = 'EndfieldRegionMapLayerManifest/1'
const EXPECTED_DATASET_FORMAT = 'EndfieldMapLayerDataset/1'
const EXPECTED_GENERATOR = 'stage119-v1'
const EXPECTED_RESOLVER = 'endfield-sector128-v1'
const MAX_SECTORS_PER_REQUEST = 256
const MAX_RECORDS_PER_REQUEST = 5000
const DEFAULT_RECORD_LIMIT = 1800
const MAX_RESPONSE_CACHE_ENTRIES = 96
const MAX_SHARD_CACHE_ENTRIES = 48

type JsonObject = Record<string, unknown>

type LayerIndex = JsonObject & {
  format?: string
  mapId?: MapId
  layer?: LayerId
  recordType?: string
  recordEncoding?: string
  geometryTypes?: string[]
  scan?: { status?: string; coverageScope?: string; limitations?: string[] }
  recordCount?: number
  coverageCount?: number
  duplicateCount?: number
  pendingCount?: number
  bindingCounts?: Record<string, number>
  sectorKeys?: string[]
  spatialLevelIds?: string[]
  records?: string | { path?: string; bytes?: number; sha256?: string }
  shards?: Array<{ path?: string; sectorKey?: string; sectorX?: number; sectorZ?: number; recordCount?: number }>
  roadShards?: Array<{ path?: string; sectorKey?: string; roadRecordCount?: number; sourceShardRecordCount?: number; sourceShardSha256?: string }>
  assetResolutionCounts?: Record<string, number>
  assetResolutionInstanceCounts?: Record<string, number>
  assetPendingCount?: number
  assetPendingInstanceCount?: number
  filter?: JsonObject
}

type Stage119Package = {
  root: string
  manifest: JsonObject & { maps: Record<MapId, { domainId?: string; worldBounds?: JsonObject; levels?: unknown[]; layers?: Record<LayerId, JsonObject> }> }
  audit: JsonObject & { status?: string }
  indexes: Record<MapId, Record<LayerId, LayerIndex>>
}

type SafeRecord = JsonObject & {
  recordType?: string
  mapId?: MapId
  bindingStatus?: string
  sectorKeys?: string[]
  positionUnity?: { x: number; y: number | null; z: number }
  boundsUnity?: JsonObject | null
}

type ResponseCacheEntry = { etag: string; body: string; createdAt: number }

const packageCache = new Map<string, Stage119Package>()
const shardCache = new Map<string, SafeRecord[]>()
const responseCache = new Map<string, ResponseCacheEntry>()

const readJson = <T>(filePath: string): T =>
  JSON.parse(readFileSync(filePath, 'utf8')) as T

const sha256File = (filePath: string) => {
  const hash = createHash('sha256')
  hash.update(readFileSync(filePath))
  return hash.digest('hex').toUpperCase()
}

const isInside = (root: string, candidate: string) => {
  const relative = path.relative(path.resolve(root), path.resolve(candidate))
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative))
}

const safeRelativePath = (root: string, relativePath: unknown) => {
  if (typeof relativePath !== 'string' || !relativePath || path.isAbsolute(relativePath)) return null
  const normalized = relativePath.replace(/\\/g, '/')
  if (normalized.split('/').some((part) => part === '..' || part === '')) return null
  const candidate = path.resolve(root, ...normalized.split('/'))
  return isInside(root, candidate) ? candidate : null
}

const recordPath = (value: LayerIndex['records']) =>
  typeof value === 'string'
    ? value
    : value && typeof value === 'object' && typeof value.path === 'string'
      ? value.path
      : null

const stableJson = (value: unknown) => JSON.stringify(value)

const asFiniteNumber = (value: unknown) =>
  typeof value === 'number' && Number.isFinite(value) ? value : null

const safeVector = (value: unknown) => {
  if (!value || typeof value !== 'object') return undefined
  const vector = value as Record<string, unknown>
  const x = asFiniteNumber(vector.x)
  const y = vector.y === null ? null : asFiniteNumber(vector.y)
  const z = asFiniteNumber(vector.z)
  if (x === null || z === null || (vector.y !== null && y === null)) return undefined
  return { x, y, z }
}

const safeBounds = (value: unknown) => {
  if (!value || typeof value !== 'object') return null
  const bounds = value as Record<string, unknown>
  const keys = ['xmin', 'xmax', 'ymin', 'ymax', 'zmin', 'zmax'] as const
  const output: Record<string, number | null> = {}
  for (const key of keys) {
    const raw = bounds[key]
    if (raw === null) {
      output[key] = null
      continue
    }
    const number = asFiniteNumber(raw)
    if (number === null) return null
    output[key] = number
  }
  return output
}

const safeGeometry = (value: unknown) => {
  if (!value || typeof value !== 'object') return undefined
  const geometry = value as Record<string, unknown>
  const output: JsonObject = {}
  for (const key of ['type', 'projectionPlane', 'footprintMeaning', 'imageOrientation'] as const) {
    if (typeof geometry[key] === 'string') output[key] = geometry[key]
  }
  if (Array.isArray(geometry.coordinatesUnityXZ)) output.coordinatesUnityXZ = geometry.coordinatesUnityXZ
  if (geometry.worldRectUnityXZ && typeof geometry.worldRectUnityXZ === 'object') {
    output.worldRectUnityXZ = safeBounds(geometry.worldRectUnityXZ)
  }
  if (geometry.flowDirectionUnityXZ && typeof geometry.flowDirectionUnityXZ === 'object') {
    output.flowDirectionUnityXZ = geometry.flowDirectionUnityXZ
  }
  return Object.keys(output).length ? output : undefined
}

const safeSectorKeys = (value: unknown) =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && /^sector:-?\d+:-?\d+$/.test(item)).slice(0, 8)
    : undefined

const sanitizeRecord = (record: JsonObject): SafeRecord => {
  const output: SafeRecord = {}
  const copyString = (key: string) => {
    if (typeof record[key] === 'string') output[key] = record[key]
  }
  for (const key of [
    'recordType',
    'mapId',
    'domainId',
    'bindingStatus',
    'displayClass',
    'category',
    'effectClass',
    'effectGroup',
    'waterType',
    'geometryStatus',
    'worldResolution',
    'spatialLevelId',
    'sourceLevelId',
    'anchorId',
    'anchorSelectionPolicy',
    'fullSystemGroup',
    'instanceId',
    'effectId',
    'waterId',
    'assetId',
    'prefabName',
  ]) copyString(key)
  const sectors = safeSectorKeys(record.sectorKeys)
  if (sectors) output.sectorKeys = sectors
  const positionUnity = safeVector(record.positionUnity)
  if (positionUnity) output.positionUnity = positionUnity
  const positionBlender = safeVector(record.positionBlender)
  if (positionBlender) output.positionBlender = positionBlender
  if (record.boundsUnity === null) output.boundsUnity = null
  else if (record.boundsUnity) output.boundsUnity = safeBounds(record.boundsUnity)
  const geometry = safeGeometry(record.geometry)
  if (geometry) output.geometry = geometry
  if (typeof record.surfaceY === 'number' || record.surfaceY === null) output.surfaceY = record.surfaceY
  if (typeof record.confidence === 'number') output.confidence = record.confidence
  if (typeof record.levelBindingStatus === 'string') output.levelBindingStatus = record.levelBindingStatus
  if (Array.isArray(record.spatialLevelIds)) {
    output.spatialLevelIds = record.spatialLevelIds.filter((item): item is string => typeof item === 'string').slice(0, 16)
  }
  return output
}

const parseSectorKey = (value: string) => /^sector:-?\d+:-?\d+$/.test(value)

const parseQueryList = (query: URLSearchParams, key: string) =>
  query.getAll(key)
    .flatMap((value) => value.split(','))
    .map((value) => value.trim())
    .filter(Boolean)

const configuredStageRoot = () => {
  const pointerPath = process.env.ENDFIELD_STAGE_POINTER_FILE?.trim()
  if (pointerPath && existsSync(pointerPath)) {
    try {
      const pointer = readJson<{ format?: string; stage_root?: string | null }>(pointerPath)
      if (pointer.format === 'EndfieldStagePointer/1' && typeof pointer.stage_root === 'string' && pointer.stage_root.trim()) {
        return pointer.stage_root.trim()
      }
    } catch { /* fall back to the startup value */ }
  }
  return process.env.ENDFIELD_MAP_LAYER_ROOT?.trim() ?? ''
}

const loadStage119Package = (): Stage119Package => {
  const configuredRoot = configuredStageRoot()
  if (!configuredRoot) throw new Error('stage119_root_unconfigured')
  const root = path.resolve(configuredRoot)
  const cached = packageCache.get(root)
  if (cached) return cached

  const manifestPath = path.join(root, 'region_map_layer_manifest.json')
  const auditPath = path.join(root, 'full_audit.json')
  if (!existsSync(manifestPath) || !existsSync(auditPath)) throw new Error('stage119_files_missing')
  const manifest = readJson<Stage119Package['manifest']>(manifestPath)
  const audit = readJson<Stage119Package['audit']>(auditPath)
  const fingerprint = typeof manifest.datasetFingerprint === 'string' ? manifest.datasetFingerprint : ''
  if (
    manifest.format !== EXPECTED_FORMAT ||
    manifest.generatorVersion !== EXPECTED_GENERATOR ||
    manifest.resolverVersion !== EXPECTED_RESOLVER ||
    !/^[0-9A-F]{64}$/.test(fingerprint) ||
    audit.status !== 'passed' ||
    audit.datasetFingerprint !== fingerprint
  ) throw new Error('stage119_contract_gate_failed')

  const indexes = {} as Stage119Package['indexes']
  for (const mapId of MAP_IDS) {
    indexes[mapId] = {} as Record<LayerId, LayerIndex>
    for (const layer of LAYERS) {
      const layerMeta = manifest.maps?.[mapId]?.layers?.[layer]
      if (!layerMeta || typeof layerMeta !== 'object') throw new Error('stage119_layer_missing')
      const indexPath = safeRelativePath(root, layerMeta.indexPath)
      if (!indexPath || !existsSync(indexPath)) throw new Error('stage119_index_path_invalid')
      const index = readJson<LayerIndex>(indexPath)
      if (index.format !== EXPECTED_DATASET_FORMAT || index.mapId !== mapId || index.layer !== layer) {
        throw new Error('stage119_index_contract_failed')
      }
      for (const shard of [...(index.shards ?? []), ...(index.roadShards ?? [])]) {
        if (shard.path && !safeRelativePath(root, shard.path)) throw new Error('stage119_shard_path_invalid')
      }
      const recordsPath = recordPath(index.records)
      if (index.records && (!recordsPath || !safeRelativePath(root, recordsPath))) throw new Error('stage119_records_path_invalid')
      indexes[mapId][layer] = index
    }
  }
  const result = { root, manifest, audit, indexes }
  packageCache.set(root, result)
  return result
}

const readGzipJsonl = (filePath: string, signal: AbortSignal) =>
  new Promise<SafeRecord[]>((resolve, reject) => {
    if (signal.aborted) {
      reject(new Error('request_aborted'))
      return
    }
    const records: SafeRecord[] = []
    const input = createReadStream(filePath)
    const gunzip = createGunzip()
    const lines = createInterface({ input: input.pipe(gunzip), crlfDelay: Infinity })
    let settled = false
    const cleanup = () => signal.removeEventListener('abort', abort)
    const abort = () => {
      input.destroy()
      gunzip.destroy()
      lines.close()
      if (!settled) {
        settled = true
        cleanup()
        reject(new Error('request_aborted'))
      }
    }
    signal.addEventListener('abort', abort, { once: true })
    lines.on('line', (line) => {
      if (!line.trim() || signal.aborted) return
      try {
        records.push(sanitizeRecord(JSON.parse(line) as JsonObject))
      } catch {
        lines.close()
        input.destroy(new Error('invalid_jsonl_record'))
      }
    })
    lines.on('close', () => {
      if (settled) return
      settled = true
      cleanup()
      resolve(records)
    })
    input.on('error', (error) => {
      if (settled) return
      settled = true
      cleanup()
      reject(error)
    })
    gunzip.on('error', (error) => {
      if (settled) return
      settled = true
      cleanup()
      reject(error)
    })
  })

const getShardRecords = async (pkg: Stage119Package, relativePath: string, signal: AbortSignal) => {
  const filePath = safeRelativePath(pkg.root, relativePath)
  if (!filePath || !existsSync(filePath)) throw new Error('stage119_shard_missing')
  const cacheKey = `${pkg.root}:${relativePath}`
  const cached = shardCache.get(cacheKey)
  if (cached) return cached
  const records = await readGzipJsonl(filePath, signal)
  shardCache.set(cacheKey, records)
  while (shardCache.size > MAX_SHARD_CACHE_ENTRIES) shardCache.delete(shardCache.keys().next().value as string)
  return records
}

const setJson = (response: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}) => {
  const payload = JSON.stringify(body)
  response.statusCode = status
  response.setHeader('Content-Type', 'application/json; charset=utf-8')
  response.setHeader('Cache-Control', 'private, max-age=30')
  for (const [key, value] of Object.entries(headers)) response.setHeader(key, value)
  response.end(payload)
}

const setCachedJson = (request: IncomingMessage, response: ServerResponse, cacheKey: string, body: unknown, pkg: Stage119Package) => {
  const fingerprint = String(pkg.manifest.datasetFingerprint ?? '')
  const versionedCacheKey = `${fingerprint}:${cacheKey}`
  const cached = responseCache.get(versionedCacheKey)
  const entry = cached ?? (() => {
    const payload = JSON.stringify(body)
    const etag = `"${createHash('sha256').update(payload).digest('hex').toUpperCase()}"`
    const value = { etag, body: payload, createdAt: Date.now() }
    responseCache.set(versionedCacheKey, value)
    while (responseCache.size > MAX_RESPONSE_CACHE_ENTRIES) responseCache.delete(responseCache.keys().next().value as string)
    return value
  })()
  if (request.headers['if-none-match'] === entry.etag) {
    response.statusCode = 304
    response.setHeader('ETag', entry.etag)
    response.end()
    return
  }
  response.statusCode = 200
  response.setHeader('Content-Type', 'application/json; charset=utf-8')
  response.setHeader('Cache-Control', 'private, max-age=30')
  response.setHeader('ETag', entry.etag)
  response.setHeader('X-Endfield-Dataset-Fingerprint', fingerprint)
  response.setHeader('X-Endfield-Layer-Source', 'stage119')
  response.end(entry.body)
  void pkg
}

const safeLayerMeta = (pkg: Stage119Package, mapId: MapId, layer: LayerId) => {
  const map = pkg.manifest.maps[mapId]
  const meta = map.layers?.[layer] ?? {}
  const index = pkg.indexes[mapId][layer]
  const shardSectorKeys = [...(index.shards ?? []), ...(index.roadShards ?? [])]
    .map((shard) => shard.sectorKey)
    .filter((value): value is string => typeof value === 'string' && parseSectorKey(value))
  const sectorKeys = Array.from(new Set([...(index.sectorKeys ?? []), ...shardSectorKeys]))
  return {
    mapId,
    domainId: map.domainId ?? null,
    layer,
    recordType: index.recordType ?? null,
    recordEncoding: index.recordEncoding ?? null,
    geometryTypes: index.geometryTypes ?? [],
    scanStatus: index.scan?.status ?? 'unknown',
    scanCoverageScope: index.scan?.coverageScope ?? null,
    scanLimitations: index.scan?.limitations ?? [],
    recordCount: index.recordCount ?? 0,
    coverageCount: index.coverageCount ?? 0,
    duplicateCount: index.duplicateCount ?? 0,
    pendingCount: index.pendingCount ?? 0,
    bindingCounts: index.bindingCounts ?? {},
    sectorKeys,
    spatialLevelIds: index.spatialLevelIds ?? [],
    levelIds: Array.isArray(meta.levelIds) ? meta.levelIds : [],
    worldBounds: meta.worldBounds ?? map.worldBounds ?? null,
    assetResolutionCounts: index.assetResolutionCounts ?? null,
    assetResolutionInstanceCounts: index.assetResolutionInstanceCounts ?? null,
    assetPendingCount: index.assetPendingCount ?? null,
    assetPendingInstanceCount: index.assetPendingInstanceCount ?? null,
    roadSectorCount: Array.isArray(index.roadShards) ? index.roadShards.length : null,
  }
}

const buildCatalog = (pkg: Stage119Package) => ({
  format: 'EndfieldMapLayerCatalog/1',
  dataset: {
    format: EXPECTED_FORMAT,
    datasetFingerprint: pkg.manifest.datasetFingerprint,
    manifestSha256: sha256File(path.join(pkg.root, 'region_map_layer_manifest.json')),
    auditSha256: sha256File(path.join(pkg.root, 'full_audit.json')),
    generatorVersion: EXPECTED_GENERATOR,
    resolverVersion: EXPECTED_RESOLVER,
    auditStatus: pkg.audit.status ?? 'unknown',
  },
  coordinateConvention: pkg.manifest.coordinateConvention ?? null,
  maps: MAP_IDS.map((mapId) => ({
    mapId,
    domainId: pkg.manifest.maps[mapId].domainId ?? null,
    worldBounds: pkg.manifest.maps[mapId].worldBounds ?? null,
    levels: pkg.manifest.maps[mapId].levels ?? [],
    layers: Object.fromEntries(LAYERS.map((layer) => [layer, safeLayerMeta(pkg, mapId, layer)])),
  })),
})

const matchesRecord = (record: SafeRecord, filters: { sectorKeys: Set<string>; levelIds: Set<string>; categories: Set<string>; bindingStatuses: Set<string> }) => {
  if (filters.sectorKeys.size && !(record.sectorKeys ?? []).some((key) => filters.sectorKeys.has(key))) return false
  const level = typeof record.spatialLevelId === 'string' ? record.spatialLevelId : record.sourceLevelId
  if (filters.levelIds.size && (typeof level !== 'string' || !filters.levelIds.has(level))) return false
  if (filters.categories.size && (typeof record.category !== 'string' || !filters.categories.has(record.category))) return false
  if (filters.bindingStatuses.size && (typeof record.bindingStatus !== 'string' || !filters.bindingStatuses.has(record.bindingStatus))) return false
  return true
}

const readLayerRecords = async (pkg: Stage119Package, mapId: MapId, layer: LayerId, query: URLSearchParams, signal: AbortSignal) => {
  const index = pkg.indexes[mapId][layer]
  const requestedSectors = parseQueryList(query, 'sectorKey').filter(parseSectorKey)
  const derivedSectorKeys = [...(index.sectorKeys ?? []), ...(index.shards ?? []).map((shard) => shard.sectorKey), ...(index.roadShards ?? []).map((shard) => shard.sectorKey)]
  const validSectorKeys = new Set(derivedSectorKeys.filter((value): value is string => typeof value === 'string' && parseSectorKey(value)))
  const sectorKeys = requestedSectors.filter((key) => validSectorKeys.has(key)).slice(0, MAX_SECTORS_PER_REQUEST)
  const sampled = sectorKeys.length === 0
  const effectiveSectors = new Set(sampled ? Array.from(validSectorKeys).slice(0, 8) : sectorKeys)
  const levelIds = new Set(parseQueryList(query, 'levelId').slice(0, 32))
  const categories = new Set(parseQueryList(query, 'category').slice(0, 16))
  const bindingStatuses = new Set(parseQueryList(query, 'bindingStatus').slice(0, 8))
  const rawLimit = Number.parseInt(query.get('limit') ?? String(DEFAULT_RECORD_LIMIT), 10)
  const limit = Number.isFinite(rawLimit) ? Math.min(MAX_RECORDS_PER_REQUEST, Math.max(1, rawLimit)) : DEFAULT_RECORD_LIMIT

  const records: SafeRecord[] = []
  const append = (values: SafeRecord[]) => {
    for (const record of values) {
      if (!matchesRecord(record, { sectorKeys: effectiveSectors, levelIds, categories, bindingStatuses })) continue
      records.push(record)
      if (records.length >= limit) break
    }
  }

  if (layer === 'instances' || layer === 'roads') {
    const shardEntries = layer === 'roads'
      ? (index.roadShards ?? []).filter((shard) => effectiveSectors.has(shard.sectorKey ?? ''))
      : (index.shards ?? []).filter((shard) => effectiveSectors.has(shard.sectorKey ?? ''))
    for (const shard of shardEntries) {
      if (signal.aborted || records.length >= limit) break
      if (!shard.path) continue
      const shardRecords = await getShardRecords(pkg, shard.path, signal)
      append(layer === 'roads' ? shardRecords.filter((record) => record.category === 'road') : shardRecords)
    }
  } else {
    const recordsPath = recordPath(index.records)
    if (recordsPath) append(await getShardRecords(pkg, recordsPath, signal))
  }

  return {
    records,
    filter: {
      sectorKeys: Array.from(effectiveSectors),
      requestedSectorCount: requestedSectors.length,
      sectorLimit: requestedSectors.length > MAX_SECTORS_PER_REQUEST,
      sampled,
      levelIds: Array.from(levelIds),
      categories: Array.from(categories),
      bindingStatuses: Array.from(bindingStatuses),
      limit,
    },
    totalMatchingHint: index.recordCount ?? records.length,
  }
}

export const stage119LayerApi = (_projectRoot: string): Plugin => ({
  name: 'endfield-stage119-layer-api',
  configureServer(server) {
    server.middlewares.use((request, response, next) => {
      const method = request.method ?? 'GET'
      const requestUrl = new URL(request.url ?? '/', 'http://127.0.0.1')
      const pathname = requestUrl.pathname
      const catalogMatch = pathname === '/api/v3/layer-catalog'
      const mapLayerMatch = pathname.match(/^\/api\/v3\/maps\/(map01|map02)\/layers(?:\/(instances|water|effects|lights|roads))?$/)
      if (method !== 'GET' || (!catalogMatch && !mapLayerMatch)) {
        next()
        return
      }

      let pkg: Stage119Package
      try {
        pkg = loadStage119Package()
      } catch {
        setJson(response, 503, {
          format: 'EndfieldMapLayerError/1',
          error: 'stage119_dataset_unavailable',
          message: 'The Stage119 layer dataset failed its server-side version gate.',
        })
        return
      }

      if (catalogMatch) {
        setCachedJson(request, response, 'catalog', buildCatalog(pkg), pkg)
        return
      }

      const mapId = mapLayerMatch?.[1] as MapId
      const layer = (mapLayerMatch?.[2] ?? null) as LayerId | null
      if (!layer) {
        setCachedJson(request, response, `map:${mapId}:layers`, {
          format: 'EndfieldMapLayerMap/1',
          datasetFingerprint: pkg.manifest.datasetFingerprint,
          mapId,
          layers: Object.fromEntries(LAYERS.map((item) => [item, safeLayerMeta(pkg, mapId, item)])),
        }, pkg)
        return
      }

      const controller = new AbortController()
      const onClose = () => controller.abort()
      request.once('close', onClose)
      void readLayerRecords(pkg, mapId, layer, requestUrl.searchParams, controller.signal)
        .then(({ records, filter, totalMatchingHint }) => {
          if (controller.signal.aborted || response.writableEnded) return
          const body = {
            format: 'EndfieldMapLayerResponse/1',
            datasetFingerprint: pkg.manifest.datasetFingerprint,
            manifestFormat: EXPECTED_FORMAT,
            generatorVersion: EXPECTED_GENERATOR,
            resolverVersion: EXPECTED_RESOLVER,
            mapId,
            layer,
            meta: safeLayerMeta(pkg, mapId, layer),
            filter,
            returnedCount: records.length,
            totalMatchingHint,
            truncated: records.length >= filter.limit,
            records,
          }
          setCachedJson(request, response, `records:${mapId}:${layer}:${stableJson(filter)}`, body, pkg)
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted || response.writableEnded) return
          setJson(response, 500, {
            format: 'EndfieldMapLayerError/1',
            error: error instanceof Error && error.message === 'stage119_shard_missing' ? 'stage119_shard_missing' : 'stage119_layer_read_failed',
            mapId,
            layer,
          })
        })
        .finally(() => request.removeListener('close', onClose))
    })
  },
})
