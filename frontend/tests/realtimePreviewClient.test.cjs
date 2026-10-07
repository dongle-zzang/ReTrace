const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

function loadClient() {
  const source = fs.readFileSync(path.join(__dirname, '../app/composables/realtimePreviewClient.ts'), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  const sockets = []
  const timers = []
  const created = []
  const revoked = []
  class FakeWebSocket {
    static CONNECTING = 0
    static OPEN = 1
    constructor(url) {
      this.url = url
      this.readyState = 0
      this.sent = []
      sockets.push(this)
    }
    send(value) { this.sent.push(JSON.parse(value)) }
    close() { this.readyState = 3; this.onclose?.() }
    open() { this.readyState = 1; this.onopen?.() }
    receive(message) { this.onmessage?.({ data: JSON.stringify(message) }) }
    receiveBinary(buffer) { this.onmessage?.({ data: buffer }) }
  }
  let urlCounter = 0
  const FakeURL = class extends URL {
    static createObjectURL(blob) { const url = `blob:${++urlCounter}`; created.push({ url, blob }); return url }
    static revokeObjectURL(url) { revoked.push(url) }
  }
  vm.runInNewContext(compiled, {
    exports, WebSocket: FakeWebSocket, URL: FakeURL, Blob, TextDecoder, DataView, Uint8Array, ArrayBuffer,
    queueMicrotask, window: { location: { href: 'http://localhost:3000/' } },
    setTimeout: (fn, delay) => { const timer = { fn, delay, cancelled: false }; timers.push(timer); return timer },
    clearTimeout: timer => { timer.cancelled = true },
  })
  return { ...exports, sockets, timers, created, revoked }
}

function frame(cameraId, sequence, jpeg) {
  const id = Buffer.from(cameraId, 'utf8')
  const buffer = new ArrayBuffer(6 + id.length + jpeg.length)
  const bytes = new Uint8Array(buffer)
  bytes[0] = 1
  bytes[1] = id.length
  bytes.set(id, 2)
  new DataView(buffer).setUint32(2 + id.length, sequence)
  bytes.set(jpeg, 6 + id.length)
  return buffer
}

const tick = () => new Promise(resolve => setImmediate(resolve))

test('parses the binary video frame layout', () => {
  const { parseVideoFrame } = loadClient()
  const parsed = parseVideoFrame(frame('1f_로비', 0xfffffffe, [0xff, 0xd8, 0xff]))
  assert.equal(parsed.cameraId, '1f_로비')
  assert.equal(parsed.sequence, 0xfffffffe)
  assert.deepEqual([...parsed.jpeg], [0xff, 0xd8, 0xff])
  assert.equal(parseVideoFrame(frame('a', 1, [])), null)
  const wrongVersion = frame('a', 1, [1])
  new Uint8Array(wrongVersion)[0] = 2
  assert.equal(parseVideoFrame(wrongVersion), null)
})

test('one socket subscribes the watched cameras with video and routes frames', async () => {
  const { RealtimePreviewClient, sockets } = loadClient()
  const states = []
  const messages = []
  const client = new RealtimePreviewClient('/ws', 12, message => messages.push(message), state => states.push(state))
  const framesA = []
  const framesB = []
  client.watch('camera-b', f => framesB.push(f))
  const stopA = client.watch('camera-a', f => framesA.push(f))
  await tick()
  assert.equal(sockets.length, 1)
  const socket = sockets[0]
  assert.equal(socket.url.toString(), 'ws://localhost:3000/ws')
  assert.equal(socket.binaryType, 'arraybuffer')
  assert.equal(socket.sent.length, 0)
  socket.open()
  assert.deepEqual(states, ['connecting', 'open'])
  assert.deepEqual(JSON.parse(JSON.stringify(socket.sent)), [
    { version: 1, type: 'subscribe', cameraIds: ['camera-a', 'camera-b'], video: true, videoFps: 12 },
  ])

  socket.receiveBinary(frame('camera-a', 7, [1, 2]))
  socket.receive({ version: 1, type: 'camera_status', cameraId: 'camera-a', status: { state: 'online', fps: 9.5 } })
  assert.equal(framesA.length, 1)
  assert.equal(framesA[0].sequence, 7)
  assert.equal(framesB.length, 0)
  assert.equal(messages[0].type, 'camera_status')

  stopA()
  await tick()
  assert.deepEqual([...socket.sent.at(-1).cameraIds], ['camera-b'])
  socket.receiveBinary(frame('camera-a', 8, [1]))
  assert.equal(framesA.length, 1)
  client.close()
  assert.equal(states.at(-1), 'closed')
})

test('reconnects after 2 seconds and resubscribes; close stops reconnecting', async () => {
  const { RealtimePreviewClient, sockets, timers } = loadClient()
  const states = []
  const client = new RealtimePreviewClient('/ws', 10, () => {}, state => states.push(state))
  client.watch('camera-a', () => {})
  sockets[0].open()
  await tick()
  sockets[0].close()
  assert.equal(states.at(-1), 'reconnecting')
  assert.equal(timers.length, 1)
  assert.equal(timers[0].delay, 2000)
  timers.shift().fn()
  assert.equal(sockets.length, 2)
  sockets[1].open()
  assert.deepEqual([...sockets[1].sent[0].cameraIds], ['camera-a'])
  assert.equal(states.at(-1), 'open')

  client.close()
  assert.equal(sockets[1].readyState, 3)
  assert.equal(timers.length, 0)
  assert.equal(sockets.length, 2)
})

test('frame view decodes offscreen one at a time, then swaps and revokes old URLs', async () => {
  const { JpegFrameView, created, revoked } = loadClient()
  const image = { src: '', removeAttribute(name) { if (name === 'src') this.src = '' } }
  const decoders = []
  const createImage = () => {
    const decoder = { src: '', naturalWidth: 1280, naturalHeight: 720 }
    decoder.decode = () => new Promise((resolve, reject) => { decoder.resolve = resolve; decoder.reject = reject })
    decoders.push(decoder)
    return decoder
  }
  const sizes = []
  const view = new JpegFrameView(image, size => sizes.push(size), createImage)

  view.push(new Uint8Array([1]))
  assert.equal(decoders[0].src, 'blob:1')
  assert.equal(image.src, '', 'visible image waits for decode')
  view.push(new Uint8Array([2]))
  view.push(new Uint8Array([3]))
  assert.equal(created.length, 1, 'frames wait in one slot while decoding')

  decoders[0].resolve()
  await tick()
  assert.equal(image.src, 'blob:1')
  assert.deepEqual(JSON.parse(JSON.stringify(sizes)), [{ width: 1280, height: 720 }])
  assert.equal(created.length, 2)
  assert.equal(decoders[1].src, 'blob:2')
  assert.equal(created[1].blob.type, 'image/jpeg')
  assert.equal(created[1].blob.size, 1)
  assert.deepEqual(revoked, [])

  decoders[1].resolve()
  await tick()
  assert.equal(image.src, 'blob:2')
  assert.deepEqual(revoked, ['blob:1'])

  view.push(new Uint8Array([4]))
  decoders[2].reject(new Error('corrupt'))
  await tick()
  assert.equal(image.src, 'blob:2', 'failed decode keeps the last good frame')
  assert.deepEqual(revoked, ['blob:1', 'blob:3'])

  view.push(new Uint8Array([5]))
  view.clear()
  decoders[3].resolve()
  await tick()
  assert.deepEqual([...new Set(revoked)].sort(), ['blob:1', 'blob:2', 'blob:3', 'blob:4'])
  assert.equal(image.src, '', 'a decode finishing after clear does not repaint')
  assert.equal(view.hasFrame, false)
})
