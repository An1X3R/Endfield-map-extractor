// @ts-nocheck
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { defineConfig, loadEnv, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { stage119LayerApi } from './server/stage119LayerApi.ts'
import { mapOverviewApi } from './server/mapOverviewApi.ts'
import { devPreviewAssetApi, parsePreviewAssetRoot } from './server/devPreviewAssetApi.ts'
import { devInstancedScenePreviewApi, parseInstancedScenePreviewRoot } from './server/devInstancedScenePreviewApi.ts'

const projectRoot = path.dirname(fileURLToPath(import.meta.url))
const API_PREFIXES = ['/api/v2', '/api/v3'] as const
const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])
const MAX_REQUEST_BODY_BYTES = 1024 * 1024

const bridgeMethods = (pathname: string): string[] | null => {
  if (/^\/api\/v2\/asset-catalogs\/(weapon|illustration|display_prop)$/.test(pathname) ||
      /^\/api\/v2\/asset-jobs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(pathname)) return ['GET']
  if (pathname === '/api/v2/asset-jobs' || pathname === '/api/v2/asset-jobs/validate' ||
      /^\/api\/v2\/asset-jobs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/cancel$/.test(pathname)) return ['POST']
  if (pathname === '/api/v2/first-run' || pathname === '/api/v2/level-map-preparation') return ['GET', 'POST']
  if (/^\/api\/v2\/level-map-preparation\/[a-f0-9]{32}$/.test(pathname)) return ['GET']
  if (/^\/api\/v2\/level-map-preparation\/[a-f0-9]{32}\/cancel$/.test(pathname)) return ['POST']
  if (/^\/api\/v2\/(level-maps|instanced-scenes|characters)$/.test(pathname) ||
      /^\/api\/v2\/level-maps\/map\d{2}_lv\d{3}\/image$/.test(pathname) ||
      /^\/api\/v2\/instanced-scenes\/[a-z][a-z0-9_]*(\/preview)?$/.test(pathname) ||
      /^\/api\/v2\/characters\/[A-Za-z0-9_:.-]+(\/animations)?$/.test(pathname) ||
      pathname === '/api/v2/first-run/events' ||
      (/^\/api\/v2\/(export-jobs|character-jobs|character-preparation|instanced-scene-jobs)\/[A-Za-z0-9._-]+(\/events)?$/.test(pathname) && !pathname.endsWith('/validate'))) return ['GET']
  if (/^\/api\/v2\/first-run\/(validate|cancel)$/.test(pathname) ||
      /^\/api\/v2\/(export-jobs|character-jobs|character-preparation|instanced-scene-jobs)(\/validate|\/[A-Za-z0-9._-]+\/cancel)?$/.test(pathname)) return ['POST']
  return null
}

const bridgeFamilies = /^\/api\/v2\/(asset-catalogs|asset-jobs|level-maps|level-map-preparation|instanced-scenes|characters|character-jobs|character-preparation|instanced-scene-jobs|first-run|export-jobs)(\/|$)/

const bridgeUrl = (pathname: string, search: string): URL | null => {
  const root = process.env.ENDFIELD_PATH_PICKER_URL?.trim() ?? ''
  if (!root) return null
  const parsed = new URL(root)
  if (parsed.protocol !== 'http:' || parsed.hostname !== '127.0.0.1' || parsed.username || parsed.password ||
      parsed.pathname !== '/' || parsed.search || parsed.hash) return null
  return new URL(`${pathname}${search}`, parsed)
}

const parseBackendOrigin = (value: string): string | null => {
  if (!value.trim()) return null
  let target: URL
  try {
    target = new URL(value)
  } catch {
    throw new Error('ENDFIELD_BACKEND_URL must be an absolute HTTP or HTTPS origin.')
  }
  if ((target.protocol !== 'http:' && target.protocol !== 'https:') || target.username || target.password ||
      target.pathname !== '/' || target.search || target.hash) {
    throw new Error('ENDFIELD_BACKEND_URL must be an HTTP or HTTPS origin without credentials, path, query, or fragment.')
  }
  return target.origin
}

const apiBoundary = (): Plugin => ({
  name: 'endfield-api-boundary',
  configureServer(server) {
    installApiBoundary(server.middlewares)
  },
  configurePreviewServer(server) {
    installApiBoundary(server.middlewares)
  },
})

const apiMutationOriginGuard = (): Plugin => ({
  name: 'endfield-api-mutation-origin-guard',
  configureServer(server) {
    installApiMutationOriginGuard(server.middlewares)
  },
  configurePreviewServer(server) {
    installApiMutationOriginGuard(server.middlewares)
  },
})

const installApiMutationOriginGuard = (middlewares: { use: (handler: (request: IncomingMessage, response: ServerResponse, next: () => void) => void) => void }): void => {
  middlewares.use((request, response, next) => {
    const rawPath = (request.url ?? '/').split('?')[0]
    if (rawPath.startsWith('/api/')) {
      let safePath = false
      try {
        safePath = rawPath.split('/').every(segment => {
          const decoded = decodeURIComponent(segment)
          return decoded !== '.' && decoded !== '..' && !/[\\/%\x00-\x1f]/.test(decoded)
        })
      } catch { /* invalid encoding is rejected below */ }
      if (!safePath) {
        sendJson(response, 400, { error: 'invalid_api_path', message: 'API path segments must not contain traversal or encoded separators.' })
        return
      }
    }
    const pathname = new URL(request.url ?? '/', 'http://127.0.0.1').pathname
    const method = (request.method ?? 'GET').toUpperCase()
    if (!API_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`)) || !MUTATING_METHODS.has(method)) {
      next()
      return
    }
    if (isSameOriginMutationRequest(request)) {
      next()
      return
    }
    sendJson(response, 403, {
      format: 'EndfieldRequestOriginError/1',
      error: 'cross_origin_mutation_rejected',
      message: 'Mutating API requests must be same-origin.',
    })
  })
}

const isSameOriginMutationRequest = (request: IncomingMessage): boolean => {
  const fetchSite = request.headers['sec-fetch-site']
  if (fetchSite !== undefined && (Array.isArray(fetchSite) || fetchSite !== 'same-origin')) return false

  const origin = request.headers.origin
  if (origin === undefined) return true
  const host = request.headers.host
  if (Array.isArray(origin) || !host || Array.isArray(host)) return false
  try {
    const suppliedOrigin = new URL(origin)
    const requestOrigin = new URL(`${request.socket.encrypted ? 'https' : 'http'}://${host}`)
    return suppliedOrigin.origin === origin && suppliedOrigin.origin === requestOrigin.origin
  } catch {
    return false
  }
}

const installApiBoundary = (middlewares: { use: (handler: (request: IncomingMessage, response: ServerResponse, next: () => void) => void) => void }): void => {
  middlewares.use((request, response, next) => {
    const pathname = new URL(request.url ?? '/', 'http://127.0.0.1').pathname
    if (!API_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`))) {
      next()
      return
    }
    sendJson(response, 503, {
      format: 'EndfieldBackendBoundaryError/1',
      error: 'backend_route_unavailable',
      message: 'Start the WebUI with launch_webui.py or configure ENDFIELD_BACKEND_URL.',
    })
  })
}

const sendJson = (response: ServerResponse, status: number, body: unknown) => {
  const payload = JSON.stringify(body)
  response.statusCode = status
  response.setHeader('Content-Type', 'application/json; charset=utf-8')
  response.setHeader('Cache-Control', 'no-store')
  response.end(payload)
}

class RequestBodyTooLargeError extends Error {
  constructor() {
    super(`Request body exceeds ${MAX_REQUEST_BODY_BYTES} bytes.`)
    this.name = 'RequestBodyTooLargeError'
  }
}

const readRequestBody = (request: IncomingMessage): Promise<string> =>
  new Promise<string>((resolve, reject) => {
    const chunks: Buffer[] = []
    let byteCount = 0
    let oversized = false
    let settled = false
    request.on('data', (chunk: Buffer | string) => {
      if (oversized) return
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
      byteCount += bytes.byteLength
      if (byteCount > MAX_REQUEST_BODY_BYTES) {
        oversized = true
        chunks.length = 0
        settled = true
        reject(new RequestBodyTooLargeError())
        return
      }
      chunks.push(bytes)
    })
    request.on('end', () => {
      if (oversized) return
      settled = true
      resolve(Buffer.concat(chunks, byteCount).toString('utf8'))
    })
    request.on('error', (error) => {
      if (settled) return
      settled = true
      reject(error)
    })
  })

const sendBodySizeError = (response: ServerResponse, error: unknown): boolean => {
  if (!(error instanceof RequestBodyTooLargeError)) return false
  sendJson(response, 413, {
    error: 'request_body_too_large',
    message: error.message,
    maxBytes: MAX_REQUEST_BODY_BYTES,
  })
  return true
}

const endfieldMapRuntimeApi = (): Plugin => ({
  name: 'endfield-map-runtime-api',
  configureServer(server) {
    installRuntimeApi(server.middlewares)
  },
  configurePreviewServer(server) {
    installRuntimeApi(server.middlewares)
  },
})

const installRuntimeApi = (middlewares: { use: (handler: (request: IncomingMessage, response: ServerResponse, next: () => void) => void) => void }): void => {
  middlewares.use((request, response, next) => {
    const method = (request.method ?? 'GET').toUpperCase()
    const requestUrl = new URL(request.url ?? '/', 'http://127.0.0.1')
    const pathname = requestUrl.pathname

    const allowedMethods = bridgeMethods(decodeURIComponent(pathname))
    if (bridgeFamilies.test(pathname)) {
      if (!allowedMethods) {
        sendJson(response, 404, { error: 'bridge_route_not_found', message: 'No bridge route matches this API path.' })
        return
      }
      if (!allowedMethods.includes(method)) {
        response.setHeader('Allow', allowedMethods.join(', '))
        sendJson(response, 405, { error: 'bridge_method_not_allowed', message: `This bridge route accepts ${allowedMethods.join(', ')}.` })
        return
      }
    }

    const levelMapImageRoute = pathname.match(/^\/api\/v2\/level-maps\/(map\d{2}_lv\d{3})\/image$/)
    const sceneBridgeRoute = pathname === '/api/v2/level-maps'
      ? { publicRoot: '/api/v2/level-maps', bridgeRoot: '/level-maps', methods: ['GET'], binary: false, kind: 'level_map' }
      : levelMapImageRoute
        ? { publicRoot: pathname, bridgeRoot: `/level-maps/${levelMapImageRoute[1]}/image`, methods: ['GET'], binary: true, kind: 'level_map' }
        : pathname === '/api/v2/instanced-scenes' || pathname.startsWith('/api/v2/instanced-scenes/')
          ? { publicRoot: '/api/v2/instanced-scenes', bridgeRoot: '/instanced-scenes', methods: ['GET', 'POST'], binary: false, kind: 'instanced_scene' }
          : pathname === '/api/v2/instanced-scene-jobs' || pathname.startsWith('/api/v2/instanced-scene-jobs/')
            ? { publicRoot: '/api/v2/instanced-scene-jobs', bridgeRoot: '/instanced-scene-jobs', methods: ['GET', 'POST'], binary: false, kind: 'instanced_scene' }
            : ['/asset-catalogs', '/asset-jobs', '/characters', '/character-jobs', '/character-preparation', '/level-map-preparation']
              .map((bridgeRoot) => ({ publicRoot: `/api/v2${bridgeRoot}`, bridgeRoot, methods: ['GET', 'POST'], binary: false, kind: bridgeRoot.startsWith('/asset-') ? 'asset' : 'instanced_scene' }))
              .find((route) => pathname === route.publicRoot || pathname.startsWith(`${route.publicRoot}/`)) ?? null
    if (sceneBridgeRoute) {
      if (!sceneBridgeRoute.methods.includes(method)) {
        const allow = sceneBridgeRoute.methods.join(', ')
        response.setHeader('Allow', allow)
        sendJson(response, 405, {
          error: sceneBridgeRoute.kind === 'level_map' ? 'level_map_method_not_allowed' : 'instanced_scene_method_not_allowed',
          message: `${sceneBridgeRoute.kind === 'level_map' ? 'Level map' : 'Independent scene bridge'} routes accept ${allow}.`,
        })
        return
      }
      const bridgeToken = process.env.ENDFIELD_PATH_PICKER_TOKEN?.trim() ?? ''
      let targetUrl: URL | null = null
      try {
        const suffix = pathname.slice(sceneBridgeRoute.publicRoot.length)
        targetUrl = bridgeUrl(`${sceneBridgeRoute.bridgeRoot}${suffix}`, requestUrl.search)
      } catch { /* unavailable response below */ }
      if (!targetUrl || targetUrl.hostname !== '127.0.0.1' || !bridgeToken) {
        sendJson(response, 503, sceneBridgeRoute.kind === 'asset'
          ? { error: 'asset_bridge_unavailable', message: 'Asset bridge unavailable; start the WebUI with launch_webui.py.' }
          : sceneBridgeRoute.kind === 'level_map'
          ? { error: 'level_map_bridge_unavailable', message: 'Level map bridge unavailable.' }
          : { error: 'instanced_scene_bridge_unavailable', message: 'Independent scene bridge unavailable.' })
        return
      }
      void (async () => {
        const rawBody = method === 'POST' ? await readRequestBody(request) : undefined
        const bridgeResponse = await fetch(targetUrl, {
          method,
          redirect: 'error',
          headers: {
            ...(rawBody !== undefined ? { 'Content-Type': 'application/json' } : {}),
            'X-Endfield-Path-Picker-Token': bridgeToken,
          },
          body: rawBody,
        })
        const payload = sceneBridgeRoute.binary
          ? Buffer.from(await bridgeResponse.arrayBuffer())
          : await bridgeResponse.text()
        response.statusCode = bridgeResponse.status
        response.setHeader('Content-Type', bridgeResponse.headers.get('content-type') ?? 'application/json; charset=utf-8')
        response.setHeader('Cache-Control', 'no-store')
        response.end(payload)
      })().catch((error) => {
        if (sendBodySizeError(response, error)) return
        sendJson(response, 502, {
          error: sceneBridgeRoute.kind === 'asset' ? 'asset_bridge_failed' : sceneBridgeRoute.kind === 'level_map' ? 'level_map_bridge_failed' : 'instanced_scene_bridge_failed',
          message: error instanceof Error ? error.message : String(error),
        })
      })
      return
    }

    if (pathname === '/api/v2/first-run' || pathname.startsWith('/api/v2/first-run/')) {
      const bridgeToken = process.env.ENDFIELD_PATH_PICKER_TOKEN?.trim() ?? ''
      let targetUrl: URL | null = null
      try {
        const suffix = pathname.slice('/api/v2/first-run'.length)
        targetUrl = bridgeUrl(`/first-run${suffix}`, requestUrl.search)
      } catch { /* unavailable response below */ }
      if (!targetUrl || targetUrl.hostname !== '127.0.0.1' || !bridgeToken) {
        sendJson(response, 503, { error: { code: 'first_run_bridge_unavailable', message: 'First-run bridge unavailable.', retryable: true } })
        return
      }
      const forward = async () => {
        const rawBody = method === 'POST' ? await readRequestBody(request) : undefined
        const bridgeResponse = await fetch(targetUrl, {
          method,
          redirect: 'error',
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
      void forward().catch((error) => {
        if (sendBodySizeError(response, error)) return
        sendJson(response, 502, {
          error: { code: 'first_run_bridge_failed', message: error instanceof Error ? error.message : String(error), retryable: true },
        })
      })
      return
    }

    if (pathname === '/api/v2/export-jobs' || pathname.startsWith('/api/v2/export-jobs/')) {
      const bridgeToken = process.env.ENDFIELD_PATH_PICKER_TOKEN?.trim() ?? ''
      let targetUrl: URL | null = null
      try {
        const suffix = pathname.slice('/api/v2/export-jobs'.length)
        targetUrl = bridgeUrl(`/jobs${suffix}`, requestUrl.search)
      } catch { /* unavailable response below */ }
      if (!targetUrl || targetUrl.hostname !== '127.0.0.1' || !bridgeToken) {
        sendJson(response, 503, { error: 'export_job_bridge_unavailable' })
        return
      }
      const forward = async () => {
        const rawBody = method === 'POST' ? await readRequestBody(request) : undefined
        const bridgeResponse = await fetch(targetUrl, {
          method,
          redirect: 'error',
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
      void forward().catch((error) => {
        if (sendBodySizeError(response, error)) return
        sendJson(response, 502, {
          error: 'export_job_bridge_failed',
          message: error instanceof Error ? error.message : String(error),
        })
      })
      return
    }

    if (method === 'GET' && pathname === '/api/v2/path-picker/status') {
      const pickerToken = process.env.ENDFIELD_PATH_PICKER_TOKEN?.trim() ?? ''
      let healthUrl: URL | null = null
      try { healthUrl = bridgeUrl('/health', '') } catch { /* unavailable response below */ }
      if (!healthUrl || healthUrl.hostname !== '127.0.0.1' || !pickerToken) {
        sendJson(response, 503, { format: 'EndfieldPathPickerHealth/1', status: 'unavailable' })
        return
      }
      void fetch(healthUrl, { redirect: 'error', headers: { 'X-Endfield-Path-Picker-Token': pickerToken } })
        .then(async (bridgeResponse) => {
          const payload = await bridgeResponse.json() as unknown
          sendJson(response, bridgeResponse.ok ? 200 : 502, payload)
        })
        .catch(() => sendJson(response, 502, { format: 'EndfieldPathPickerHealth/1', status: 'unavailable' }))
      return
    }

    if (method === 'POST' && pathname === '/api/v2/path-picker') {
      const pickerToken = process.env.ENDFIELD_PATH_PICKER_TOKEN?.trim() ?? ''
      let pickerUrl: URL | null = null
      try {
        pickerUrl = bridgeUrl('/pick', '')
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
            redirect: 'error',
            headers: {
              'Content-Type': 'application/json',
              'X-Endfield-Path-Picker-Token': pickerToken,
            },
            body: JSON.stringify({ kind: body.kind, current: body.current }),
          })
          const payload = await bridgeResponse.json() as unknown
          sendJson(response, bridgeResponse.ok ? 200 : 502, payload)
        })
        .catch((error) => {
          if (sendBodySizeError(response, error)) return
          sendJson(response, 502, {
            format: 'EndfieldPathPickerError/1',
            error: 'path_picker_bridge_failed',
          })
        })
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
        .catch((error) => {
          if (sendBodySizeError(response, error)) return
          sendJson(response, 400, { error: 'invalid_request_body' })
        })
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
}

export default defineConfig(({ mode }) => {
  const fileEnv = loadEnv(mode, projectRoot, '')
  const bridgeOrigin = process.env.ENDFIELD_PATH_PICKER_URL?.trim()
  const cacheScope = bridgeOrigin ? `bridge-${new URL(bridgeOrigin).port}` : 'standalone'
  const backendValue = process.env.ENDFIELD_BACKEND_URL ?? fileEnv.ENDFIELD_BACKEND_URL ?? ''
  const assetValue = process.env.ENDFIELD_PREVIEW_ASSET_ROOT ?? fileEnv.ENDFIELD_PREVIEW_ASSET_ROOT ?? ''
  const sceneAssetValue = process.env.ENDFIELD_SCENE_PREVIEW_ASSET_ROOT ?? fileEnv.ENDFIELD_SCENE_PREVIEW_ASSET_ROOT ?? ''
  const backend = parseBackendOrigin(backendValue)
  const assetRoot = parsePreviewAssetRoot(assetValue)
  const sceneAssetRoot = parseInstancedScenePreviewRoot(sceneAssetValue)
  if (backend && assetRoot) {
    throw new Error('Set either ENDFIELD_BACKEND_URL or ENDFIELD_PREVIEW_ASSET_ROOT, not both.')
  }
  const proxy = backend ? {
    '/api/v2': { target: backend, changeOrigin: true },
    '/api/v3': { target: backend, changeOrigin: true },
  } : undefined
  return {
    cacheDir: path.join(projectRoot, 'node_modules', '.vite', cacheScope),
    plugins: backend
      ? [apiMutationOriginGuard(), react(), devInstancedScenePreviewApi(sceneAssetRoot)]
      : [apiMutationOriginGuard(), react(), devPreviewAssetApi(assetRoot), devInstancedScenePreviewApi(sceneAssetRoot), mapOverviewApi(), endfieldMapRuntimeApi(), stage119LayerApi(projectRoot), apiBoundary()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      strictPort: true,
      proxy,
    },
    preview: {
      host: '127.0.0.1',
      port: 4174,
      strictPort: true,
      proxy,
    },
  }
})
