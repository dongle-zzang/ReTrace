const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

const file = path.join(__dirname, '../app/lib/training.ts')
const moduleExports = {}
vm.runInNewContext(ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText,
  { exports: moduleExports, Number, Math, String, Array })
const training = moduleExports
// Values come from another VM realm; compare them as plain data.
const plain = value => JSON.parse(JSON.stringify(value))

const limits = { epochs: [1, 300], batch: [1, 32], seed: [0, 2147483647], patience: [1, 300], imgsz: [640, 960] }
const form = { epochs: 50, imgsz: 640, batch: 4, seed: 42, patience: 15 }
const policy = { min_free_mib: 6000, reserve_mib: 2048, vram_estimate: { base_mib: 150, per_image_mib_at_640: 800 } }

test('form validation mirrors the backend limits before anything is sent', () => {
  assert.deepEqual(plain(training.validateTrainingForm(form, limits)), [])
  assert.deepEqual(plain(training.validateTrainingForm({ ...form, epochs: '50' }, limits)), [])
  for (const [key, value] of [['epochs', 0], ['epochs', 301], ['epochs', '1.5'], ['epochs', ''], ['batch', 33], ['batch', 'four'], ['seed', -1], ['patience', 0]]) {
    assert.equal(training.validateTrainingForm({ ...form, [key]: value }, limits).length, 1, `${key}=${value}`)
  }
  assert.match(training.validateTrainingForm({ ...form, imgsz: 800 }, limits)[0], /640 or 960/)
})

test('VRAM estimate matches the measured 640/960 batch-4 runs and picks a safe batch', () => {
  assert.equal(training.estimateVramMib(640, 4), 3350)
  assert.equal(training.estimateVramMib(960, 4), 7350)
  assert.equal(training.requiredFreeMib(640, 4, policy), 6000)
  assert.equal(training.requiredFreeMib(960, 4, policy), 9398)
  assert.equal(training.safeBatch(960, 11484, policy), 5)
  assert.equal(training.safeBatch(960, 3000, policy), 0)
})

test('chart paths skip missing values and share one domain', () => {
  const points = [{ epoch: 1, a: 1, b: 3 }, { epoch: 2, a: null, b: 2 }, { epoch: 3, a: 2, b: 1 }]
  const domain = training.chartDomain(points, ['a', 'b'])
  assert.deepEqual(plain(domain), { min: 1, max: 3 })
  const layout = { width: 100, height: 50, padding: 10 }
  assert.equal(training.linePath(points, 'a', domain, layout), 'M10.0 40.0 M90.0 25.0')
  assert.equal(training.linePath(points, 'b', domain, layout), 'M10.0 10.0 L50.0 25.0 L90.0 40.0')
  assert.equal(training.chartDomain([{ epoch: 1, a: null }], ['a']), null)
  assert.deepEqual(plain(training.chartDomain([{ epoch: 1, a: 0 }], ['a'])), { min: -0.5, max: 0.5 })
})

test('formatting and box geometry', () => {
  assert.equal(training.formatDuration(59), '59s')
  assert.equal(training.formatDuration(125), '2m 05s')
  assert.equal(training.formatDuration(3720), '1h 02m')
  assert.equal(training.formatMetric(null), '–')
  assert.equal(training.formatMetric(0.12345), '0.123')
  assert.equal(training.deltaLabel(0.6, 0.4), '+0.200')
  assert.equal(training.deltaLabel(0.3, 0.4), '-0.100')
  assert.equal(training.deltaLabel(undefined, 0.4), null)
  assert.deepEqual(plain(training.boxStyle([10, 20, 60, 70], 200, 100)), { left: '5%', top: '20%', width: '25%', height: '50%' })
  assert.ok(training.isActiveStatus('stopping') && !training.isActiveStatus('stopped'))
})

test('Train All epoch hint comes from the latest standard run on the same export', () => {
  const job = (name, created_at, changes = {}) => ({ name, created_at, status: 'completed', export_id: 'e1', config: { kind: 'train', train_mode: 'standard' }, best: { epoch: 20, map50_95: 0.5 }, ...changes })
  const jobs = [
    job('old', '2026-10-01T00:00:00Z', { best: { epoch: 12, map50_95: 0.4 } }),
    job('new', '2026-10-02T00:00:00Z', { best: { epoch: 31, map50_95: 0.6 } }),
    job('all', '2026-10-03T00:00:00Z', { config: { train_mode: 'all' }, best: { epoch: 50, map50_95: null } }),
    job('smoke', '2026-10-04T00:00:00Z', { config: { smoke: true } }),
    job('other', '2026-10-05T00:00:00Z', { export_id: 'e2' }),
    job('failed', '2026-10-06T00:00:00Z', { status: 'failed' }),
    job('eval', '2026-10-07T00:00:00Z', { config: { kind: 'evaluate' } }),
  ]
  assert.deepEqual(plain(training.suggestedTrainAllEpochs(jobs, 'e1')), { epoch: 31, name: 'new' })
  assert.equal(training.suggestedTrainAllEpochs(jobs, 'e3'), null)
  assert.equal(training.jobKind({}), 'standard')
  assert.equal(training.jobKind({ train_mode: 'all' }), 'all')
  assert.equal(training.jobKind({ kind: 'evaluate' }), 'evaluate')
})

test('deployment is offered only for compatible models on an available runtime', () => {
  const model = { id: 'm1', kind: 'finetuned', class_mode: 'vehicle3', config: { imgsz: 640 }, files: { 'best.pt': {} } }
  const runtime = { available: true, active: 'production-original' }
  assert.equal(training.deployBlocker(model, runtime, false), null)
  assert.equal(training.deployBlocker({ ...model, names_count: 80 }, runtime, false), null)
  // 1-class car head (class 0) is deployable; the worker remaps it to the runtime car id.
  assert.equal(training.deployBlocker({ ...model, class_mode: 'single', names_count: 1 }, runtime, false), null)
  assert.match(training.deployBlocker({ ...model, class_mode: 'single', names_count: 3 }, runtime, false), /single car/)
  assert.match(training.deployBlocker({ ...model, class_mode: 'multi', names_count: 3 }, runtime, false), /single car/)
  assert.match(training.deployBlocker({ ...model, names_count: 1 }, runtime, false), /COCO80/)
  assert.match(training.deployBlocker({ ...model, class_mode: 'single', names_count: 1, config: { imgsz: 960 } }, runtime, false), /640px/)
  assert.match(training.deployClasses('single'), /1 class/)
  assert.match(training.deployBlocker({ ...model, files: {} }, runtime, false), /best\.pt/)
  assert.match(training.deployBlocker({ ...model, kind: 'base' }, runtime, false), /best\.pt/)
  assert.match(training.deployBlocker(model, null, false), /enabled/)
  assert.match(training.deployBlocker(model, { ...runtime, available: false }, false), /enabled/)
  assert.match(training.deployBlocker(model, { ...runtime, active: 'm1' }, false), /already active/)
  assert.match(training.deployBlocker(model, runtime, true), /active/)
  assert.equal(training.jobKind({ kind: 'deploy' }), 'deploy')
  assert.equal(training.DEPLOY_PHASES.rolling_back, 'Restoring previous model')
})

test('deployment checks name the failed condition with camera values', () => {
  assert.deepEqual(plain(training.deploymentCheckText({ check: 'fps_below_baseline', camera: 'p1', fps: 7, baseline_fps: 10, baseline_low: 9.5, inference_fps: 7, blocking: true })),
    { key: '{camera}: median {fps} fps, below 85% of the pre-switch median {baseline} fps (own low {low} fps, inferred {inference} fps)',
      params: { camera: 'p1', state: '–', fps: '7', baseline: '10', low: '9.5', inference: '7', was: '–', age: '–', frames: '–', status: '–', seconds: '–', cameras: '–' } })
  // Offline before the switch vs during it; unrelated cameras are listed together.
  assert.match(training.deploymentCheckText({ check: 'camera_offline_before_deployment', camera: 'p2', state: 'offline' }).key, /before the switch/)
  const went = plain(training.deploymentCheckText({ check: 'camera_went_offline', camera: 'p2', state: 'offline', baseline_state: 'online' }))
  assert.equal(went.key, '{camera}: went {state} during the deployment (was {was})')
  assert.equal(went.params.was, 'online')
  assert.match(training.deploymentCheckText({ check: 'camera_went_offline', cameras: ['lobby'] }).key, /Other cameras went offline/)
  assert.match(training.deploymentCheckText({ check: 'camera_fps_variation', camera: 'p1' }).key, /own pre-switch range/)
  assert.match(training.deploymentCheckText({ check: 'fps_below_baseline', camera: 'p1', pipeline_slow: false }).key, /camera source, not the model/)
  assert.match(training.deploymentCheckText({ check: 'fps_below_baseline', camera: 'p1', pipeline_slow: true }).key, /^\{camera\}: median \{fps\} fps, below 85% of the pre-switch median \{baseline\} fps \(own low/)
  assert.match(training.deploymentCheckText({ check: 'vehicle_inference_below_baseline', fps: 12, baseline_fps: 20 }).key, /frames\/s in total/)
  assert.match(training.DIAGNOSIS_VERDICTS.camera_source_suspected, /RTSP source/)
  const service = plain(training.deploymentCheckText({ check: 'service_fps_below_baseline', fps: 72, baseline_fps: 90, cameras: ['lobby', 'hall'] }))
  assert.match(service.key, /Other cameras/)
  assert.equal(service.params.cameras, 'lobby, hall')
  assert.equal(plain(training.deploymentCheckText({ check: 'inference_stalled', camera: 'p1', inference_age: null })).params.age, '–')
  assert.equal(plain(training.deploymentCheckText({ check: 'new_runtime_code' })).key, 'new_runtime_code')
})
