import assert from 'node:assert/strict'
import { createServer } from 'node:http'

import { stage119LayerApi } from '../server/stage119LayerApi.ts'

const plugin = stage119LayerApi(process.cwd())
let middleware
plugin.configureServer({
  middlewares: {
    use(handler) {
      middleware = handler
    },
  },
})

assert.equal(typeof middleware, 'function')

const server = createServer((request, response) => {
  middleware(request, response, () => {
    response.statusCode = 404
    response.end('not_found')
  })
})

await new Promise((resolve, reject) => {
  server.once('error', reject)
  server.listen(0, '127.0.0.1', resolve)
})

const address = server.address()
assert(address && typeof address === 'object')
const baseUrl = `http://127.0.0.1:${address.port}`
const forbiddenBrowserFields = /(?:sourcePath|meshPath|rootAssetPath|pathId|[A-Za-z]:\\)/i

const getJson = async (pathname, init) => {
  const response = await fetch(`${baseUrl}${pathname}`, init)
  const text = response.status === 304 ? '' : await response.text()
  return {
    response,
    text,
    json: text ? JSON.parse(text) : null,
  }
}

try {
  const catalogResult = await getJson('/api/v3/layer-catalog')
  assert.equal(catalogResult.response.status, 200)
  assert.equal(catalogResult.json.dataset.auditStatus, 'passed')
  assert.match(catalogResult.json.dataset.datasetFingerprint, /^[0-9A-F]{64}$/)
  assert.equal(forbiddenBrowserFields.test(catalogResult.text), false)

  const summary = []

  for (const mapId of ['map01', 'map02']) {
    const map = catalogResult.json.maps.find((item) => item.mapId === mapId)
    assert(map)
    const instanceMeta = map.layers.instances
    assert(instanceMeta.sectorKeys.length > 0)

    const sectorKey = instanceMeta.sectorKeys[0]
    const recordsResult = await getJson(
      `/api/v3/maps/${mapId}/layers/instances?sectorKey=${encodeURIComponent(sectorKey)}&limit=10`,
    )
    assert.equal(recordsResult.response.status, 200)
    assert.equal(recordsResult.json.filter.sampled, false)
    assert(recordsResult.json.returnedCount > 0)
    assert.equal(forbiddenBrowserFields.test(recordsResult.text), false)

    const etag = recordsResult.response.headers.get('etag')
    assert(etag)
    const cachedResult = await getJson(
      `/api/v3/maps/${mapId}/layers/instances?sectorKey=${encodeURIComponent(sectorKey)}&limit=10`,
      { headers: { 'If-None-Match': etag } },
    )
    assert.equal(cachedResult.response.status, 304)

    const roadSector = map.layers.roads.sectorKeys[0]
    const roadsResult = roadSector
      ? await getJson(`/api/v3/maps/${mapId}/layers/roads?sectorKey=${encodeURIComponent(roadSector)}&limit=10`)
      : await getJson(`/api/v3/maps/${mapId}/layers/roads?limit=10`)
    assert.equal(roadsResult.response.status, 200)
    assert(roadsResult.json.records.every((record) => record.category === 'road'))

    summary.push({
      mapId,
      instanceSectorCount: instanceMeta.sectorKeys.length,
      instanceReturnedCount: recordsResult.json.returnedCount,
      roadReturnedCount: roadsResult.json.returnedCount,
      etagStatus: cachedResult.response.status,
    })
  }

  for (const layer of ['water', 'effects']) {
    const result = await getJson(`/api/v3/maps/map02/layers/${layer}?limit=10`)
    assert.equal(result.response.status, 200)
    assert(['not_started', 'partial', 'complete'].includes(result.json.meta.scanStatus))
    assert.equal(forbiddenBrowserFields.test(result.text), false)
  }

  const map01Lights = await getJson('/api/v3/maps/map01/layers/lights?limit=10')
  assert.equal(map01Lights.response.status, 200)
  assert.equal(map01Lights.json.returnedCount, 0)
  assert(['not_started', 'partial', 'complete'].includes(map01Lights.json.meta.scanStatus))

  const map02Lights = await getJson('/api/v3/maps/map02/layers/lights?limit=10')
  assert.equal(map02Lights.response.status, 200)
  assert.equal(map02Lights.json.returnedCount, 0)
  assert(['not_started', 'partial', 'complete'].includes(map02Lights.json.meta.scanStatus))

  console.log(JSON.stringify({ status: 'passed', summary }))
} finally {
  await new Promise((resolve) => server.close(resolve))
}
