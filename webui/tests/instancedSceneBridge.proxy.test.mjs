import assert from 'node:assert/strict'
import { createServer as createHttpServer } from 'node:http'
import { createServer as createViteServer } from 'vite'

const envKeys = [
  'ENDFIELD_BACKEND_URL',
  'ENDFIELD_PATH_PICKER_URL',
  'ENDFIELD_PATH_PICKER_TOKEN',
  'ENDFIELD_PREVIEW_ASSET_ROOT',
  'ENDFIELD_SCENE_PREVIEW_ASSET_ROOT',
]
const previousEnvironment = new Map(envKeys.map((key) => [key, process.env[key]]))
const forwardedRequests = []
const bridge = createHttpServer((request, response) => {
  let body = ''
  request.setEncoding('utf8')
  request.on('data', (chunk) => { body += chunk })
  request.on('end', () => {
    forwardedRequests.push({
      method: request.method,
      url: request.url,
      token: request.headers['x-endfield-path-picker-token'],
      body,
    })
    if (request.method === 'GET') {
      response.statusCode = 418
      response.setHeader('Content-Type', 'text/plain; charset=utf-8')
      response.end('upstream error is preserved')
      return
    }
    response.statusCode = 202
    response.setHeader('Content-Type', 'application/json; charset=utf-8')
    response.end('{"accepted":true}')
  })
})

const listen = (server) => new Promise((resolve, reject) => {
  server.once('error', reject)
  server.listen(0, '127.0.0.1', () => {
    server.off('error', reject)
    resolve(server.address())
  })
})

const close = (server) => new Promise((resolve, reject) => {
  server.close((error) => error ? reject(error) : resolve())
})

let viteServer
let bridgeStarted = false
try {
  const bridgeAddress = await listen(bridge)
  bridgeStarted = true
  if (!bridgeAddress || typeof bridgeAddress === 'string') throw new Error('Local bridge did not expose a TCP address.')
  process.env.ENDFIELD_BACKEND_URL = ''
  process.env.ENDFIELD_PATH_PICKER_URL = `http://127.0.0.1:${bridgeAddress.port}`
  process.env.ENDFIELD_PATH_PICKER_TOKEN = 'instanced-scene-test-token'
  process.env.ENDFIELD_PREVIEW_ASSET_ROOT = ''
  process.env.ENDFIELD_SCENE_PREVIEW_ASSET_ROOT = ''
  viteServer = await createViteServer({
    configFile: 'vite.config.ts',
    server: { host: '127.0.0.1', port: 5197, strictPort: false },
  })
  await viteServer.listen()
  const viteAddress = viteServer.httpServer?.address()
  if (!viteAddress || typeof viteAddress === 'string') throw new Error('Vite dev server did not expose a TCP address.')
  const origin = `http://127.0.0.1:${viteAddress.port}`

  const getResponse = await fetch(`${origin}/api/v2/instanced-scenes/scene-001?catalog_id=cat-01&after=12`)
  assert.equal(getResponse.status, 418)
  assert.equal(await getResponse.text(), 'upstream error is preserved')
  assert.deepEqual(forwardedRequests[0], {
    method: 'GET',
    url: '/instanced-scenes/scene-001?catalog_id=cat-01&after=12',
    token: 'instanced-scene-test-token',
    body: '',
  })

  const postBody = '{"scene_id":"scene-001","request_id":"test-request"}'
  const postResponse = await fetch(`${origin}/api/v2/instanced-scene-jobs/validate?mode=check`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: postBody,
  })
  assert.equal(postResponse.status, 202)
  assert.equal(await postResponse.text(), '{"accepted":true}')
  assert.deepEqual(forwardedRequests[1], {
    method: 'POST',
    url: '/instanced-scene-jobs/validate?mode=check',
    token: 'instanced-scene-test-token',
    body: postBody,
  })

  const crossOriginResponse = await fetch(`${origin}/api/v2/instanced-scenes`, {
    method: 'POST',
    headers: { Origin: 'https://attacker.example', 'Content-Type': 'application/json' },
    body: '{}',
  })
  assert.equal(crossOriginResponse.status, 403)
  assert.equal((await crossOriginResponse.json()).error, 'cross_origin_mutation_rejected')
  assert.equal(forwardedRequests.length, 2)

  const crossSiteMetadataResponse = await fetch(`${origin}/api/v2/instanced-scenes`, {
    method: 'POST',
    headers: { Origin: origin, 'Sec-Fetch-Site': 'cross-site', 'Content-Type': 'application/json' },
    body: '{}',
  })
  assert.equal(crossSiteMetadataResponse.status, 403)
  assert.equal(forwardedRequests.length, 2)

  const sameOriginResponse = await fetch(`${origin}/api/v2/instanced-scenes`, {
    method: 'POST',
    headers: { Origin: origin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json' },
    body: '{"source":"same-origin"}',
  })
  assert.equal(sameOriginResponse.status, 202)
  assert.equal(forwardedRequests.at(-1).body, '{"source":"same-origin"}')

  const oversizedBody = 'é'.repeat(786433)
  assert.ok(oversizedBody.length < 1024 * 1024)
  const oversizedResponse = await fetch(`${origin}/api/v2/instanced-scenes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: oversizedBody,
  })
  assert.equal(oversizedResponse.status, 413)
  const oversizedError = await oversizedResponse.json()
  assert.equal(oversizedError.error, 'request_body_too_large')
  assert.equal(oversizedError.maxBytes, 1024 * 1024)
  assert.equal(forwardedRequests.length, 3)

  const methodResponse = await fetch(`${origin}/api/v2/instanced-scenes`, { method: 'PUT' })
  assert.equal(methodResponse.status, 405)
  assert.equal(forwardedRequests.length, 3)

  for (const publicPath of [
    '/api/v2/characters/character%3Atest?catalog_id=cat-01',
    '/api/v2/character-jobs/job-001/events?after=12&limit=1',
    '/api/v2/character-preparation/prep-001/events?after=0',
  ]) {
    const response = await fetch(`${origin}${publicPath}`)
    assert.equal(response.status, 418)
    assert.equal(await response.text(), 'upstream error is preserved')
    assert.equal(forwardedRequests.at(-1).url, publicPath.slice('/api/v2'.length))
    assert.equal(forwardedRequests.at(-1).token, 'instanced-scene-test-token')
  }
  const preparation = await fetch(`${origin}/api/v2/character-preparation/validate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"format":"CharacterPreparationRequest/1"}',
  })
  assert.equal(preparation.status, 202)
  assert.equal(forwardedRequests.at(-1).url, '/character-preparation/validate')
  assert.equal(forwardedRequests.at(-1).body, '{"format":"CharacterPreparationRequest/1"}')

  await close(bridge)
  bridgeStarted = false
  const failedBridgeResponse = await fetch(`${origin}/api/v2/instanced-scenes`)
  assert.equal(failedBridgeResponse.status, 502)
  const failedBridgeBody = await failedBridgeResponse.json()
  assert.equal(failedBridgeBody.error, 'instanced_scene_bridge_failed')
  assert.equal(typeof failedBridgeBody.message, 'string')

  await viteServer.close()
  viteServer = undefined
  const backendAddress = await listen(bridge)
  bridgeStarted = true
  if (!backendAddress || typeof backendAddress === 'string') throw new Error('Local backend did not expose a TCP address.')
  process.env.ENDFIELD_BACKEND_URL = `http://127.0.0.1:${backendAddress.port}`
  viteServer = await createViteServer({
    configFile: 'vite.config.ts',
    server: { host: '127.0.0.1', port: 5198, strictPort: false },
  })
  await viteServer.listen()
  const backendViteAddress = viteServer.httpServer?.address()
  if (!backendViteAddress || typeof backendViteAddress === 'string') throw new Error('Backend proxy Vite server did not expose a TCP address.')
  const backendOrigin = `http://127.0.0.1:${backendViteAddress.port}`
  const forwardedCountBeforeBackend = forwardedRequests.length
  const backendDeniedResponse = await fetch(`${backendOrigin}/api/v2/proxy-check`, {
    method: 'POST',
    headers: { Origin: 'https://attacker.example', 'Content-Type': 'application/json' },
    body: '{}',
  })
  assert.equal(backendDeniedResponse.status, 403)
  assert.equal(forwardedRequests.length, forwardedCountBeforeBackend)
  const backendAllowedResponse = await fetch(`${backendOrigin}/api/v2/proxy-check`, {
    method: 'POST',
    headers: { Origin: backendOrigin, 'Content-Type': 'application/json' },
    body: '{"source":"backend-proxy"}',
  })
  assert.equal(backendAllowedResponse.status, 202)
  assert.equal(forwardedRequests.length, forwardedCountBeforeBackend + 1)
  assert.equal(forwardedRequests.at(-1).url, '/api/v2/proxy-check')
  assert.equal(forwardedRequests.at(-1).body, '{"source":"backend-proxy"}')
  console.log('Instanced scene local bridge forwarding passed.')
} finally {
  if (viteServer) await viteServer.close()
  if (bridgeStarted) await close(bridge)
  for (const [key, value] of previousEnvironment) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
}
