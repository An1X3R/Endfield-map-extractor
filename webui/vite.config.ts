// @ts-nocheck
﻿import { createHash } from 'node:crypto'
import { createReadStream, existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { stage119LayerApi } from './server/stage119LayerApi.ts'

const projectRoot = path.dirname(fileURLToPath(import.meta.url))
const runtimeManifestPath = path.join(projectRoot, 'src', 'data', 'region_map_runtime_manifest.json')

type MapId = 'map01' | 'map02'
type OverviewVariant = 'clean' | 'sectors'
type OverviewSource = 'dev-override' | 'generated-cache'

type DevOverrides = {
  enabled?: boolean
  serverOnly?: boolean
  maps?: Partial<Record<MapId, Partial<Record<OverviewVariant, string>>>>
}

const isOverviewVariant = (value: string): value is OverviewVariant => value === 'clean' || value === 'sectors'

const sendJson = (response: ServerResponse, status: number, body: unknown) => {
  const payload = JSON.stringify(body)
  response.statusCode = status
  response.setHeader('Content-Type', 'application/json; charset=utf-8')
  response.setHeader('Cache-Control', 'no-store')
  response.end(payload)
}

const readJson = <T>(filePath: string): T | null => {
  try {
    return JSON.parse(readFileSync(filePath, 'utf8')) as T
  } catch {
    return null
  }
}

const getOverrides = (): DevOverrides => {
  const configuredPath = process.env.ENDFIELD_MAP_OVERRIDES_FILE?.trim()
  if (!configuredPath) return {}
  const overrides = readJson<DevOverrides>(configuredPath)
  if (!overrides?.enabled || overrides.serverOnly === false) return {}
  return overrides
}

const getCacheRoot = () =>
  process.env.ENDFIELD_MAP_CACHE_ROOT?.trim() || process.env.VITE_MAP_CACHE_ROOT?.trim() || ''

const isInside = (root: string, candidate: string) => {
  const relative = path.relative(path.resolve(root), path.resolve(candidate))
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative))
}

const hashFile = (filePath: string) => {
  const hash = createHash('sha256')
  hash.update(readFileSync(filePath))
  return hash.digest('hex').toUpperCase()
}

const findGeneratedCacheFile = (mapId: MapId, variant: OverviewVariant) => {
  const cacheRoot = getCacheRoot()
  if (!cacheRoot || !existsSync(cacheRoot)) return null

  const mapsRoot = path.join(cacheRoot, 'maps')
  if (!existsSync(mapsRoot)) return null

  const requestedFingerprint = process.env.ENDFIELD_MAP_FINGERPRINT?.trim()
  const candidates = readdirSync(mapsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .filter((entry) => !requestedFingerprint || entry.name === requestedFingerprint)
    .map((entry) => path.join(mapsRoot, entry.name))
    .filter((directory) => isInside(mapsRoot, directory))
    .sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs)

  for (const directory of candidates) {
    const manifest = readJson<{ status?: string }>(path.join(directory, 'cache_manifest.json'))
    if (manifest?.status && manifest.status !== 'ready') continue
    const candidate = path.join(directory, mapId, variant + '.png')
    if (existsSync(candidate) && isInside(directory, candidate)) return candidate
  }
  return null
}

const resolveOverviewFile = (mapId: MapId, variant: OverviewVariant) => {
  const overrides = getOverrides()
  const overrideFile = overrides.maps?.[mapId]?.[variant]
  if (overrideFile && existsSync(overrideFile)) {
    return { filePath: overrideFile, source: 'dev-override' as OverviewSource }
  }

  const generatedFile = findGeneratedCacheFile(mapId, variant)
  if (generatedFile) return { filePath: generatedFile, source: 'generated-cache' as OverviewSource }
  return null
}

const getSafeMaps = () => {
  const runtime = readJson<{
    maps?: Record<MapId, { mapId: MapId; domainId: string; worldBounds: unknown; sideSectors: number }>
  }>(runtimeManifestPath)
  const maps = runtime?.maps ?? {}
  return (['map01', 'map02'] as MapId[]).map((mapId) => {
    const clean = resolveOverviewFile(mapId, 'clean')
    const sectors = resolveOverviewFile(mapId, 'sectors')
    return {
      mapId,
      domainId: maps[mapId]?.domainId ?? null,
      worldBounds: maps[mapId]?.worldBounds ?? null,
      sideSectors: maps[mapId]?.sideSectors ?? null,
      overview: {
        clean: '/api/v2/maps/' + mapId + '/overview?variant=clean',
        sectors: '/api/v2/maps/' + mapId + '/overview?variant=sectors',
        state: clean || sectors ? 'ready' : 'missing',
        sources: Array.from(new Set([clean?.source, sectors?.source].filter(Boolean))),
      },
    }
  })
}

const readRequestBody = (request: IncomingMessage) =>
  new Promise<string>((resolve, reject) => {
    let body = ''
    request.setEncoding('utf8')
    request.on('data', (chunk) => {
      body += chunk
      if (body.length > 1024 * 1024) reject(new Error('request body too large'))
    })
    request.on('end', () => resolve(body))
    request.on('error', reject)
  })

const endfieldMapRuntimeApi = (): Plugin => ({
  name: 'endfield-map-runtime-api',
  configureServer(server) {
    server.middlewares.use((request, response, next) => {
      const method = request.method ?? 'GET'
      const requestUrl = new URL(request.url ?? '/', 'http://127.0.0.1')
      const pathname = requestUrl.pathname

      if (pathname === '/api/v2/first-run' || pathname.startsWith('/api/v2/first-run/')) {
        const bridgeRoot = process.env.ENDFIELD_PATH_PICKER_URL?.trim() ?? ''
        const bridgeToken = process.env.ENDFIELD_PATH_PICKER_TOKEN?.trim() ?? ''
        let targetUrl: URL | null = null
        try {
          const suffix = pathname.slice('/api/v2/first-run'.length)
          targetUrl = new URL(`/first-run${suffix}${requestUrl.search}`, bridgeRoot)
        } catch { /* unavailable response below */ }
        if (!targetUrl || targetUrl.hostname !== '127.0.0.1' || !bridgeToken) {
          sendJson(response, 503, { error: { code: 'first_run_bridge_unavailable', message: 'First-run bridge unavailable.', retryable: true } })
          return
        }
        const forward = async () => {
          const rawBody = method === 'POST' ? await readRequestBody(request) : undefined
          const bridgeResponse = await fetch(targetUrl, {
            method,
            headers: {
              ...(rawBody !== undefined ? { 'Content-Type': 'application/json' } : {}),
              'X-Endfield-Path-Picker-Token': bridgeToken,
            },
            body: rawBody,
          })
          const payload = await bridgeResponse.text()
          response.statusCode = bridgeResponse.status
          response.setHeader('Content-Type', bridgeResponse.headers.get('content-type') ?? 'application/json; charset=utf-8')
          response.setHeader('Cache-Control', 'no-store')
          response.end(payload)
        }
        void forward().catch((error) => sendJson(response, 502, {
          error: { code: 'first_run_bridge_failed', message: error instanceof Error ? error.message : String(error), retryable: true },
        }))
        return
      }

      if (pathname === '/api/v2/export-jobs' || pathname.startsWith('/api/v2/export-jobs/')) {
        const bridgeRoot = process.env.ENDFIELD_PATH_PICKER_URL?.trim() ?? ''
        const bridgeToken = process.env.ENDFIELD_PATH_PICKER_TOKEN?.trim() ?? ''
        let targetUrl: URL | null = null
        try {
          const suffix = pathname.slice('/api/v2/export-jobs'.length)
          targetUrl = new URL(`/jobs${suffix}${requestUrl.search}`, bridgeRoot)
        } catch { /* unavailable response below */ }
        if (!targetUrl || targetUrl.hostname !== '127.0.0.1' || !bridgeToken) {
          sendJson(response, 503, { error: 'export_job_bridge_unavailable' })
          return
        }
        const forward = async () => {
          const rawBody = method === 'POST' ? await readRequestBody(request) : undefined
          const bridgeResponse = await fetch(targetUrl, {
            method,
            headers: {
              ...(rawBody !== undefined ? { 'Content-Type': 'application/json' } : {}),
              'X-Endfield-Path-Picker-Token': bridgeToken,
            },
            body: rawBody,
          })
          const payload = await bridgeResponse.text()
          response.statusCode = bridgeResponse.status
          response.setHeader('Content-Type', bridgeResponse.headers.get('content-type') ?? 'application/json; charset=utf-8')
          response.setHeader('Cache-Control', 'no-store')
          response.end(payload)
        }
        void forward().catch((error) => sendJson(response, 502, {
          error: 'export_job_bridge_failed',
          message: error instanceof Error ? error.message : String(error),
        }))
        return
      }

      const overviewMatch = pathname.match(/^\/api\/v2\/maps\/(map01|map02)\/overview$/)
      if ((method === 'GET' || method === 'HEAD') && overviewMatch) {
        const mapId = overviewMatch[1] as MapId
        const variantValue = requestUrl.searchParams.get('variant') ?? 'clean'
        const variant = isOverviewVariant(variantValue) ? variantValue : null
        if (!variant) {
          sendJson(response, 400, { error: 'invalid_overview_variant', message: 'variant must be clean or sectors' })
          return
        }

        const resolved = resolveOverviewFile(mapId, variant)
        if (!resolved) {
          sendJson(response, 404, {
            error: 'map_overview_not_ready',
            mapId,
            variant,
            message: 'No development override or generated cache is available for this overview.',
          })
          return
        }

        const etag = '"' + hashFile(resolved.filePath) + '"'
        if (request.headers['if-none-match'] === etag) {
          response.statusCode = 304
          response.end()
          return
        }

        response.statusCode = 200
        response.setHeader('Content-Type', 'image/png')
        response.setHeader('Content-Length', statSync(resolved.filePath).size)
        response.setHeader('Cache-Control', 'no-store')
        response.setHeader('ETag', etag)
        response.setHeader('X-Endfield-Map-Source', resolved.source)
        if (method === 'HEAD') {
          response.end()
          return
        }
        createReadStream(resolved.filePath).on('error', () => {
          if (!response.headersSent) sendJson(response, 500, { error: 'map_overview_read_failed' })
          else response.destroy()
        }).pipe(response)
        return
      }

      if (method === 'GET' && pathname === '/api/v2/maps') {
        sendJson(response, 200, {
          format: 'EndfieldMapCatalog/1',
          cacheRootConfigured: Boolean(getCacheRoot()),
          maps: getSafeMaps(),
        })
        return
      }

      if (method === 'GET' && pathname === '/api/v2/path-picker/status') {
        const pickerRoot = process.env.ENDFIELD_PATH_PICKER_URL?.trim() ?? ''
        const pickerToken = process.env.ENDFIELD_PATH_PICKER_TOKEN?.trim() ?? ''
        let healthUrl: URL | null = null
        try { healthUrl = new URL('/health', pickerRoot) } catch { /* unavailable response below */ }
        if (!healthUrl || healthUrl.hostname !== '127.0.0.1' || !pickerToken) {
          sendJson(response, 503, { format: 'EndfieldPathPickerHealth/1', status: 'unavailable' })
          return
        }
        void fetch(healthUrl, { headers: { 'X-Endfield-Path-Picker-Token': pickerToken } })
          .then(async (bridgeResponse) => {
            const payload = await bridgeResponse.json() as unknown
            sendJson(response, bridgeResponse.ok ? 200 : 502, payload)
          })
          .catch(() => sendJson(response, 502, { format: 'EndfieldPathPickerHealth/1', status: 'unavailable' }))
        return
      }

      if (method === 'POST' && pathname === '/api/v2/path-picker') {
        const pickerRoot = process.env.ENDFIELD_PATH_PICKER_URL?.trim() ?? ''
        const pickerToken = process.env.ENDFIELD_PATH_PICKER_TOKEN?.trim() ?? ''
        let pickerUrl: URL | null = null
        try {
          pickerUrl = new URL('/pick', pickerRoot)
        } catch { /* unavailable response below */ }
        if (!pickerUrl || pickerUrl.hostname !== '127.0.0.1' || !pickerToken) {
          sendJson(response, 503, {
            format: 'EndfieldPathPickerError/1',
            error: 'path_picker_unavailable',
            message: 'Start the WebUI with launch_webui.py to use native path selection.',
          })
          return
        }
        void readRequestBody(request)
          .then(async (rawBody) => {
            let body: { kind?: string; current?: string } = {}
            try { body = rawBody ? JSON.parse(rawBody) as typeof body : {} } catch { /* validation below */ }
            if (!['game', 'output', 'cache', 'blender'].includes(body.kind ?? '') || typeof body.current !== 'string') {
              sendJson(response, 400, { error: 'invalid_path_picker_request' })
              return
            }
            const bridgeResponse = await fetch(pickerUrl, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'X-Endfield-Path-Picker-Token': pickerToken,
              },
              body: JSON.stringify({ kind: body.kind, current: body.current }),
            })
            const payload = await bridgeResponse.json() as unknown
            sendJson(response, bridgeResponse.ok ? 200 : 502, payload)
          })
          .catch(() => sendJson(response, 502, {
            format: 'EndfieldPathPickerError/1',
            error: 'path_picker_bridge_failed',
          }))
        return
      }

      if (method === 'POST' && pathname === '/api/v2/game-source/validate') {
        void readRequestBody(request)
          .then((rawBody) => {
            let body: { gameRoot?: string } = {}
            try { body = rawBody ? JSON.parse(rawBody) as { gameRoot?: string } : {} } catch { /* validation response below */ }
            const gameRoot = body.gameRoot?.trim() ?? ''
            const checks = {
              executable: Boolean(gameRoot) && existsSync(path.join(gameRoot, 'Endfield.exe')),
              globalgamemanagers: Boolean(gameRoot) && existsSync(path.join(gameRoot, 'Endfield_Data', 'globalgamemanagers')),
              vfs: Boolean(gameRoot) && existsSync(path.join(gameRoot, 'Endfield_Data', 'StreamingAssets', 'VFS')),
            }
            const valid = Object.values(checks).every(Boolean)
            sendJson(response, 200, {
              format: 'EndfieldGameSourceValidation/1',
              status: valid ? 'valid' : 'incomplete',
              readOnly: true,
              checks,
              missing: Object.entries(checks).filter(([, ok]) => !ok).map(([key]) => key),
            })
          })
          .catch(() => sendJson(response, 400, { error: 'invalid_request_body' }))
        return
      }

      if (method === 'POST' && pathname === '/api/v2/map-cache-jobs') {
        sendJson(response, 501, {
          error: 'map_cache_generation_not_implemented',
          message: 'P0/P1 runtime serving is available; the Stage113 VFS indexing and tile-generation worker is still pending.',
        })
        return
      }

      next()
    })
  },
})

export default defineConfig({
  plugins: [react(), endfieldMapRuntimeApi(), stage119LayerApi(projectRoot)],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
  preview: {
    host: '127.0.0.1',
    port: 4174,
    strictPort: true,
  },
})
