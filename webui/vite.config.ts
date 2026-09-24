// @ts-nocheck
﻿import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { stage119LayerApi } from './server/stage119LayerApi.ts'
import { mapOverviewApi } from './server/mapOverviewApi.ts'

const projectRoot = path.dirname(fileURLToPath(import.meta.url))

const sendJson = (response: ServerResponse, status: number, body: unknown) => {
  const payload = JSON.stringify(body)
  response.statusCode = status
  response.setHeader('Content-Type', 'application/json; charset=utf-8')
  response.setHeader('Cache-Control', 'no-store')
  response.end(payload)
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
  plugins: [react(), mapOverviewApi(), endfieldMapRuntimeApi(), stage119LayerApi(projectRoot)],
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
