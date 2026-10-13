const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vm = require('node:vm')
const source = fs.readFileSync(path.join(__dirname, '../app/lib/datasetNavigation.ts'), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const exportsObject = {}
vm.runInNewContext(compiled, { exports: exportsObject, Set })
const { collectDatasetIds, datasetNeighbors } = exportsObject

test('navigation spans pages in displayed order and retains the viewed filtered page', async () => {
  const current = { items: [{ id: 'camera-b-new' }, { id: 'camera-a-old' }], total: 5, page: 2, page_size: 2 }
  const requested = []
  const ids = await collectDatasetIds(current, async page => {
    requested.push(page)
    return { items: (page === 1 ? ['latest-a', 'latest-b'] : ['oldest']).map(id => ({ id })) }
  })
  assert.deepEqual(requested, [1, 3])
  assert.deepEqual(Array.from(ids), ['latest-a', 'latest-b', 'camera-b-new', 'camera-a-old', 'oldest'])
  assert.equal(datasetNeighbors(ids, 'camera-b-new').previous, 'latest-b')
  assert.equal(datasetNeighbors(ids, 'camera-b-new').next, 'camera-a-old')
  // Reviewing the current pending image must not remove it or change its neighbors.
  current.items[0].status = 'reviewed'
  assert.equal(datasetNeighbors(ids, 'camera-b-new').next, 'camera-a-old')
  assert.equal(datasetNeighbors(ids, 'latest-a').previous, null)
  assert.equal(datasetNeighbors(ids, 'oldest').next, null)
  assert.equal(datasetNeighbors(ids, 'deleted').next, null)
})
