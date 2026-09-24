// @ts-nocheck
import { createHash } from 'node:crypto'
import { closeSync, createReadStream, existsSync, openSync, readFileSync, readSync, realpathSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin } from 'vite'

type MapId = 'map01' | 'map02'
type OverviewVariant = 'clean' | 'sectors'
type OverviewSource = 'runtime-config' | 'dev-override'
type OverviewFile = { filePath: string; source: OverviewSource }
type JsonRecord = Record<string, unknown>

const MAP_IDS: MapId[] = ['map01', 'map02']
const OVERVIEW_VARIANTS: OverviewVariant[] = ['clean', 'sectors']
const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const runtimeManifestPath = path.join(projectRoot, 'src', 'data', 'region_map_runtime_manifest.json')

class MapOverviewError extends Error {
  readonly reason: string

  constructor(reason: string, message: string) {
    super(message)
    this.name = 'MapOverviewError'
    this.reason = reason
  }
}

const isRecord = (value: unknown): value is JsonRecord =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const sendJson = (response: ServerResponse, status: number, body: JsonRecord): void => {
  response.statusCode = status
  response.setHeader('Content-Type', 'application/json; charset=utf-8')
  response.setHeader('Cache-Control', 'no-store')
  response.end(JSON.stringify(body))
}

const readJson = (filePath: string, missingReason: string): JsonRecord => {
  if (!existsSync(filePath)) throw new MapOverviewError(missingReason, 'Required map overview metadata is missing.')
  try {
    const value: unknown = JSON.parse(readFileSync(filePath, 'utf8'))
    if (!isRecord(value)) throw new MapOverviewError('invalid_metadata', 'Map overview metadata must be a JSON object.')
    return value
  } catch (error) {
    if (error instanceof MapOverviewError) throw error
    throw new MapOverviewError('invalid_metadata', 'Map overview metadata is not valid JSON.')
  }
}

const resolveCurrentRuntime = (): { runtimePath: string; runtime: JsonRecord } | null => {
  const pointerPath = process.env.ENDFIELD_STAGE_POINTER_FILE?.trim()
  if (!pointerPath) return null

  const pointer = readJson(pointerPath, 'stage_pointer_missing')
  if (pointer.format !== 'EndfieldStagePointer/1') {
    throw new MapOverviewError('invalid_stage_pointer', 'The active stage pointer has an unsupported format.')
  }
  const configuredRuntimePath = pointer.runtime_config
  if (configuredRuntimePath === null || configuredRuntimePath === undefined || configuredRuntimePath === '') return null
  if (typeof configuredRuntimePath !== 'string') {
    throw new MapOverviewError('invalid_stage_pointer', 'The active stage pointer runtime_config must be a path or null.')
  }

  const runtimePath = path.isAbsolute(configuredRuntimePath)
    ? path.resolve(configuredRuntimePath)
    : path.resolve(path.dirname(pointerPath), configuredRuntimePath)
  const runtime = readJson(runtimePath, 'runtime_config_missing')
  if (runtime.format !== 'EndfieldRuntimeConfig/1') {
    throw new MapOverviewError('invalid_runtime_config', 'The active runtime config has an unsupported format.')
  }
  return { runtimePath, runtime }
}

const getOverviewManifestPath = (runtimePath: string, runtime: JsonRecord): string => {
  const extraction = runtime.extraction
  if (!isRecord(extraction) || !isRecord(extraction.mapOverviews)) {
    throw new MapOverviewError('overview_manifest_not_declared', 'The active runtime does not declare a map overview manifest.')
  }
  const manifestPath = extraction.mapOverviews.path
  if (typeof manifestPath !== 'string' || !manifestPath.trim()) {
    throw new MapOverviewError('invalid_overview_manifest_path', 'The active runtime mapOverviews.path must be a non-empty path.')
  }
  return path.isAbsolute(manifestPath)
    ? path.resolve(manifestPath)
    : path.resolve(path.dirname(runtimePath), manifestPath)
}

const safeManifestRelativePath = (root: string, value: unknown): string => {
  if (typeof value !== 'string' || !value.trim()) {
    throw new MapOverviewError('invalid_overview_path', 'Overview image paths must be non-empty relative paths.')
  }
  const normalized = value.replace(/\\/g, '/')
  if (
    normalized.startsWith('/') ||
    /^[a-zA-Z]:/.test(normalized) ||
    normalized.split('/').some((part) => part === '..' || part === '' || part === '.')
  ) {
    throw new MapOverviewError('unsafe_overview_path', 'Overview image paths must stay inside the manifest directory.')
  }
  const candidate = path.resolve(root, ...normalized.split('/'))
  const relative = path.relative(path.resolve(root), candidate)
  if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new MapOverviewError('unsafe_overview_path', 'Overview image paths must stay inside the manifest directory.')
  }
  return candidate
}

const validatePng = (filePath: string, manifestRoot: string): void => {
  if (!existsSync(filePath)) throw new MapOverviewError('overview_image_missing', 'The active overview image is missing.')
  let realRoot: string
  let realFile: string
  try {
    realRoot = realpathSync(manifestRoot)
    realFile = realpathSync(filePath)
  } catch {
    throw new MapOverviewError('overview_image_missing', 'The active overview image cannot be resolved.')
  }
  const relative = path.relative(realRoot, realFile)
  if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new MapOverviewError('unsafe_overview_path', 'Overview image symlinks must stay inside the manifest directory.')
  }
  if (!statSync(realFile).isFile()) throw new MapOverviewError('invalid_overview_image', 'The active overview image path is not a file.')
  const descriptor = openSync(realFile, 'r')
  const signature = Buffer.alloc(PNG_SIGNATURE.length)
  try {
    readSync(descriptor, signature, 0, PNG_SIGNATURE.length, 0)
  } finally {
    closeSync(descriptor)
  }
  if (!signature.equals(PNG_SIGNATURE)) throw new MapOverviewError('invalid_overview_image', 'The active overview image is not a PNG file.')
}

const hashFile = async (filePath: string): Promise<string> => {
  const hash = createHash('sha256')
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(filePath)
    stream.on('data', (chunk) => hash.update(chunk))
    stream.once('error', reject)
    stream.once('end', resolve)
  })
  return hash.digest('hex').toUpperCase()
}

const resolveManifestOverview = (
  manifestPath: string,
  mapId: MapId,
  variant: OverviewVariant,
): OverviewFile => {
  const manifest = readJson(manifestPath, 'overview_manifest_missing')
  if (manifest.format !== 'EndfieldMapOverviews/1' || manifest.status !== 'ready' || !isRecord(manifest.maps)) {
    throw new MapOverviewError('invalid_overview_manifest', 'The active map overview manifest is not ready or has an unsupported format.')
  }
  for (const requiredMapId of MAP_IDS) {
    const map = manifest.maps[requiredMapId]
    if (!isRecord(map)) throw new MapOverviewError('invalid_overview_manifest', `The overview manifest is missing ${requiredMapId}.`)
    for (const requiredVariant of OVERVIEW_VARIANTS) {
      safeManifestRelativePath(path.dirname(manifestPath), map[requiredVariant])
    }
  }
  const map = manifest.maps[mapId]
  if (!isRecord(map)) throw new MapOverviewError('invalid_overview_manifest', `The overview manifest is missing ${mapId}.`)
  const filePath = safeManifestRelativePath(path.dirname(manifestPath), map[variant])
  validatePng(filePath, path.dirname(manifestPath))
  return { filePath, source: 'runtime-config' }
}

const resolveDevOverride = (mapId: MapId, variant: OverviewVariant): OverviewFile | null => {
  const configuredPath = process.env.ENDFIELD_MAP_OVERRIDES_FILE?.trim()
  if (!configuredPath) return null
  const overrides = readJson(configuredPath, 'dev_override_missing')
  if (overrides.enabled !== true || overrides.serverOnly === false || !isRecord(overrides.maps)) return null
  const map = overrides.maps[mapId]
  if (!isRecord(map)) return null
  const imagePath = map[variant]
  if (typeof imagePath !== 'string' || !imagePath.trim()) return null
  const resolvedPath = path.resolve(imagePath)
  validatePng(resolvedPath, path.dirname(resolvedPath))
  return { filePath: resolvedPath, source: 'dev-override' }
}

const resolveOverviewFile = (mapId: MapId, variant: OverviewVariant): OverviewFile => {
  const currentRuntime = resolveCurrentRuntime()
  if (currentRuntime) {
    const manifestPath = getOverviewManifestPath(currentRuntime.runtimePath, currentRuntime.runtime)
    return resolveManifestOverview(manifestPath, mapId, variant)
  }
  const override = resolveDevOverride(mapId, variant)
  if (override) return override
  throw new MapOverviewError('no_active_overview', 'The active runtime has no ready map overview and no explicit development override is configured.')
}

const isMapId = (value: string): value is MapId => value === 'map01' || value === 'map02'
const isOverviewVariant = (value: string): value is OverviewVariant => value === 'clean' || value === 'sectors'

const staticMapCatalog = (): JsonRecord => {
  const manifest = readJson(runtimeManifestPath, 'static_runtime_manifest_missing')
  if (!isRecord(manifest.maps)) throw new MapOverviewError('invalid_static_runtime_manifest', 'The built-in map catalog is invalid.')
  return manifest.maps
}

const handleOverviewRequest = async (
  request: IncomingMessage,
  response: ServerResponse,
  mapId: MapId,
  variant: OverviewVariant,
): Promise<void> => {
  try {
    const resolved = resolveOverviewFile(mapId, variant)
    const etag = `"${await hashFile(resolved.filePath)}"`
    if (request.headers['if-none-match'] === etag) {
      response.statusCode = 304
      response.setHeader('Cache-Control', 'no-store')
      response.setHeader('ETag', etag)
      response.end()
      return
    }
    response.statusCode = 200
    response.setHeader('Content-Type', 'image/png')
    response.setHeader('Content-Length', statSync(resolved.filePath).size)
    response.setHeader('Cache-Control', 'no-store')
    response.setHeader('ETag', etag)
    response.setHeader('X-Endfield-Map-Source', resolved.source)
    if (request.method === 'HEAD') {
      response.end()
      return
    }
    createReadStream(resolved.filePath)
      .on('error', () => {
        if (!response.headersSent) {
          sendJson(response, 500, { error: 'map_overview_read_failed', reason: 'overview_image_unreadable' })
        } else {
          response.destroy()
        }
      })
      .pipe(response)
  } catch (error) {
    const overviewError = error instanceof MapOverviewError
      ? error
      : new MapOverviewError('overview_resolution_failed', 'The active map overview could not be resolved.')
    sendJson(response, 404, {
      error: 'map_overview_missing',
      mapId,
      variant,
      reason: overviewError.reason,
      message: overviewError.message,
    })
  }
}

const handleCatalogRequest = (response: ServerResponse): void => {
  try {
    const maps = staticMapCatalog()
    const catalog = MAP_IDS.map((mapId) => {
      const metadata = maps[mapId]
      const resolved = OVERVIEW_VARIANTS.map((variant) => {
        try {
          return { variant, ...resolveOverviewFile(mapId, variant) }
        } catch (error) {
          return { variant, reason: error instanceof MapOverviewError ? error.reason : 'overview_resolution_failed' }
        }
      })
      const sources = Array.from(new Set(resolved.flatMap((item) => 'source' in item ? [item.source] : [])))
      return {
        mapId,
        domainId: isRecord(metadata) ? metadata.domainId ?? null : null,
        worldBounds: isRecord(metadata) ? metadata.worldBounds ?? null : null,
        sideSectors: isRecord(metadata) ? metadata.sideSectors ?? null : null,
        overview: {
          clean: `/api/v2/maps/${mapId}/overview?variant=clean`,
          sectors: `/api/v2/maps/${mapId}/overview?variant=sectors`,
          state: resolved.every((item) => 'filePath' in item) ? 'ready' : 'missing',
          sources,
          missing: resolved.filter((item) => 'reason' in item).map((item) => ({ variant: item.variant, reason: item.reason })),
        },
      }
    })
    sendJson(response, 200, {
      format: 'EndfieldMapCatalog/1',
      cacheRootConfigured: Boolean(process.env.ENDFIELD_MAP_CACHE_ROOT?.trim() || process.env.VITE_MAP_CACHE_ROOT?.trim()),
      runtimePointerConfigured: Boolean(process.env.ENDFIELD_STAGE_POINTER_FILE?.trim()),
      maps: catalog,
    } as unknown as JsonRecord)
  } catch (error) {
    const overviewError = error instanceof MapOverviewError
      ? error
      : new MapOverviewError('catalog_resolution_failed', 'The map catalog could not be resolved.')
    sendJson(response, 500, { error: 'map_catalog_unavailable', reason: overviewError.reason, message: overviewError.message })
  }
}

export const mapOverviewApi = (): Plugin => ({
  name: 'endfield-map-overview-api',
  configureServer(server) {
    server.middlewares.use((request, response, next) => {
      const method = request.method ?? 'GET'
      const requestUrl = new URL(request.url ?? '/', 'http://127.0.0.1')
      const overviewMatch = requestUrl.pathname.match(/^\/api\/v2\/maps\/(map01|map02)\/overview$/)
      if ((method === 'GET' || method === 'HEAD') && overviewMatch) {
        const mapId = overviewMatch[1]
        const variantValue = requestUrl.searchParams.get('variant') ?? 'clean'
        if (!isMapId(mapId) || !isOverviewVariant(variantValue)) {
          sendJson(response, 400, { error: 'invalid_overview_variant', message: 'variant must be clean or sectors' })
          return
        }
        void handleOverviewRequest(request, response, mapId, variantValue)
        return
      }
      if (method === 'GET' && requestUrl.pathname === '/api/v2/maps') {
        handleCatalogRequest(response)
        return
      }
      next()
    })
  },
})
