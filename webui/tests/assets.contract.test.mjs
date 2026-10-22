import assert from 'node:assert/strict'
import path from 'node:path'
import { registerHooks } from 'node:module'

registerHooks({
  resolve(specifier, context, nextResolve) {
    return nextResolve(specifier.startsWith('.') && path.extname(specifier) === '' ? `${specifier}.ts` : specifier, context)
  },
})

const [originArgument, jobId] = process.argv.slice(2)
assert.ok(originArgument, 'Pass the running WebUI origin as the first argument.')
const origin = new URL(originArgument)
assert.equal(origin.protocol, 'http:')
assert.equal(origin.hostname, '127.0.0.1')
assert.equal(origin.pathname, '/')
const transport = globalThis.fetch
globalThis.fetch = (input, init) => transport(typeof input === 'string' ? new URL(input, origin) : input, init)
const api = await import('../src/features/assets/api.ts')
const controller = new AbortController()
const counts = {}
try {
  const wrongMethod = await transport(new URL('/api/v2/asset-catalogs/weapon', origin), { method: 'POST', headers: { Origin: origin.origin } })
  assert.equal(wrongMethod.status, 405)
  const unknownCatalog = await transport(new URL('/api/v2/asset-catalogs/invalid', origin))
  assert.equal(unknownCatalog.status, 404)
  for (const kind of ['weapon', 'illustration', 'display_prop']) {
    const catalog = await api.getAssetCatalog(kind, controller.signal)
    counts[kind] = catalog.entries.length
    assert.ok(catalog.entries.length > 0)
    assert.equal(catalog.kind, kind)
    assert.equal(Object.hasOwn(catalog, 'token'), false)
    let selection
    if (kind === 'weapon') {
      const entry = catalog.entries.find(row => row.model_status === 'resolved' && row.variants.some(variant => variant.variant === 'base'))
      assert.ok(entry)
      const selected = entry.variants.find(variant => variant.variant === 'base')
      selection = { kind, catalog_id: catalog.catalog_id, item_ids: [selected.item_id], variant: 'base' }
    } else {
      const entry = catalog.entries.find(row => row.provider_status === 'ready')
      assert.ok(entry)
      selection = { kind, catalog_id: catalog.catalog_id, item_ids: [kind === 'illustration' ? entry.illustration_id : entry.prop_id] }
    }
    const request = api.createAssetExportRequest(selection)
    assert.match(request.request_id, /^[0-9a-f-]{36}$/)
    assert.equal(Object.hasOwn(request, 'variant'), kind === 'weapon')
    const validation = await api.validateAssetRequest(request, controller.signal)
    assert.deepEqual(validation.canonical_request, request)
    assert.throws(() => api.parseAssetExportRequest({ ...request, item_ids: [...request.item_ids, ...request.item_ids] }), /distinct/)
    if (kind !== 'weapon') assert.throws(() => api.parseAssetExportRequest({ ...request, variant: 'base' }), /no variant/)
  }
  if (jobId) {
    const job = await api.getAssetJob(jobId, controller.signal)
    assert.equal(job.job_id, jobId)
    assert.equal(job.request.request_id, jobId)
    assert.throws(() => api.parseAssetJob({ ...job, progress: 1.1 }), /fraction/)
  }
  console.log(JSON.stringify({ status: 'passed', catalogs: counts, validated: Object.keys(counts), job: jobId ?? null }))
} finally {
  globalThis.fetch = transport
  controller.abort()
}
