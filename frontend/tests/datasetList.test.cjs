const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const { parse, compileScript } = require('vue/compiler-sfc')
const i18n = require('./i18nStub.cjs')

const filename = path.join(__dirname, '../app/pages/datasets/index.vue')
const { descriptor } = parse(fs.readFileSync(filename, 'utf8'), { filename })
const source = compileScript(descriptor, { id: 'dataset-list-test' }).content
function evaluate(source, globals = {}) {
  const exports = {}
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, { exports, ...globals })
  return exports
}
const helpers = evaluate(fs.readFileSync(path.join(__dirname, '../app/lib/datasetNavigation.ts'), 'utf8'))
const renderer = vue.createRenderer({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null,
})
const flush = async () => { for (let i = 0; i < 5; i++) await vue.nextTick() }
function mount({ width = 1920, page = 1, size = 8, request } = {}) {
  const navigation = { listCamera: vue.ref(''), listStatus: vue.ref(''), listPage: vue.ref(page), listPageSize: vue.ref(size), prepare: async () => {} }
  const calls = [], listeners = new Map()
  const window = { innerWidth: width, addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }
  const datasetRequest = async (url, options) => {
    calls.push(url)
    if (request) return request(url, options)
    if (url === '/exports') return []
    const query = new URLSearchParams(url.split('?')[1])
    const page = Number(query.get('page')), page_size = Number(query.get('page_size'))
    return { items: Array.from({ length: page_size }, (_, i) => ({ id: String((page - 1) * page_size + i) })), total: 40, page, page_size }
  }
  const component = evaluate(source, {
    ...vue, window, URLSearchParams, setInterval: () => 1, clearInterval() {}, setTimeout: () => 1, clearTimeout() {},
    useHead() {}, useRoute: () => ({ query: {} }), useRuntimeConfig: () => ({ public: {} }), useDatasetNavigation: () => navigation,
    useConfirmDialog: () => ({ confirm: async () => true }),
    require: name => name === 'vue' ? vue : name.endsWith('/useI18n') ? i18n : name.endsWith('/datasetNavigation') ? helpers
      : name.endsWith('/datasetApi') ? { datasetRequest }
      : name.endsWith('/datasetAutoLabel') ? { isAutoLabelActive: () => false, autoLabelPercent: () => 0, readAutoLabelSettings: () => null }
      : name.endsWith('/autolabelDemo') ? { isAutoLabelDemo: () => false, AUTO_LABEL_DEMO_QUERY: {} }
      : name.endsWith('/trainingApi') ? { trainingRequest: async () => { throw new Error('training API offline') }, useTrainingToken: () => ({ token: vue.ref(''), setToken() {} }) }
      : name.endsWith('/useCameras') ? { useCameras: () => ({ cameras: vue.ref([]) }) }
      : name.endsWith('/useRealtimePreview') ? { useRealtimePreview: () => ({}) }
      : name.endsWith('/types/dataset') ? { DATASET_STATUS_LABELS: { pending: 'Pending' } } : {},
  }).default
  let state
  const setup = component.setup
  component.setup = (props, context) => { state = setup(props, context); return () => null }
  const container = {}
  renderer.render(vue.h(component), container)
  return { state, calls, navigation, resize(width) { window.innerWidth = width; listeners.get('resize')() }, unmount: () => renderer.render(null, container) }
}

test('resize, return and filters preserve ordinal positions and request two-row pages', async () => {
  const ui = mount({ page: 3, size: 8, width: 900 })
  try {
    await flush()
    assert.equal(ui.state.page.value, 5)
    assert.equal(ui.state.visibleImages.value.length, 4)
    assert.match(ui.calls[0], /page=5&page_size=4/)
    ui.resize(390)
    assert.equal(ui.state.visibleImages.value.length, 2)
    await flush()
    assert.equal(ui.state.page.value, 9)
    assert.equal(ui.state.images.value.items[0].id, '16')
    ui.navigation.listCamera.value = 'parking-camera'
    await flush()
    assert.equal(ui.state.page.value, 1)
    assert.match(ui.calls.at(-1), /camera_id=parking-camera/)
    ui.state.page.value = 2
    ui.navigation.listStatus.value = 'pending'
    await flush()
    assert.equal(ui.state.page.value, 1)
    assert.match(ui.calls.at(-1), /status=pending/)
  } finally { ui.unmount() }
})

test('stale responses cannot replace a more recent responsive query', async () => {
  const pending = []
  const ui = mount({ request: url => url === '/exports' ? [] : new Promise(resolve => pending.push({ url, resolve })) })
  try {
    ui.resize(390)
    pending[1].resolve({ items: [{ id: 'new' }], total: 1, page: 1, page_size: 2 })
    await flush()
    pending[0].resolve({ items: [{ id: 'stale' }], total: 30, page: 1, page_size: 8 })
    await flush()
    assert.equal(ui.state.images.value.items[0].id, 'new')
    assert.equal(ui.state.totalPages.value, 1)
  } finally { ui.unmount() }
})

test('deleting the last image of the last page reloads the previous valid page', async () => {
  let total = 9
  const ui = mount({ page: 2, request: (url, options) => {
    if (options?.method === 'DELETE') { total--; return }
    if (url === '/exports') return []
    const query = new URLSearchParams(url.split('?')[1])
    const page = Number(query.get('page')), page_size = Number(query.get('page_size'))
    return { items: Array.from({ length: Math.max(0, Math.min(page_size, total - (page - 1) * page_size)) }, (_, i) => ({ id: String(i) })), total, page, page_size }
  } })
  try {
    await flush()
    await ui.state.remove({ id: 'last' })
    assert.equal(ui.state.page.value, 1)
    assert.equal(ui.state.visibleImages.value.length, 8)
    assert.equal(ui.state.totalPages.value, 1)
  } finally { ui.unmount() }
})
