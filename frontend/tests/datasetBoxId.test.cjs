const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vm = require('node:vm')
const { webcrypto } = require('node:crypto')
const source = fs.readFileSync(path.join(__dirname, '../app/lib/datasetBoxId.ts'), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function load(crypto) {
  const exports = {}
  vm.runInNewContext(compiled, { exports, crypto })
  return exports.createDatasetBoxId
}

test('HTTP context without randomUUID generates distinct API-compatible v4 UUIDs', () => {
  const create = load({ getRandomValues: webcrypto.getRandomValues.bind(webcrypto) })
  const ids = Array.from({ length: 100 }, () => create())
  for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  assert.equal(new Set(ids).size, ids.length)
})

test('secure context uses native UUID generation with its crypto receiver', () => {
  const crypto = { randomUUID() { assert.equal(this, crypto); return 'native-uuid' } }
  assert.equal(load(crypto)(), 'native-uuid')
})
