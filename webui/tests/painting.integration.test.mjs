import assert from 'node:assert/strict'
import path from 'node:path'
import { registerHooks } from 'node:module'
import { createHash } from 'node:crypto'

registerHooks({ resolve(specifier, context, nextResolve) {
  return nextResolve(specifier.startsWith('.') && !path.extname(specifier) ? `${specifier}.ts` : specifier, context)
} })
const { isLevelMapRegistry, selectionBounds, snapPaintingSelection } = await import('../src/features/si-painting/levelMaps.ts')
const { paintingRequest } = await import('../src/features/si-painting/usePaintingRuntime.ts')
const { isExportJob } = await import('../src/lib/mapExportJob.ts')
const base = process.env.ENDFIELD_TEST_ORIGIN
assert.ok(base, 'Set ENDFIELD_TEST_ORIGIN to a real running WebUI with prepared level maps.')
const response = await fetch(`${base}/api/v2/level-maps`)
assert.equal(response.status, 200, await response.clone().text())
const registry = await response.json()
assert.ok(isLevelMapRegistry(registry), 'Live level map registry must pass the runtime contract.')
const map = registry.entries.find(entry => entry.levelId === 'map02_lv010')
assert.ok(map, 'The live registry must contain the requested painting level.')
const grid = map.selectionGrid
for (let row = 0; row < grid.rows; row++) {
  for (let column = 0; column < grid.columns; column++) {
    const selection = { u: (column + .25) / grid.columns, v: (grid.rows - row - .75) / grid.rows, width: .5 / grid.columns, height: .5 / grid.rows }
    const expected = {
      xmin: grid.origin.x + column * grid.cellSizeMetres, xmax: grid.origin.x + (column + 1) * grid.cellSizeMetres,
      zmin: grid.origin.z + row * grid.cellSizeMetres, zmax: grid.origin.z + (row + 1) * grid.cellSizeMetres,
    }
    assert.deepEqual(selectionBounds(map, selection), expected)
    assert.deepEqual(selectionBounds(map, snapPaintingSelection(map, selection)), expected)
  }
}
assert.deepEqual(selectionBounds(map, { u: 2 / 15, v: 3 / 10, width: 1 / 15, height: 1 / 10 }), { xmin: 128, xmax: 256, zmin: 1152, zmax: 1280 })
assert.deepEqual(selectionBounds(map, { u: 0, v: 0, width: 1, height: 1 }), map.worldBounds)
assert.throws(() => selectionBounds(map, { u: 0, v: 0, width: 0, height: 1 }), RangeError)
const malformed = structuredClone(registry)
malformed.entries[0].pixelToWorld[1][1] *= -1
assert.equal(isLevelMapRegistry(malformed), false)
for (const mutation of [
  entry => { entry.artOwner = 'map01'; entry.selectionGrid.artOwner = 'map01' },
  entry => { entry.mapId = 'map01'; entry.selectionGrid.mapId = 'map01' },
  entry => { entry.image.url = '/characters/other/image' },
  entry => { entry.worldBounds.xmax = Infinity },
  entry => { entry.selectionGrid.columns = Number.MAX_SAFE_INTEGER + 1 },
  entry => { entry.pixelToWorld[0][0] += 1; entry.pixelToWorld[0][1] -= entry.image.pixelWidth / entry.image.pixelHeight },
]) {
  const invalid = structuredClone(registry)
  mutation(invalid.entries[0])
  assert.equal(isLevelMapRegistry(invalid), false)
}
for (const selection of [
  { u: NaN, v: 0, width: 1, height: 1 },
  { u: 0, v: 0, width: Infinity, height: 1 },
  { u: -0.1, v: 0, width: 1, height: 1 },
  { u: 1, v: 0, width: 0.0000001, height: 1 },
]) assert.throws(() => selectionBounds(map, selection), RangeError)
const job = { job_id: 'job-001', state: 'extracting', phase: 'extract', progress: 0.5 }
assert.ok(isExportJob(job))
for (const invalid of [{ ...job, progress: NaN }, { ...job, progress: Infinity }, { ...job, progress: 1.1 }, { ...job, job_id: '../other' }]) assert.equal(isExportJob(invalid), false)
const settings = { layers: { terrain: true, instances: true, water: false, effects: false }, groups: ['terrain'], chunkMode: 'per_sector', exportMode: 'data_package', outputName: 'coordinate_contract' }
const request = paintingRequest(map, settings, { u: 2 / 15, v: 3 / 10, width: 1 / 15, height: 1 / 10 }, { game: 'user-selected-game', output: 'user-selected-output', cache: 'user-selected-cache', blender: '' })
assert.equal(request.map_id, map.mapId)
assert.deepEqual(request.selection, { type: 'world_bounds', xmin: 128, xmax: 256, zmin: 1152, zmax: 1280 })
assert.equal(request.batch_mode, 'per_sector')
assert.deepEqual(request.export_groups, ['terrain'])
const image = await fetch(`${base}/api/v2${map.image.url}`)
assert.equal(image.status, 200)
assert.match(image.headers.get('content-type'), /^image\/png/)
const bytes = Buffer.from(await image.arrayBuffer())
assert.equal(bytes.length, map.image.bytes)
assert.equal(createHash('sha256').update(bytes).digest('hex'), map.image.sha256)
console.log(`Live painting integration passed: ${grid.columns * grid.rows} cells, full image SHA256, world-coordinate request mapping. No export job was submitted.`)
