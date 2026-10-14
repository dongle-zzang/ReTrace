const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const { parse, compileScript } = require('vue/compiler-sfc')
const i18n = require('./i18nStub.cjs')

const appDir = path.join(__dirname, '../app')
function loadModule(specifier) {
  const file = path.join(appDir, specifier.replace(/^~\//, '') + '.ts')
  return evaluate(fs.readFileSync(file, 'utf8'))
}
function evaluate(source) {
  const exports = {}
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  vm.runInNewContext(compiled, {
    ...vue, exports, Error,
    require: specifier => specifier === 'vue' ? vue : specifier.endsWith('/useI18n') ? i18n : specifier.startsWith('~/lib/') ? loadModule(specifier) : {},
  })
  return exports
}

// Mount the real SFC setup in Vue's renderer so watches and unmount invalidation run as in the UI.
// Rendering native input controls is covered separately by the browser verification.
const filename = path.join(appDir, 'components/ParkingZoneInspector.vue')
const { descriptor } = parse(fs.readFileSync(filename, 'utf8'), { filename })
const componentSource = compileScript(descriptor, { id: 'parking-inspector-test' }).content
const renderer = vue.createRenderer({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null,
})
const polygon = [{ x: .1, y: .1 }, { x: .3, y: .1 }, { x: .2, y: .3 }]
const zone = (id, parkingSpaceId = '') => ({ id, cameraId: 'camera', parkingSpaceId, polygon })
function mount(createSpace) {
  const props = vue.reactive({
    zone: zone('new'), zones: [zone('new'), zone('used', 's1')],
    spaces: [{ id: 's1', label: 'A-01' }, { id: 'shared', label: 'B-01' }],
    disabled: false, createSpace,
  })
  const component = evaluate(componentSource).default
  let state
  const setup = component.setup
  component.setup = (props, context) => {
    state = setup(props, context)
    return () => null
  }
  const assignments = []
  const root = { render: () => vue.h(component, { ...props, onAssign: (...args) => assignments.push(args) }) }
  const container = {}
  renderer.render(vue.h(root), container)
  state.focus()
  return { props, state, assignments, unmount: () => renderer.render(null, container) }
}
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

test('label selection requires apply, rejects labels used here, and reuses an available label', async () => {
  let creates = 0
  const ui = mount(async () => { creates++; throw Error('must not create') })
  try {
    ui.state.query.value = 'A-01'
    await vue.nextTick()
    assert.equal(ui.assignments.length, 0)
    await ui.state.apply()
    assert.match(ui.state.error.value, /already used/)
    assert.equal(ui.assignments.length, 0)
    ui.state.query.value = ' b-01 '
    await vue.nextTick()
    await ui.state.apply()
    assert.deepEqual(ui.assignments, [['new', 'shared']])
    assert.equal(creates, 0)
  } finally { ui.unmount() }
})

test('failed label creation retains the polygon and input and allows retry', async () => {
  let fail = true
  const ui = mount(async label => {
    if (fail) throw Error('Server unavailable')
    return { id: 'created', label }
  })
  try {
    ui.state.query.value = 'C-03'
    await vue.nextTick()
    await ui.state.apply()
    assert.equal(ui.state.query.value, 'C-03')
    assert.equal(ui.state.error.value, 'Server unavailable')
    assert.deepEqual(ui.props.zone.polygon, polygon)
    assert.equal(ui.state.busy.value, false)
    fail = false
    await ui.state.apply()
    assert.deepEqual(ui.assignments, [['new', 'created']])
  } finally { ui.unmount() }
})

test('a late label response cannot relabel a different selected zone', async () => {
  const request = deferred()
  const ui = mount(() => request.promise)
  try {
    ui.state.query.value = 'SLOW'
    await vue.nextTick()
    const applying = ui.state.apply()
    ui.props.zone = zone('other', 'shared')
    await vue.nextTick()
    request.resolve({ id: 'created', label: 'SLOW' })
    await applying
    assert.equal(ui.assignments.length, 0)
    assert.equal(ui.state.query.value, 'B-01')
  } finally { ui.unmount() }
})

test('cancel/unmount invalidates an in-flight label creation', async () => {
  const request = deferred()
  const ui = mount(() => request.promise)
  ui.state.query.value = 'SLOW'
  await vue.nextTick()
  const applying = ui.state.apply()
  ui.unmount()
  request.resolve({ id: 'created', label: 'SLOW' })
  await applying
  assert.equal(ui.assignments.length, 0)
})


test('moving vertices keeps typed labels and does not cancel a pending label application', async () => {
  const request = deferred()
  const ui = mount(() => request.promise)
  try {
    ui.state.query.value = 'SLOW'
    await vue.nextTick()
    const applying = ui.state.apply()
    ui.props.zone = { ...ui.props.zone, polygon: polygon.map(point => ({ ...point, x: point.x + .1 })) }
    await vue.nextTick()
    assert.equal(ui.state.query.value, 'SLOW')
    request.resolve({ id: 'created', label: 'SLOW' })
    await applying
    assert.deepEqual(ui.assignments, [['new', 'created']])
  } finally { ui.unmount() }
})


test('closing the label modal cancels a pending application and reopening restores the current label', async () => {
  const request = deferred()
  const ui = mount(() => request.promise)
  try {
    ui.state.query.value = 'SLOW'
    await vue.nextTick()
    const applying = ui.state.apply()
    ui.state.setOpen(false)
    request.resolve({ id: 'created', label: 'SLOW' })
    await applying
    assert.equal(ui.assignments.length, 0)
    assert.equal(ui.state.open.value, false)
    ui.state.focus()
    assert.equal(ui.state.query.value, '')
    assert.equal(ui.state.busy.value, false)
  } finally { ui.unmount() }
})


test('Escape closes the modal without propagating a deselect/delete shortcut to the editor', () => {
  const ui = mount(async () => { throw Error('unexpected creation') })
  try {
    let stopped = false
    let prevented = false
    ui.state.onDialogKeydown({ key: 'Escape', stopPropagation() { stopped = true }, preventDefault() { prevented = true } })
    assert.equal(stopped, true)
    assert.equal(prevented, true)
    assert.equal(ui.state.open.value, false)
    assert.equal(ui.assignments.length, 0)
  } finally { ui.unmount() }
})
