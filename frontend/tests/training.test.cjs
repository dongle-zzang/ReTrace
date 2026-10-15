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
