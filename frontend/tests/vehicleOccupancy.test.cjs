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
    ...vue, exports, Date, Set, Map, Object, Promise, JSON, Math, Number, String, Array,
    require: name => load(name, globals), ...globals,
  })
  return exports
}
const { parseVehicleFrame, readZones, matchVehicleToZone, clipPolygonToRect, createVehicleOccupancy, normalizeOccupancyMode } = load('~/lib/vehicleOccupancy')
const { polygonArea } = load('~/lib/videoGeometry')

const square = (x0, y0, x1, y1) => [{ x: x0, y: y0 }, { x: x1, y: y0 }, { x: x1, y: y1 }, { x: x0, y: y1 }]
const zone = (id, polygon, extra = {}) => ({ id, cameraId: 'cam', parkingSpaceId: 'S-' + id, polygon, ...extra })
// A car whose box spans x 0.1–0.3 and y 0.3–0.6 of a 1000×1000 frame: its footprint is x 0.12–0.28, y 0.525–0.6.
const car = (x = 100, confidence = 0.8, trackId = 1) => ({ track_id: trackId, class_id: 2, confidence, tracker_confidence: 0.9, bbox: { x, y: 300, width: 200, height: 300 } })
const frame = (timestamp, vehicles, extra = {}) => parseVehicleFrame('cam', {
  timestamp, bbox_width: 1000, bbox_height: 1000, vehicle_detection_enabled: true, vehicle_inference_done: true, vehicles, ...extra,
})

test('vehicle frames are normalized to the video frame and clipped', () => {
  const parsed = frame('t1', [car(), { track_id: null, confidence: null, bbox: { x: 900, y: 900, width: 300, height: 300 } }, { bbox: { x: 'bad' } }])
  assert.equal(parsed.vehicles.length, 2)
  const first = parsed.vehicles[0]
  assert.equal(first.trackId, '1')
  assert.equal(first.confidence, 0.8)
  for (const [key, value] of Object.entries({ x: 0.1, y: 0.3, width: 0.2, height: 0.3 })) assert.ok(Math.abs(first.bbox[key] - value) < 1e-9, key)
  assert.equal(parsed.vehicles[1].trackId, null)
  assert.ok(Math.abs(parsed.vehicles[1].bbox.width - 0.1) < 1e-9)
  const empty = parseVehicleFrame('cam', { timestamp: null, vehicles: [], vehicle_detection_enabled: false })
  assert.equal(empty.detectionEnabled, false)
  assert.equal(empty.inferenceDone, false)
  assert.equal(normalizeOccupancyMode('COLOR'), 'color')
  assert.equal(normalizeOccupancyMode('other'), 'detection')
})

test('polygon clipping handles concave zones', () => {
  // An L-shaped zone; the rectangle covers the notch, so only part of it intersects.
  const shape = [{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 2, y: 1 }, { x: 1, y: 1 }, { x: 1, y: 2 }, { x: 0, y: 2 }]
  assert.ok(Math.abs(polygonArea(clipPolygonToRect(shape, { x0: 0.5, x1: 1.5, y0: 0.5, y1: 1.5 })) - 0.75) < 1e-9)
  assert.equal(clipPolygonToRect(shape, { x0: 1.2, x1: 1.8, y0: 1.2, y1: 1.8 }).length, 0)
})

test('a car counts for the zone under its ground footprint, not the one its roof covers', () => {
  const vehicle = frame('t1', [car()]).vehicles[0]
  const ground = zone('ground', square(0.08, 0.5, 0.32, 0.65))
  const roof = zone('roof', square(0.08, 0.28, 0.32, 0.5))
  assert.equal(matchVehicleToZone(vehicle, ground.polygon).overlap, 1)
  assert.equal(matchVehicleToZone(vehicle, ground.polygon).anchored, true)
  assert.equal(matchVehicleToZone(vehicle, roof.polygon).overlap, 0)
  const readings = readZones(frame('t1', [car()]), [ground, roof])
  assert.equal(readings.get('ground').overlap, 1)
  assert.equal(readings.get('roof').overlap, 0)
})

test('a car between two spots fills at most one of them', () => {
  // Footprint x 0.12–0.28: 0.11 of its 0.16 width on the left spot, 0.05 on the right one.
  const left = zone('left', square(0, 0.5, 0.23, 0.7)), right = zone('right', square(0.23, 0.5, 0.5, 0.7))
  const readings = readZones(frame('t1', [car()]), [left, right])
  assert.ok(readings.get('left').overlap > 0.6)
  assert.equal(readings.get('right').overlap, 0)
})

test('status changes only after consecutive new frames and low-confidence vehicles hold it', () => {
  const occupancy = createVehicleOccupancy()
  const zones = [zone('a', square(0.08, 0.5, 0.32, 0.65), { revision: 1 })]
  const read = (timestamp, vehicles, extra) => occupancy.observe(frame(timestamp, vehicles, extra), zones)[0]
  assert.equal(read('t1', [car()]).status, 'unknown')
  assert.equal(read('t1', [car()]).status, 'unknown', 'the same frame twice is one observation')
  const occupied = read('t2', [car()])
  assert.equal(occupied.status, 'occupied')
  assert.equal(occupied.score, 1)
  assert.equal(occupied.updatedAt, 't2')
  assert.equal(read('t3', []).status, 'occupied')
  assert.equal(read('t4', []).status, 'occupied')
  assert.equal(read('t5', [car(100, 0.3)]).status, 'occupied', 'a low-confidence car resets the empty count')
  assert.equal(read('t6', [car(100, null)]).status, 'occupied', 'a tracker-only box is not proof of absence')
  assert.equal(read('t7', []).status, 'occupied')
  assert.equal(read('t8', []).status, 'occupied')
  assert.equal(read('t9', []).status, 'empty')
  assert.equal(read('t10', [car(100, 0.3)]).status, 'empty', 'a low-confidence car never makes a zone occupied')
  const off = read('t11', [car()], { vehicle_inference_done: false })
  assert.equal(off.status, 'unknown')
  assert.equal(off.score, null)
  // A redrawn polygon starts over.
  zones[0] = { ...zones[0], revision: 2 }
  assert.equal(read('t12', [car()]).status, 'unknown')
})

test('detection mode decides zone status in the store from polled vehicles', async () => {
  let currentNow = 10_000, vehicleTick
  const connection = vue.ref('open')
  const zoneList = [{ id: 'z1', cameraId: 'one', parkingSpaceId: 'A', polygon: square(0.08, 0.5, 0.32, 0.65), revision: 1 }]
  let timestamp = 0
  const api = {
    kind: 'http',
    listSpaces: async () => [{ id: 'A', label: 'A' }],
    listZones: async id => id === 'one' ? zoneList : [],
    getStatus: async () => ({ spaces: [{ spaceId: 'A', label: 'A', status: 'empty' }], zones: [{ zoneId: 'z1', cameraId: 'one', parkingSpaceId: 'A', status: 'empty', updatedAt: null, score: 0 }] }),
    getVehicles: async cameraId => parseVehicleFrame(cameraId, {
      timestamp: new Date(++timestamp * 2000).toISOString(), bbox_width: 1000, bbox_height: 1000,
      vehicle_detection_enabled: true, vehicle_inference_done: true, vehicles: [car()],
    }),
  }
  const { useParking } = load('~/composables/useParking', {
    Date: class extends Date { static now() { return currentNow } },
    setInterval: (fn, ms) => { if (ms === 2000) vehicleTick = fn; return 1 }, clearInterval() {},
    require: name => name.endsWith('/parkingApi') ? { useParkingApi: () => api, ParkingApiError: class extends Error {} } : load(name),
  })
  const mode = vue.ref('detection')
  let store
  const container = {}
  const renderer = vue.createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
  const flush = async () => { await new Promise(resolve => setImmediate(resolve)); await vue.nextTick() }
  renderer.render(vue.h({ setup() { store = useParking({ realtime: { connection, onMessage() { return () => {} } }, cameraIds: vue.ref(['one']), mode }); return () => null } }), container)
  try {
    await flush()
    // The color server says empty, but detections decide: one frame is not enough yet.
    assert.equal(store.zoneStatuses.value.z1, 'unknown')
    vehicleTick(); await flush()
    assert.equal(store.zoneStatuses.value.z1, 'occupied')
    assert.equal(store.totals.value.occupied, 1)
    // Detection mode doesn't need /ws.
    connection.value = 'reconnecting'; await flush()
    assert.equal(store.disconnected.value, false)
    assert.equal(store.zoneStatuses.value.z1, 'occupied')
    // Back to color: detections are dropped and the server status is used.
    connection.value = 'open'
    mode.value = 'color'; await flush(); await flush()
    assert.equal(store.zoneStatuses.value.z1, 'empty')
  } finally { renderer.render(null, container) }
})
