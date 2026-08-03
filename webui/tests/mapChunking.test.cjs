const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const modulePath = process.argv[2]
const manifestPath = process.argv[3]
if (!modulePath || !manifestPath) throw new Error('usage: test compiled-module manifest')
const chunking = require(path.resolve(modulePath))
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'))

for (const mapId of ['map01', 'map02']) {
  const map = manifest.maps[mapId]
  const drag = chunking.screenDragToWorldBounds(
    map,
    { x: 0, y: 0 },
    { x: 1000, y: 1000 },
    { x: 0, y: 0, width: 1000, height: 1000 },
  )
  assert.deepEqual(drag, map.worldBounds, `${mapId} must use +Z up`)
}

const formal = chunking.resolveSelection(
  manifest,
  'map01',
  { xmin: -256, xmax: 0, zmin: -512, zmax: -256 },
  'per_sector',
)
assert.equal(formal.sectors.length, 4)
assert.deepEqual(
  formal.sectors.map((item) => [item.sectorX, item.sectorZ, item.hTile.levelId, item.hTile.tileX, item.hTile.tileY]),
  [
    [-2, -4, 'map01_lv001', 6, 3],
    [-1, -4, 'map01_lv001', 7, 3],
    [-2, -3, 'map01_lv001', 6, 4],
    [-1, -3, 'map01_lv001', 7, 4],
  ],
)
assert.deepEqual(formal.sectors[0].blenderBoundsXY, {
  xmin: -256, xmax: -128, ymin: 384, ymax: 512,
})

const central = chunking.resolveSelection(
  manifest,
  'map01',
  { xmin: -512, xmax: 512, zmin: -512, zmax: 512 },
  'merged_selection',
)
assert.equal(central.sectors.length, 64)
assert.equal(central.sectors.filter((item) => item.coverage === 'mapped').length, 54)
assert.equal(central.batches.length, 1)

const fullMap01 = chunking.resolveSelection(manifest, 'map01', manifest.maps.map01.worldBounds)
const fullMap02 = chunking.resolveSelection(manifest, 'map02', manifest.maps.map02.worldBounds)
assert.equal(fullMap01.sectors.length, 256)
assert.equal(fullMap01.sectors.filter((item) => item.coverage === 'mapped').length, 139)
assert.equal(fullMap02.sectors.length, 1024)
assert.equal(fullMap02.sectors.filter((item) => item.coverage === 'mapped').length, 262)

const clusters = chunking.resolveSelection(
  manifest,
  'map01',
  { xmin: -256, xmax: 256, zmin: -256, zmax: 256 },
  'cluster_4x4_sectors',
)
assert.equal(clusters.batches.length, 4)
assert.deepEqual(clusters.batches.map((item) => item.id), [
  'cluster4:-1:-1', 'cluster4:-1:0', 'cluster4:0:-1', 'cluster4:0:0',
])

const pixels = chunking.sectorToImageBounds(manifest.maps.map01, -2, -4, 2400, 2400)
assert.deepEqual(pixels, { xmin: 900, xmax: 1050, ymin: 1650, ymax: 1800 })

console.log(JSON.stringify({ tests: 16, status: 'passed' }))
