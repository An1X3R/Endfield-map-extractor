import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { mapOverviewApi } from '../server/mapOverviewApi.ts'

const testTempRoot = process.env.ENDFIELD_WEBUI_TEST_TEMP_ROOT || tmpdir()
mkdirSync(testTempRoot, { recursive: true })
const fixtureRoot = mkdtempSync(path.join(testTempRoot, 'endfield-map-overview-contract-'))
const pointerPath = path.join(fixtureRoot, 'stage_pointer.json')
const legacyCachePath = path.join(fixtureRoot, 'legacy-cache', 'maps', 'stale', 'map01', 'clean.png')
const previousPointerPath = process.env.ENDFIELD_STAGE_POINTER_FILE
const previousCacheRoot = process.env.ENDFIELD_MAP_CACHE_ROOT
const previousOverridesPath = process.env.ENDFIELD_MAP_OVERRIDES_FILE

const writeJson = (filePath, payload) => {
  mkdirSync(path.dirname(filePath), { recursive: true })
  writeFileSync(filePath, JSON.stringify(payload), 'utf8')
}

const pngFixture = (label) => Buffer.concat([
  Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
  Buffer.from(label, 'utf8'),
])

const createRuntime = (runtimeName, label) => {
  const runtimeRoot = path.join(fixtureRoot, runtimeName)
  const overviewRoot = path.join(runtimeRoot, 'map_overviews')
  const maps = {}
  for (const mapId of ['map01', 'map02']) {
    const mapRoot = path.join(overviewRoot, mapId)
    mkdirSync(mapRoot, { recursive: true })
    const clean = `${mapId}/clean.png`
    const sectors = `${mapId}/sectors.png`
    writeFileSync(path.join(overviewRoot, clean), pngFixture(`${label}-${mapId}-clean`))
    writeFileSync(path.join(overviewRoot, sectors), pngFixture(`${label}-${mapId}-sectors`))
    maps[mapId] = { clean, sectors }
  }
  const manifestPath = path.join(overviewRoot, 'overview_manifest.json')
  writeJson(manifestPath, { format: 'EndfieldMapOverviews/1', status: 'ready', maps })
  const runtimePath = path.join(runtimeRoot, 'runtime.json')
  writeJson(runtimePath, {
    format: 'EndfieldRuntimeConfig/1',
    extraction: { mapOverviews: { path: 'map_overviews/overview_manifest.json' } },
  })
  return { runtimePath, manifestPath, overviewRoot, maps }
}

const writePointer = (runtimeConfig) => writeJson(pointerPath, {
  format: 'EndfieldStagePointer/1',
  stage_root: fixtureRoot,
  runtime_config: runtimeConfig,
})

process.env.ENDFIELD_STAGE_POINTER_FILE = pointerPath
process.env.ENDFIELD_MAP_CACHE_ROOT = path.dirname(path.dirname(path.dirname(legacyCachePath)))
delete process.env.ENDFIELD_MAP_OVERRIDES_FILE
mkdirSync(path.dirname(legacyCachePath), { recursive: true })
writeFileSync(legacyCachePath, pngFixture('stale-cache'))
writePointer(null)

const plugin = mapOverviewApi()
let middleware
plugin.configureServer({ middlewares: { use(handler) { middleware = handler } } })
assert.equal(typeof middleware, 'function')

const server = createServer((request, response) => middleware(request, response, () => {
  response.statusCode = 404
  response.end('not_found')
}))

try {
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  assert(address && typeof address === 'object')
  const baseUrl = `http://127.0.0.1:${address.port}`

  const missingResponse = await fetch(`${baseUrl}/api/v2/maps/map01/overview?variant=clean`)
  assert.equal(missingResponse.status, 404)
  assert.equal((await missingResponse.json()).reason, 'no_active_overview')

  const devOverridePath = path.join(fixtureRoot, 'dev-override.png')
  const devOverridesPath = path.join(fixtureRoot, 'dev-overrides.json')
  writeFileSync(devOverridePath, pngFixture('explicit-dev-override'))
  writeJson(devOverridesPath, {
    enabled: true,
    serverOnly: true,
    maps: { map01: { clean: devOverridePath } },
  })
  process.env.ENDFIELD_MAP_OVERRIDES_FILE = devOverridesPath
  const devOverrideResponse = await fetch(`${baseUrl}/api/v2/maps/map01/overview?variant=clean`)
  assert.equal(devOverrideResponse.status, 200)
  assert.equal(devOverrideResponse.headers.get('x-endfield-map-source'), 'dev-override')
  delete process.env.ENDFIELD_MAP_OVERRIDES_FILE

  const runtimeOne = createRuntime('runtime-one', 'first')
  writePointer(path.relative(fixtureRoot, runtimeOne.runtimePath))
  let firstRuntimeEtag = ''
  for (const mapId of ['map01', 'map02']) {
    const response = await fetch(`${baseUrl}/api/v2/maps/${mapId}/overview?variant=clean`, { cache: 'no-store' })
    assert.equal(response.status, 200)
    assert.equal(response.headers.get('x-endfield-map-source'), 'runtime-config')
    if (mapId === 'map01') firstRuntimeEtag = response.headers.get('etag') || ''
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), readFileSync(path.join(runtimeOne.overviewRoot, runtimeOne.maps[mapId].clean)))
  }
  assert(firstRuntimeEtag)
  const unchangedResponse = await fetch(`${baseUrl}/api/v2/maps/map01/overview?variant=clean`, {
    cache: 'no-store',
    headers: { 'If-None-Match': firstRuntimeEtag },
  })
  assert.equal(unchangedResponse.status, 304)

  const mapListResponse = await fetch(`${baseUrl}/api/v2/maps`)
  assert.equal(mapListResponse.status, 200)
  const mapList = await mapListResponse.json()
  assert.equal(mapList.format, 'EndfieldMapCatalog/1')
  assert.equal(mapList.maps.length, 2)
  assert.equal(mapList.maps.every((map) => map.overview.state === 'ready'), true)

  const manifest = JSON.parse(readFileSync(runtimeOne.manifestPath, 'utf8'))
  manifest.maps.map02.clean = '../outside.png'
  writeJson(runtimeOne.manifestPath, manifest)
  const traversalResponse = await fetch(`${baseUrl}/api/v2/maps/map01/overview?variant=clean`)
  assert.equal(traversalResponse.status, 404)
  assert.equal((await traversalResponse.json()).reason, 'unsafe_overview_path')
  manifest.maps.map02.clean = runtimeOne.maps.map02.clean
  writeJson(runtimeOne.manifestPath, manifest)

  const runtimeTwo = createRuntime('runtime-two', 'second')
  writePointer(path.relative(fixtureRoot, runtimeTwo.runtimePath))
  const switchedResponse = await fetch(`${baseUrl}/api/v2/maps/map01/overview?variant=clean`, {
    cache: 'no-store',
    headers: { 'If-None-Match': firstRuntimeEtag },
  })
  assert.equal(switchedResponse.status, 200)
  assert.notEqual(switchedResponse.headers.get('etag'), firstRuntimeEtag)
  const switchedImage = Buffer.from(await switchedResponse.arrayBuffer())
  assert.deepEqual(switchedImage, readFileSync(path.join(runtimeTwo.overviewRoot, runtimeTwo.maps.map01.clean)))
  assert.notDeepEqual(switchedImage, readFileSync(path.join(runtimeOne.overviewRoot, runtimeOne.maps.map01.clean)))

  console.log(JSON.stringify({ status: 'passed', checks: ['missing-does-not-use-old-cache', 'explicit-dev-override', 'pointer-activation', 'both-maps', 'etag-304', 'traversal-rejected', 'runtime-switch-refreshes-image'] }))
} finally {
  await new Promise((resolve) => server.close(resolve))
  if (previousPointerPath === undefined) delete process.env.ENDFIELD_STAGE_POINTER_FILE
  else process.env.ENDFIELD_STAGE_POINTER_FILE = previousPointerPath
  if (previousCacheRoot === undefined) delete process.env.ENDFIELD_MAP_CACHE_ROOT
  else process.env.ENDFIELD_MAP_CACHE_ROOT = previousCacheRoot
  if (previousOverridesPath === undefined) delete process.env.ENDFIELD_MAP_OVERRIDES_FILE
  else process.env.ENDFIELD_MAP_OVERRIDES_FILE = previousOverridesPath
  const resolvedFixtureRoot = path.resolve(fixtureRoot)
  const resolvedTempRoot = path.resolve(testTempRoot)
  const relativeFixturePath = path.relative(resolvedTempRoot, resolvedFixtureRoot)
  assert(existsSync(resolvedFixtureRoot))
  assert(relativeFixturePath && !relativeFixturePath.startsWith('..') && !path.isAbsolute(relativeFixturePath))
  rmSync(resolvedFixtureRoot, { recursive: true, force: true })
}
