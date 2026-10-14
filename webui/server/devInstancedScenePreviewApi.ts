// @ts-expect-error Node built-in types are intentionally absent from the frontend dependency set.
import * as fs from 'node:fs'
// @ts-expect-error Node built-in types are intentionally absent from the frontend dependency set.
import * as path from 'node:path'
import type { Connect, Plugin } from 'vite'

const ASSET_ROUTE = '/api/v2/dev/instanced-scene-preview/office_preview.png'
const PNG_SIGNATURE = Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10])
type RuntimeUrl = { pathname: string }
type RuntimeUrlConstructor = new (value: string, base: string) => RuntimeUrl
type PreviewRequest = { url?: string; method?: string }
const RuntimeURL = (globalThis as typeof globalThis & { URL: RuntimeUrlConstructor }).URL

/** Resolves an explicit external asset root without copying its contents. */
export function parseInstancedScenePreviewRoot(value: string): string | null {
  if (!value.trim()) return null
  const root: string = path.resolve(value)
  if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) {
    throw new Error('ENDFIELD_SCENE_PREVIEW_ASSET_ROOT must point to an existing directory.')
  }
  return fs.realpathSync(root) as string
}

/** Serves one whitelisted local PNG only from the development server. */
export function devInstancedScenePreviewApi(root: string | null): Plugin {
  return {
    name: 'endfield-dev-instanced-scene-preview',
    configureServer(server) {
      server.middlewares.use((rawRequest, response, next) => {
        const request = rawRequest as Connect.IncomingMessage & PreviewRequest
        const pathname = new RuntimeURL(request.url ?? '/', 'http://127.0.0.1').pathname
        if (pathname !== ASSET_ROUTE) {
          next()
          return
        }
        const fail = (status: number, code: string, message: string): void => {
          response.statusCode = status
          response.setHeader('Content-Type', 'application/json; charset=utf-8')
          response.setHeader('Cache-Control', 'no-store')
          response.end(JSON.stringify({ code, message }))
        }
        if (request.method !== 'GET' && request.method !== 'HEAD') {
          response.setHeader('Allow', 'GET, HEAD')
          fail(405, 'scene_preview_method_not_allowed', 'The development scene preview accepts GET or HEAD.')
          return
        }
        if (!root) {
          fail(503, 'scene_preview_root_unconfigured', 'Set ENDFIELD_SCENE_PREVIEW_ASSET_ROOT to the external development preview directory.')
          return
        }
        const candidate: string = path.join(root, 'office_preview.png')
        if (!fs.existsSync(candidate)) {
          fail(404, 'scene_preview_missing', 'office_preview.png is missing from the external development preview directory.')
          return
        }
        const resolved: string = fs.realpathSync(candidate)
        const relative: string = path.relative(root, resolved)
        if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
          fail(403, 'scene_preview_path_unsafe', 'office_preview.png must resolve inside the configured development preview directory.')
          return
        }
        const stat = fs.statSync(resolved)
        if (!stat.isFile()) {
          fail(415, 'scene_preview_invalid', 'office_preview.png must be a regular PNG file.')
          return
        }
        const signature = new Uint8Array(PNG_SIGNATURE.length)
        const descriptor: number = fs.openSync(resolved, 'r')
        try {
          fs.readSync(descriptor, signature, 0, signature.length, 0)
        } finally {
          fs.closeSync(descriptor)
        }
        if (!signature.every((byte, index) => byte === PNG_SIGNATURE[index])) {
          fail(415, 'scene_preview_invalid', 'office_preview.png does not have a valid PNG signature.')
          return
        }
        response.statusCode = 200
        response.setHeader('Content-Type', 'image/png')
        response.setHeader('Content-Length', stat.size)
        response.setHeader('Cache-Control', 'no-store')
        response.setHeader('X-Endfield-Scene-Source', 'development-preview')
        if (request.method === 'HEAD') response.end()
        else fs.createReadStream(resolved).once('error', (error: Error) => {
          if (response.headersSent) response.destroy(error)
          else fail(500, 'scene_preview_read_failed', error.message)
        }).pipe(response)
      })
    },
  }
}
