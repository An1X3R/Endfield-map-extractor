import assert from 'node:assert/strict'
import { readFileSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

import { stage119LayerApi } from '../server/stage119LayerApi.ts'

const baseRoot = process.env.ENDFIELD_STAGE_BASE_ROOT
const retryRoot = process.env.ENDFIELD_STAGE_RETRY_ROOT
assert(baseRoot, 'ENDFIELD_STAGE_BASE_ROOT is required')
assert(retryRoot, 'ENDFIELD_STAGE_RETRY_ROOT is required')

const pointerPath = process.env.ENDFIELD_STAGE_POINTER_FILE
  || join(tmpdir(), `endfield-stage-pointer-contract-${process.pid}.json`)
process.env.ENDFIELD_STAGE_POINTER_FILE = pointerPath

const writePointer = (stageRoot) => writeFileSync(pointerPath, JSON.stringify({
  format: 'EndfieldStagePointer/1',
  stage_root: stageRoot,
  runtime_config: null,
  updated_at: Date.now() / 1000,
}) + '\n', 'utf8')

writePointer(baseRoot)
const plugin = stage119LayerApi(process.cwd())
let middleware
plugin.configureServer({ middlewares: { use(handler) { middleware = handler } } })
assert.equal(typeof middleware, 'function')

const server = createServer((request, response) => middleware(request, response, () => {
  response.statusCode = 404
  response.end('not_found')
}))
await new Promise((resolve, reject) => {
  server.once('error', reject)
  server.listen(0, '127.0.0.1', resolve)
})

const address = server.address()
assert(address && typeof address === 'object')
const catalog = async () => {
  const response = await fetch(`http://127.0.0.1:${address.port}/api/v3/layer-catalog`)
  assert.equal(response.status, 200)
  return response.json()
}

try {
  const before = await catalog()
  writePointer(retryRoot)
  const after = await catalog()
  const retryManifest = JSON.parse(readFileSync(join(retryRoot, 'region_map_layer_manifest.json'), 'utf8'))
  assert.notEqual(before.dataset.datasetFingerprint, after.dataset.datasetFingerprint)
  assert.equal(after.dataset.datasetFingerprint, retryManifest.datasetFingerprint)
  console.log(JSON.stringify({
    status: 'passed',
    beforeFingerprint: before.dataset.datasetFingerprint,
    afterFingerprint: after.dataset.datasetFingerprint,
    retryRoot,
  }))
} finally {
  server.close()
}
