const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

const appDir = path.join(__dirname, '../app')
const cache = new Map()

// Transpiles app modules to CommonJS and resolves their `~/` imports the same way.
function load(specifier) {
  const file = path.join(appDir, specifier.replace(/^~\//, '') + '.ts')
  if (cache.has(file)) return cache.get(file)
  const source = fs.readFileSync(file, 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  cache.set(file, exports)
  vm.runInNewContext(compiled, {
    exports, require: load, JSON, Math, Number, Date, Error, Set, Map, Promise, Object, Array, String,
    queueMicrotask, setTimeout, globalThis: {},
  })
  return exports
}

const geometry = load('~/lib/videoGeometry')
const parking = load('~/lib/parking')
const api = load('~/composables/parkingApi')

const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} ≈ ${expected}`)
const plain = value => JSON.parse(JSON.stringify(value))
const square = [{ x: 0.1, y: 0.1 }, { x: 0.3, y: 0.1 }, { x: 0.3, y: 0.3 }, { x: 0.1, y: 0.3 }]

test('contentRect follows object-fit letterboxing', () => {
  const media = { width: 1920, height: 1080 }
  // Taller box than 16:9 → bars top and bottom.
  assert.deepEqual(plain(geometry.contentRect({ width: 800, height: 600 }, media)), { left: 0, top: 75, width: 800, height: 450 })
  // Wider box (e.g. ultrawide fullscreen) → bars left and right.
  assert.deepEqual(plain(geometry.contentRect({ width: 2560, height: 1080 }, media)), { left: 320, top: 0, width: 1920, height: 1080 })
  const cover = geometry.contentRect({ width: 800, height: 600 }, media, 'cover')
  close(cover.left, -400 / 3)
  close(cover.width, 3200 / 3)
  assert.equal(cover.height, 600)
  assert.deepEqual(plain(geometry.contentRect({ width: 800, height: 600 }, media, 'fill')), { left: 0, top: 0, width: 800, height: 600 })
  assert.deepEqual(plain(geometry.contentRect({ width: 4000, height: 3000 }, media, 'scale-down')), { left: 1040, top: 960, width: 1920, height: 1080 })
  assert.deepEqual(plain(geometry.contentRect({ width: 0, height: 0 }, media)), { left: 0, top: 0, width: 0, height: 0 })
})

test('client and normalized coordinates round-trip through the letterboxed content', () => {
  const box = { width: 1000, height: 1000 }
  const rect = geometry.contentRect(box, { width: 1280, height: 720 })
  // Stage placed at (40, 60) in the viewport.
  const onScreen = { left: 40 + rect.left, top: 60 + rect.top, width: rect.width, height: rect.height }
  const original = { x: 0.25, y: 0.8 }
  const pixel = geometry.normalizedToPixels(original, rect)
  const back = geometry.clientToNormalized(onScreen.left + pixel.x, onScreen.top + pixel.y, onScreen)
  close(back.x, original.x)
  close(back.y, original.y)
  // Clicks in the letterbox bars clamp to the frame edge.
  assert.deepEqual(plain(geometry.clientToNormalized(0, 0, onScreen)), { x: 0, y: 0 })
  assert.deepEqual(plain(geometry.clientToNormalized(5000, 5000, onScreen)), { x: 1, y: 1 })
  const raw = geometry.clientToNormalized(onScreen.left - onScreen.width, onScreen.top, onScreen, false)
  close(raw.x, -1)
})

test('polygon helpers detect crossings and compute centroid and area', () => {
  close(geometry.polygonArea(square), 0.04)
  const centroid = geometry.polygonCentroid(square)
  close(centroid.x, 0.2)
  close(centroid.y, 0.2)
  assert.equal(geometry.isSelfIntersecting(square), false)
  assert.equal(geometry.isSelfIntersecting([square[0], square[2], square[1], square[3]]), true)
  assert.equal(geometry.isSelfIntersecting(square.slice(0, 3)), false)
})


const zone = (id, parkingSpaceId, polygon = square, cameraId = 'cam') => ({ id, cameraId, parkingSpaceId, polygon })

test('validateZones needs a label, a known space, one zone per space per camera, and a valid polygon', () => {
  const bowtie = [square[0], square[2], square[1], square[3]]
  const tiny = [{ x: 0.5, y: 0.5 }, { x: 0.501, y: 0.5 }, { x: 0.5, y: 0.501 }]
  const problems = parking.validateZones([
    zone('ok', 's1'),
    zone('dup', 's1'),
    zone('blank', ''),
    zone('gone', 'deleted'),
    zone('two', 's2', square.slice(0, 2)),
    zone('cross', 's3', bowtie),
    zone('tiny', 's4', tiny),
    zone('out', 's5', [{ x: -0.1, y: 0 }, ...square.slice(1)]),
  ], new Set(['s1', 's2', 's3', 's4', 's5']))
  assert.deepEqual(plain(problems).map(problem => problem.zoneId), ['ok', 'dup', 'blank', 'gone', 'two', 'cross', 'tiny', 'out'])
  assert.equal(problems[0].message, 'Label used twice on this camera')
  assert.equal(problems[2].message, 'Choose a label')
  assert.equal(problems[3].message, 'Label no longer exists')
  assert.deepEqual(plain(parking.validateZones([zone('ok', 's1')])), [])
})

test('labels match ignoring case and spacing, and sort naturally', () => {
  const spaces = [{ id: '1', label: 'A-10' }, { id: '2', label: 'A-2' }]
  assert.equal(parking.findSpaceByLabel(spaces, '  a-2 ').id, '2')
  assert.equal(parking.findSpaceByLabel(spaces, 'A-3'), null)
  assert.equal(parking.findSpaceByLabel(spaces, '   '), null)
  assert.deepEqual(['A-10', 'A-2', 'B-1'].sort(parking.compareLabels), ['A-2', 'A-10', 'B-1'])
  assert.equal(parking.validateLabel(' '), 'Enter a label')
  assert.equal(parking.validateLabel('x'.repeat(41)), 'Use at most 40 characters')
  assert.equal(parking.validateLabel('A-01'), null)
  assert.match(parking.createZoneId(), /^pz-/)
  assert.equal(parking.isClientZoneId(parking.createZoneId()), true)
})

test('counts treat missing statuses as unknown; status text is case-insensitive', () => {
  assert.deepEqual(plain(parking.countStatuses(['a', 'b', 'c', 'd'], { a: 'occupied', b: 'empty' })), { total: 4, occupied: 1, empty: 1, unknown: 2 })
  assert.equal(parking.normalizeStatus('OCCUPIED'), 'occupied')
  assert.equal(parking.normalizeStatus('Empty'), 'empty')
  assert.equal(parking.normalizeStatus('parked?'), 'unknown')
})

test('isParkingCamera matches 주차장 in the display name only', () => {
  assert.equal(parking.isParkingCamera('주차장 전체'), true)
  assert.equal(parking.isParkingCamera('1층 야외주차장'), true)
  assert.equal(parking.isParkingCamera('주차타워 안'), false)
  assert.equal(parking.isParkingCamera('b1_parking_overview'), false)
  assert.equal(parking.isParkingCamera(undefined), false)
})

const state = (zoneId, cameraId, status, score = null, parkingSpaceId = 'A') => ({ zoneId, cameraId, parkingSpaceId, status, score, updatedAt: null })

test('combined space status ignores unknown views and flags disagreement', () => {
  // Camera 1 occupied, camera 2 cannot see: occupied, no conflict.
  assert.deepEqual(plain(parking.combineSpaceStatus([state('z1', 'c1', 'occupied', 0.9), state('z2', 'c2', 'unknown')])), { status: 'occupied', conflict: false })
  assert.deepEqual(plain(parking.combineSpaceStatus([state('z1', 'c1', 'unknown')])), { status: 'unknown', conflict: false })
  // Disagreement: higher score wins, occupied on a tie or without scores.
  assert.deepEqual(plain(parking.combineSpaceStatus([state('z1', 'c1', 'occupied', 0.6), state('z2', 'c2', 'empty', 0.9)])), { status: 'empty', conflict: true })
  assert.deepEqual(plain(parking.combineSpaceStatus([state('z1', 'c1', 'occupied'), state('z2', 'c2', 'empty')])), { status: 'occupied', conflict: true })
  // A server verdict decides the status; its conflict flag wins when present.
  assert.deepEqual(plain(parking.combineSpaceStatus([state('z1', 'c1', 'occupied'), state('z2', 'c2', 'empty')], { spaceId: 'A', status: 'empty' })), { status: 'empty', conflict: true })
  assert.deepEqual(plain(parking.combineSpaceStatus([state('z1', 'c1', 'occupied')], { spaceId: 'A', status: 'occupied', conflict: true })), { status: 'occupied', conflict: true })
})

test('summarizeSpaces groups zones of different cameras by parkingSpaceId', () => {
  const spaces = [{ id: 'A', label: 'A-10' }, { id: 'B', label: 'A-2' }, { id: 'C', label: 'unused' }]
  const summaries = parking.summarizeSpaces(spaces, [
    state('z1', 'cam2', 'occupied', 0.8, 'A'),
    state('z2', 'cam1', 'unknown', null, 'A'),
    state('z3', 'cam1', 'empty', 0.7, 'B'),
    state('z4', 'cam1', 'empty', 0.7, null),
  ])
  assert.deepEqual(plain(summaries.map(summary => summary.space.label)), ['A-2', 'A-10'])
  assert.deepEqual(plain(summaries[1].zones.map(zone => zone.cameraId)), ['cam1', 'cam2'])
  assert.equal(summaries[1].status, 'occupied')
  assert.equal(summaries[1].conflict, false)
})

test('status payloads from REST and /ws parking.status_updated', () => {
  const report = parking.parseStatusReport({
    spaces: [
      {
        parkingSpaceId: 'A', label: 'A-01', status: 'OCCUPIED', conflict: true,
        zones: [
          { zoneId: 'z1', cameraId: 'c1', status: 'OCCUPIED', score: 0.91, updatedAt: '2026-10-08T00:00:01Z' },
          { zoneId: 'z2', cameraId: 'c2', status: 'weird', score: 7 },
          { cameraId: 'c3', status: 'EMPTY' },
        ],
      },
      { status: 'EMPTY' },
    ],
  })
  assert.deepEqual(plain(report), {
    zones: [
      { zoneId: 'z1', cameraId: 'c1', parkingSpaceId: 'A', status: 'occupied', score: 0.91, updatedAt: '2026-10-08T00:00:01Z' },
      { zoneId: 'z2', cameraId: 'c2', parkingSpaceId: 'A', status: 'unknown', score: 1, updatedAt: null },
    ],
    spaces: [{ spaceId: 'A', label: 'A-01', status: 'occupied', conflict: true }],
  })
  assert.deepEqual(plain(parking.parseStatusReport(null)), { zones: [], spaces: [] })

  const update = parking.statusReportFromMessage({ type: 'parking.status_updated', data: { spaces: [{ parkingSpaceId: 'A', status: 'EMPTY', conflict: false, zones: [{ zoneId: 'z1', cameraId: 'c1', status: 'EMPTY' }] }] } })
  assert.equal(update.zones[0].status, 'empty')
  assert.equal(parking.statusReportFromMessage({ type: 'camera_status', data: {} }), null)

  const older = { zoneId: 'z', status: 'occupied', revision: 2, updatedAt: '2026-10-08T00:00:05Z' }
  assert.equal(parking.isNewerState(older, { ...older, revision: 1, updatedAt: '2026-10-08T00:00:09Z' }), false)
  assert.equal(parking.isNewerState(older, { ...older, revision: 3, updatedAt: '2026-10-08T00:00:01Z' }), true)
  assert.equal(parking.isNewerState(older, { ...older, updatedAt: '2026-10-08T00:00:04Z' }), false)
  assert.equal(parking.isNewerState(undefined, older), true)
})

function memoryStorage() {
  const values = new Map()
  return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
}

test('mock API: labels are unique, zones save per camera and persist', async () => {
  const storage = memoryStorage()
  const mock = api.createMockParkingApi({ storage, latencyMs: 0, now: () => 0, seed: { spaces: [], zones: [] } })
  assert.equal(mock.kind, 'mock')
  const a01 = await mock.createSpace(' A-01 ')
  assert.equal(a01.label, 'A-01')
  await assert.rejects(mock.createSpace('a-01'), error => error.code === 'duplicate')
  await assert.rejects(mock.createSpace('  '), error => error.code === 'invalid')

  // The same space on two cameras: different polygons, same parkingSpaceId.
  const cam1 = await mock.saveZones('cam1', { base: [], zones: [zone(parking.createZoneId(), a01.id, square, 'cam1')] })
  const other = [{ x: 0.5, y: 0.5 }, { x: 0.9, y: 0.5 }, { x: 0.9, y: 0.9 }]
  const cam2 = await mock.saveZones('cam2', { base: [], zones: [zone(parking.createZoneId(), a01.id, other, 'cam2')] })
  assert.equal(cam1[0].parkingSpaceId, cam2[0].parkingSpaceId)
  assert.notEqual(cam1[0].id, cam2[0].id)
  assert.equal(parking.isClientZoneId(cam1[0].id), false)

  await assert.rejects(mock.saveZones('cam1', { base: [], zones: [zone(parking.createZoneId(), 'missing')] }), error => error.code === 'invalid')

  const reloaded = api.createMockParkingApi({ storage, latencyMs: 0, now: () => 0 })
  assert.equal((await reloaded.listSpaces()).length, 1)
  assert.deepEqual(plain(await reloaded.listZones('cam2')).map(item => item.id), [cam2[0].id])

  const status = await reloaded.getStatus()
  assert.deepEqual(status.zones.map(item => item.zoneId).sort(), [cam1[0].id, cam2[0].id].sort())
  for (const item of status.zones) assert.ok(['occupied', 'empty', 'unknown'].includes(item.status))
})

test('mock seed shares labels across cameras', async () => {
  const mock = api.createMockParkingApi({ storage: null, latencyMs: 0, now: () => 0 })
  const spaces = await mock.listSpaces()
  const a04 = spaces.find(space => space.label === 'A-04')
  const b1 = await mock.listZones('b1_parking_overview')
  const outdoor = await mock.listZones('1f_outdoor_parking')
  assert.ok(b1.some(item => item.parkingSpaceId === a04.id))
  assert.ok(outdoor.some(item => item.parkingSpaceId === a04.id))
})

function fakeServer(routes) {
  const calls = []
  const request = async (url, options = {}) => {
    const method = options.method ?? 'GET'
    calls.push(options.body === undefined ? `${method} ${url}` : `${method} ${url} ${JSON.stringify(options.body)}`)
    const handler = routes[`${method} ${url}`]
    if (!handler) throw Object.assign(new Error('not found'), { statusCode: 404 })
    return handler(options.body)
  }
  return { calls, request }
}

test('HTTP API uses the /api/parking spaces, zones and status endpoints', async () => {
  const server = fakeServer({
    'GET /api/parking/spaces': () => [{ id: 's1', label: 'A-01' }],
    'POST /api/parking/spaces': body => ({ id: 's2', label: body.label }),
    'GET /api/parking/zones?cameraId=cam%201': () => [
      { id: 'z1', cameraId: 'cam 1', parkingSpaceId: 's1', polygon: square, revision: 3 },
      { id: 'zx', cameraId: 'other', parkingSpaceId: 's1', polygon: square },
    ],
    'GET /api/parking/status': () => ({ spaces: [{ parkingSpaceId: 's1', label: 'A-01', status: 'EMPTY', conflict: false, zones: [{ zoneId: 'z1', cameraId: 'cam 1', status: 'EMPTY', score: 0.8 }] }] }),
    'POST /api/parking/zones/z1/calibrate': () => ({}),
    'DELETE /api/parking/zones/z1/calibrate': () => undefined,
  })
  const http = api.createHttpParkingApi(server.request)
  assert.equal(http.kind, 'http')
  assert.deepEqual(plain(await http.listSpaces()), [{ id: 's1', label: 'A-01' }])
  assert.deepEqual(plain(await http.createSpace(' A-02 ')), { id: 's2', label: 'A-02' })
  const zones = await http.listZones('cam 1')
  assert.deepEqual(plain(zones).map(item => item.id), ['z1'])
  assert.equal(zones[0].revision, 3)
  assert.equal((await http.getStatus()).zones[0].status, 'empty')
  await http.calibrateZone('z1')
  await http.resetCalibration('z1')
  assert.deepEqual(server.calls, [
    'GET /api/parking/spaces',
    'POST /api/parking/spaces {"label":"A-02"}',
    'GET /api/parking/zones?cameraId=cam%201',
    'GET /api/parking/status',
    'POST /api/parking/zones/z1/calibrate',
    'DELETE /api/parking/zones/z1/calibrate',
  ])
})

test('HTTP createSpace maps 409 to duplicate; a missing endpoint is reported as not found', async () => {
  const conflict = api.createHttpParkingApi(async () => { throw Object.assign(new Error('conflict'), { statusCode: 409 }) })
  await assert.rejects(conflict.createSpace('A-01'), error => error.code === 'duplicate')
  const missing = api.createHttpParkingApi(fakeServer({}).request)
  await assert.rejects(missing.listSpaces(), error => error.code === 'not-found')
})

test('HTTP save turns the zone diff into DELETE, PATCH and POST, then re-reads the camera', async () => {
  const kept = zone('z1', 's1', square, 'cam')
  const moved = zone('z2', 's2', square, 'cam')
  const removed = zone('z3', 's3', square, 'cam')
  const shifted = square.map(point => ({ x: point.x + 0.1, y: point.y }))
  const added = zone('pz-new', 's4', square, 'cam')
  const server = fakeServer({
    'DELETE /api/parking/zones/z3': () => undefined,
    'PATCH /api/parking/zones/z2': () => ({}),
    'POST /api/parking/zones': () => ({}),
    'GET /api/parking/zones?cameraId=cam': () => [{ id: 'z9', cameraId: 'cam', parkingSpaceId: 's4', polygon: square }],
  })
  const http = api.createHttpParkingApi(server.request)
  const saved = await http.saveZones('cam', {
    base: [kept, moved, removed],
    zones: [kept, { ...moved, parkingSpaceId: 's5', polygon: shifted }, added],
  })
  assert.deepEqual(plain(saved).map(item => item.id), ['z9'])
  assert.deepEqual(server.calls, [
    'DELETE /api/parking/zones/z3',
    `PATCH /api/parking/zones/z2 ${JSON.stringify({ parkingSpaceId: 's5', polygon: shifted })}`,
    `POST /api/parking/zones ${JSON.stringify({ cameraId: 'cam', parkingSpaceId: 's4', polygon: square })}`,
    'GET /api/parking/zones?cameraId=cam',
  ])
})

test('HTTP save reports partial failure with the re-read zones; deleting a gone zone is fine', async () => {
  const server = fakeServer({
    'POST /api/parking/zones': () => { throw Object.assign(new Error('bad'), { statusCode: 422 }) },
    'GET /api/parking/zones?cameraId=cam': () => [],
  })
  const http = api.createHttpParkingApi(server.request)
  await assert.rejects(
    http.saveZones('cam', { base: [zone('gone', 's1')], zones: [zone('pz-1', 's1')] }),
    error => error.code === 'partial' && Array.isArray(error.zones) && /1 of 2 changes/.test(error.message) && /rejected/.test(error.message),
  )
})
