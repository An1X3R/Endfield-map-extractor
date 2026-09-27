// @ts-expect-error Node built-in types are intentionally absent from the frontend dependency set.
import * as fs from 'node:fs'
// @ts-expect-error Node built-in types are intentionally absent from the frontend dependency set.
import * as paths from 'node:path'
import type { Connect, Plugin } from 'vite'

const PNG_SIGNATURE = Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10])

type PreviewRequest = { url?: string; method?: string }
type PreviewResponse = {
  statusCode: number
  headersSent: boolean
  setHeader: (name: string, value: string | number) => void
  end: (body?: string) => void
  destroy: (error: Error) => void
}
type RuntimeUrl = {
  pathname: string
  searchParams: { get: (name: string) => string | null }
}
type RuntimeUrlConstructor = new (value: string, base: string) => RuntimeUrl
const RuntimeURL = (globalThis as typeof globalThis & { URL: RuntimeUrlConstructor }).URL

const sendError = (response: PreviewResponse, status: number, error: string, message: string): void => {
  response.statusCode = status
  response.setHeader('Content-Type', 'application/json; charset=utf-8')
  response.setHeader('Cache-Control', 'no-store')
  response.end(JSON.stringify({ error, message }))
}

export const parsePreviewAssetRoot = (value: string): string | null => {
  if (!value.trim()) return null
  const root: string = paths.resolve(value)
  if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) {
    throw new Error('ENDFIELD_PREVIEW_ASSET_ROOT must point to an existing directory.')
  }
  return fs.realpathSync(root) as string
}

export const devPreviewAssetApi = (root: string | null): Plugin => ({
  name: 'endfield-dev-preview-assets',
  configureServer(server) {
    if (!root) return
    server.middlewares.use((rawRequest, rawResponse, next) => {
      const request = rawRequest as Connect.IncomingMessage & PreviewRequest
      const response = rawResponse as unknown as PreviewResponse
      const method = request.method ?? 'GET'
      const requestUrl = new RuntimeURL(request.url ?? '/', 'http://127.0.0.1')
      const match = requestUrl.pathname.match(/^\/api\/v2\/maps\/(map01|map02)\/overview$/)
      if (!match || (method !== 'GET' && method !== 'HEAD')) {
        next()
        return
      }
      if ((requestUrl.searchParams.get('variant') ?? 'clean') !== 'clean') {
        sendError(response, 404, 'preview_variant_unavailable', 'Only clean overview PNGs are available from the development preview root.')
        return
      }
      const candidate: string = paths.resolve(root, `${match[1]}.png`)
      if (!fs.existsSync(candidate)) {
        sendError(response, 404, 'map_overview_missing', `Preview image ${match[1]}.png is missing.`)
        return
      }
      const filePath = fs.realpathSync(candidate) as string
      const relative: string = paths.relative(root, filePath)
      if (relative === '..' || relative.startsWith(`..${paths.sep}`) || paths.isAbsolute(relative)) {
        sendError(response, 403, 'unsafe_preview_asset', 'Preview image must resolve inside ENDFIELD_PREVIEW_ASSET_ROOT.')
        return
      }
      const stats = fs.statSync(filePath) as { isFile: () => boolean; size: number }
      if (!stats.isFile()) {
        sendError(response, 415, 'invalid_preview_asset', 'Preview image must be a PNG file.')
        return
      }
      const signature = new Uint8Array(PNG_SIGNATURE.length)
      const descriptor: number = fs.openSync(filePath, 'r')
      try {
        fs.readSync(descriptor, signature, 0, signature.length, 0)
      } finally {
        fs.closeSync(descriptor)
      }
      if (!signature.every((byte, index) => byte === PNG_SIGNATURE[index])) {
        sendError(response, 415, 'invalid_preview_asset', 'Preview image must be a PNG file.')
        return
      }
      response.statusCode = 200
      response.setHeader('Content-Type', 'image/png')
      response.setHeader('Content-Length', stats.size)
      response.setHeader('Cache-Control', 'no-store')
      response.setHeader('X-Endfield-Map-Source', 'dev-override')
      if (method === 'HEAD') response.end()
      else fs.createReadStream(filePath)
        .once('error', (error: Error) => {
          if (!response.headersSent) {
            sendError(response, 500, 'preview_asset_read_failed', error.message)
          } else {
            response.destroy(error)
          }
        })
        .pipe(rawResponse)
    })
  },
})
