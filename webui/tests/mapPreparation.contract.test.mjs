import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { registerHooks } from 'node:module'
import path from 'node:path'

registerHooks({ resolve(specifier, context, nextResolve) {
  return nextResolve(specifier.startsWith('.') && !path.extname(specifier) ? `${specifier}.ts` : specifier, context)
} })
const { isMapPreparationCatalog, isMapPreparationJob } = await import('../src/features/si-painting/useMapPreparation.ts')
assert.ok(process.argv[2], 'Provide the result.json from a real map preparation run.')
const result = JSON.parse(await readFile(process.argv[2], 'utf8'))
assert.ok(isMapPreparationCatalog(result.catalog))
for (const key of ['job', 'cancelled', 'reuse']) assert.ok(isMapPreparationJob(result[key]), key)
assert.equal(result.job.result.reused, false)
assert.equal(result.cancelled.state, 'cancelled')
assert.equal(result.reuse.result.reused, true)
for (const change of [
  job => { job.job_id = '../runtime' }, job => { job.level_id = 'invented' },
  job => { job.catalog_id = 'bad' }, job => { job.progress = Infinity }, job => { job.progress = NaN },
  job => { job.progress = -1 }, job => { job.cancel_requested = 'yes' },
  job => { job.result.level_id = 'map99_lv999' }, job => { job.result = null },
]) {
  const job = structuredClone(result.job); change(job)
  assert.equal(isMapPreparationJob(job), false)
}
for (const change of [
  catalog => { catalog.entries[0].artOwner = 'map99' },
  catalog => { catalog.entries[0].tileCount = 1.5 },
  catalog => { catalog.entries.push(structuredClone(catalog.entries[0])) },
  catalog => { catalog.preparedLevelIds = ['map99_lv999'] },
  catalog => { catalog.activeJob = { ...result.job, catalog_id: 'a'.repeat(64) } },
]) {
  const catalog = structuredClone(result.catalog); change(catalog)
  assert.equal(isMapPreparationCatalog(catalog), false)
}
console.log(`Real map preparation contracts passed: ${result.catalog.entries.length} discovered levels, completed/cancelled/reused tasks.`)
