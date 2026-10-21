const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vm = require('node:vm')
const source = fs.readFileSync(path.join(__dirname, '../app/lib/datasetAutoLabel.ts'), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const lib = {}
vm.runInNewContext(compiled, { exports: lib })

const auto = { id: 'a', class_id: 0, x: 10, y: 10, width: 50, height: 40, source: 'auto', score: 0.87 }
const manual = { id: 'm', class_id: 0, x: 100, y: 100, width: 20, height: 20, source: 'manual' }

test('an untouched auto box keeps its source and score', () => {
  const next = lib.markEditedBoxes([auto, manual], [auto, { ...manual, x: 120 }])
  assert.deepEqual(next[0], auto)
  assert.equal(next[1].source, 'manual')
})

test('moving or resizing an auto box makes it manual without a score', () => {
  const [moved] = lib.markEditedBoxes([auto], [{ ...auto, x: 12 }])
  assert.equal(moved.source, 'manual')
  assert.equal('score' in moved, false)
  const [resized] = lib.markEditedBoxes([auto], [{ ...auto, width: 60 }])
  assert.equal(resized.source, 'manual')
})

test('new boxes pass through and counts only include auto boxes', () => {
  const drawn = { ...manual, id: 'n' }
  const next = lib.markEditedBoxes([auto], [auto, drawn])
  assert.equal(next[1], drawn)
  assert.equal(lib.autoBoxCount(next), 1)
})

test('score and progress formatting', () => {
  assert.equal(lib.formatScore(0.874), '87%')
  assert.equal(lib.formatScore(null), '')
  assert.equal(lib.autoLabelPercent({ progress: 0.1, dataset_summary: { total: 40, processed: 10 } }), 25)
  assert.equal(lib.autoLabelPercent({ progress: 0.5, dataset_summary: null }), 50)
  assert.equal(lib.autoLabelPercent({ progress: null, dataset_summary: { total: 0, processed: 0 } }), 0)
  assert.equal(lib.isAutoLabelActive('running'), true)
  assert.equal(lib.isAutoLabelActive('completed'), false)
})

test('model proposals overlapping an existing box are skipped', () => {
  const proposals = [
    { ...auto, id: 'p1', x: 12, y: 11 }, // same vehicle as the existing auto box
    { ...auto, id: 'p2', x: 400, y: 300 },
    { ...auto, id: 'p3', x: 402, y: 301 }, // duplicate of p2 inside the same proposal set
  ]
  const { boxes, added } = lib.mergeAutoBoxes([auto, manual], proposals)
  assert.equal(added, 1)
  assert.deepEqual([...boxes.map(box => box.id)], ['a', 'm', 'p2'])
  assert.equal(lib.boxIou(auto, { ...auto, x: 1000 }), 0)
})

test('auto-label settings round-trip and reject broken values', () => {
  const store = new Map()
  const storage = { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) }
  assert.equal(lib.readAutoLabelSettings(storage), null)
  lib.saveAutoLabelSettings({ model_id: 'm1', conf: 0.99 }, storage)
  assert.deepEqual({ ...lib.readAutoLabelSettings(storage) }, { model_id: 'm1', conf: 0.9 })
  store.set('retrace.autolabelSettings', '{oops')
  assert.equal(lib.readAutoLabelSettings(storage), null)
  assert.equal(lib.readAutoLabelSettings({ getItem() { throw new Error('blocked') } }), null)
})
