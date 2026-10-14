const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
function load(name, globals = {}) {
  const file = path.join(__dirname, '../app', name.replace(/^~\//, '') + '.ts')
  const exports = {}
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
    ...vue, exports, Date, Set, Map, Object, Promise,
    require: name => load(name, globals), ...globals,
  })
  return exports
}
const { filterParkingSpaces, unknownReason, isParkingReportFresh } = load('~/lib/parkingOperations')
const row = (id, status, zones = [], conflict = false) => ({ space: { id, label: id }, spaceIds: [id], status, zones, conflict })
test('filters compose with normalized label search and distinguish unused and missing baselines', () => {
  const rows = [row('A-P1', 'occupied', [{ cameraId: 'one' }], true), row('B-P1', 'empty', [{ cameraId: 'two' }]), row('Unused', 'unknown')]
  assert.equal(filterParkingSpaces(rows, ' a-p ', 'occupied', []).length, 1)
  assert.equal(filterParkingSpaces(rows, '', 'check', []).length, 1)
  assert.equal(filterParkingSpaces(rows, '', 'unused', [])[0].space.id, 'Unused')
  assert.equal(filterParkingSpaces(rows, '', 'uncalibrated', [{ parkingSpaceId: 'B-P1', calibrated: false }])[0].space.id, 'B-P1')
})
test('unknown reasons distinguish setup, connection, camera availability and report delay', () => {
  const assigned = row('A', 'unknown', [{ cameraId: 'one' }])
  assert.match(unknownReason(row('A', 'unknown'), true, true, new Set()), /No camera zone/)
  assert.match(unknownReason(assigned, true, false, new Set()), /disconnected/)
  assert.match(unknownReason(assigned, false, true, new Set(['one'])), /offline/)
  assert.match(unknownReason(assigned, false, true, new Set()), /fresh/)
  assert.equal(unknownReason(row('A', 'empty'), true, true, new Set()), null)
  assert.equal(isParkingReportFresh(1000, 16000), true)
  assert.equal(isParkingReportFresh(1000, 16001), false)
})

const renderer = vue.createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); await vue.nextTick() }
test('parking store invalidates cached readings on disconnect and expires individual stale zones', async () => {
  let currentNow = 1000, listener, clock
  const connection = vue.ref('open')
  const report = () => ({ spaces: [{ spaceId: 'A', label: 'A', status: 'empty' }], zones: [
    { zoneId: 'z1', cameraId: 'one', parkingSpaceId: 'A', status: 'empty', updatedAt: new Date(currentNow).toISOString(), score: .1 },
    { zoneId: 'z2', cameraId: 'two', parkingSpaceId: 'A', status: 'empty', updatedAt: new Date(currentNow).toISOString(), score: .1 },
  ] })
  const api = { kind: 'http', listSpaces: async () => [{ id: 'A', label: 'A' }], listZones: async id => [{ id: id === 'one' ? 'z1' : 'z2', cameraId: id, parkingSpaceId: 'A' }], getStatus: async () => report() }
  const { useParking } = load('~/composables/useParking', {
    Date: class extends Date { static now() { return currentNow } },
    setInterval: (fn, ms) => { if (ms === 1000) clock = fn; return 1 }, clearInterval() {},
    require: name => name.endsWith('/parkingApi') ? { useParkingApi: () => api, ParkingApiError: class extends Error {} } : load(name),
  })
  let store
  const container = {}
  renderer.render(vue.h({ setup() { store = useParking({ realtime: { connection, onMessage(fn) { listener = fn; return () => {} } }, cameraIds: vue.ref(['one', 'two']) }); return () => null } }), container)
  try {
    await flush()
    assert.equal(store.totals.value.empty, 1, JSON.stringify({ totals: store.totals.value, states: store.zoneStatuses.value, delayed: store.delayed.value, time: store.lastReportAt.value, summaries: store.summaries.value }))
    connection.value = 'reconnecting'; await flush()
    assert.equal(store.totals.value.unknown, 1)
    connection.value = 'open'; await flush()
    assert.equal(store.totals.value.empty, 1)
    currentNow += 16000; clock(); await flush()
    assert.equal(store.totals.value.unknown, 1)
    listener({ type: 'parking.status_updated', cameraId: 'one', data: { spaces: [{ parkingSpaceId: 'A', label: 'A', status: 'EMPTY', zones: [{ zoneId: 'z1', cameraId: 'one', status: 'EMPTY', score: .1, updatedAt: new Date(currentNow).toISOString() }] }] } })
    await flush()
    assert.equal(store.zoneStatuses.value.z1, 'empty')
    assert.equal(store.zoneStatuses.value.z2, 'unknown')
    assert.equal(store.totals.value.unknown, 1)
  } finally { renderer.render(null, container) }
})
