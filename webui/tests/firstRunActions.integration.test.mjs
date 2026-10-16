import assert from 'node:assert/strict'
import { canStartFirstRunRequest, firstRunRequestMatchesPrepared } from '../src/lib/firstRunActions.ts'

assert.ok(process.argv[2], 'Provide a real ready WebUI origin.')
const response = await fetch(`${process.argv[2]}/api/v2/first-run`)
assert.equal(response.status, 200)
const status = await response.json()
assert.equal(status.ready, true, 'The integration check requires an actually restored ready source.')
assert.ok(status.request)
const request = structuredClone(status.request)
assert.equal(firstRunRequestMatchesPrepared(request, status.request), true)
assert.equal(canStartFirstRunRequest(status, request), false, 'Same ready request stays disabled.')
for (const field of ['game_root', 'export_root', 'cache_root', 'blender_exe']) {
  const changed = { ...request, [field]: `${request[field] ?? ''}-different-selection` }
  assert.equal(firstRunRequestMatchesPrepared(changed, status.request), false, field)
  assert.equal(canStartFirstRunRequest(status, changed), true, `${field}: a different validated request may prepare.`)
  for (const state of ['queued', 'running']) {
    assert.equal(canStartFirstRunRequest({ ...status, state }, changed), false, `${field}: running preparation blocks another start.`)
  }
}
assert.equal(canStartFirstRunRequest(null, request), false)
assert.equal(canStartFirstRunRequest({ ...status, ready: false, state: 'partial', actions: { ...status.actions, can_start: true } }, request), true)
console.log('Live restored-source action checks passed: same request, changed game/output/cache/Blender and active tasks.')
