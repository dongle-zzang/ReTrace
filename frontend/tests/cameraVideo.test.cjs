const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const { parse, compileScript } = require('vue/compiler-sfc')
const filename = path.join(__dirname, '../app/components/CameraVideo.vue')
const { descriptor } = parse(fs.readFileSync(filename, 'utf8'), { filename })
const source = compileScript(descriptor, { id: 'camera-video-test' }).content
const renderer = vue.createRenderer({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null,
})
function mount() {
  const exports = {}
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
    ...vue, exports, require: name => name === 'vue' ? vue : {},
  })
  const component = exports.default
  let state
  const setup = component.setup
  component.setup = (props, context) => { state = setup(props, context); return () => null }
  const props = vue.reactive({ camera: { camera_id: 'camera', name: 'Parking', enabled: true, status: { state: 'online', stale: false } }, active: true, realtimeClient: {}, connection: 'open' })
  const emitted = []
  const container = {}
  renderer.render(vue.h({ render: () => vue.h(component, { ...props, onTransport: value => emitted.push(value) }) }), container)
  return { props, emitted, get state() { return state }, unmount: () => renderer.render(null, container) }
}

test('video readiness gates overlays until a decoded frame, and resets on offline/reconnect', async () => {
  const ui = mount()
  try {
    assert.equal(ui.state.state.value, 'connecting')
    ui.state.onFrameShown({ width: 1280, height: 720 })
    await vue.nextTick()
    assert.equal(ui.emitted.at(-1), 'connected')
    ui.props.camera.status.state = 'offline'
    await vue.nextTick()
    assert.equal(ui.emitted.at(-1), 'connecting')
    ui.props.camera.status.state = 'online'
    await vue.nextTick()
    assert.equal(ui.state.state.value, 'connecting')
    ui.state.onFrameShown({ width: 1280, height: 720 })
    await vue.nextTick()
    ui.props.connection = 'reconnecting'
    await vue.nextTick()
    assert.equal(ui.emitted.at(-1), 'reconnecting')
    ui.props.connection = 'open'
    await vue.nextTick()
    assert.equal(ui.state.state.value, 'connecting')
    ui.state.onFrameShown({ width: 1280, height: 720 })
    await vue.nextTick()
    assert.equal(ui.emitted.at(-1), 'connected')
    ui.props.camera.enabled = false
    await vue.nextTick()
    assert.equal(ui.emitted.at(-1), 'waiting')
  } finally { ui.unmount() }
})

test('live starting status or stale snapshot suppresses even an already decoded frame', async () => {
  const ui = mount()
  try {
    ui.props.cameraState = 'starting'
    await vue.nextTick()
    ui.state.onFrameShown({ width: 1280, height: 720 })
    assert.equal(ui.state.state.value, 'connecting')
    ui.props.cameraState = 'online'
    await vue.nextTick()
    assert.equal(ui.state.state.value, 'connecting')
    ui.props.cameraState = undefined
    ui.props.camera.status.stale = true
    await vue.nextTick()
    assert.equal(ui.state.state.value, 'connecting')
    ui.props.active = false
    await vue.nextTick()
    assert.equal(ui.emitted.at(-1), 'waiting')
  } finally { ui.unmount() }
})
