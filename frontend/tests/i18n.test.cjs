const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

const appDir = path.join(__dirname, '../app')
function load(file, required = {}) {
  const exports = {}
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(appDir, file), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText,
    { exports, require: name => required[name] ?? {}, String })
  return exports
}
const i18n = load('lib/i18n.ts')
const { ko } = load('lib/i18n.ko.ts', { './i18n': i18n })

test('locale comes from the saved choice, then the browser language', () => {
  assert.equal(i18n.initialLocale('en', ['ko-KR']), 'en')
  assert.equal(i18n.initialLocale(null, ['ko-KR', 'en-US']), 'ko')
  assert.equal(i18n.initialLocale('fr', ['en-US', 'ko']), 'ko')
  assert.equal(i18n.initialLocale(null, ['en-US']), 'en')
  assert.equal(i18n.initialLocale(undefined, []), 'en')
})

test('translation interpolates params and falls back to the English text', () => {
  assert.equal(i18n.translate(null, 'Showing {shown} of {total} cameras', { shown: 2, total: 5 }), 'Showing 2 of 5 cameras')
  assert.equal(i18n.translate(ko, 'Showing {shown} of {total} cameras', { shown: 2, total: 5 }), '카메라 5대 중 2대 표시')
  assert.equal(i18n.translate(ko, 'Server sent text we do not know'), 'Server sent text we do not know')
  assert.equal(i18n.translate(ko, 'Use at most 40 characters'), '최대 40자까지 입력할 수 있습니다')
  assert.equal(i18n.translate(ko, 'Epochs must be a whole number from 1 to 300.'), 'Epochs는 1–300 사이의 정수여야 합니다.')
  assert.equal(i18n.translate(ko, '{missing} stays'), '{missing} stays')
})

test('every literal t() key in app/ has a Korean translation with the same placeholders', () => {
  const files = []
  const walk = dir => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) { if (entry.name !== 'ui') walk(full) }
      else if (/\.(vue|ts)$/.test(entry.name) && !entry.name.startsWith('i18n')) files.push(full)
    }
  }
  walk(appDir)
  const missing = [], mismatched = []
  const placeholders = text => [...text.matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort().join(',')
  for (const file of files) {
    const source = fs.readFileSync(file, 'utf8')
    for (const match of source.matchAll(/\bt\(\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")/g)) {
      const key = (match[1] ?? match[2]).replace(/\\(.)/g, '$1')
      const message = ko.messages[key]
      if (message === undefined) missing.push(`${path.relative(appDir, file)}: ${key}`)
      else if (placeholders(message) !== placeholders(key)) mismatched.push(key)
    }
  }
  assert.deepEqual(missing, [])
  assert.deepEqual(mismatched, [])
})
