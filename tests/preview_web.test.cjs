"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function frame(cameraId, sequence, jpeg) {
  const name = Buffer.from(cameraId);
  const header = Buffer.alloc(6 + name.length);
  header[0] = 1;
  header[1] = name.length;
  name.copy(header, 2);
  header.writeUInt32BE(sequence, 2 + name.length);
  const bytes = Buffer.concat([header, Buffer.from(jpeg)]);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length);
}

test("21 cameras share one WebSocket, decode newest frames, reconnect and release on exit", async () => {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; }
    append(...children) { this.children.push(...children); }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; if (name === "src") this.src = undefined; }
  }
  const elements = {};
  const document = {
    createElement: tag => new Element(tag),
    getElementById: id => elements[id] || (elements[id] = new Element(id))
  };
  const window = { listeners: {}, addEventListener(name, cb) { this.listeners[name] = cb; } };
  const sources = Array.from({length: 21}, (_, id) => ({ id, camera_id: `cam-${id}`, name: `카메라 ${id}`,
    format: "mjpeg", url: `/mjpeg/source${id}`, runtime: { state: "connecting", fps: 0 } }));
  const sockets = [];
  class FakeWebSocket {
    constructor(url) { this.url = String(url); this.sent = []; sockets.push(this); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() { this.closed = true; }
  }
  const objectUrls = new Set();
  let nextUrl = 0;
  const timers = [];
  const context = vm.createContext({
    document, window, console, Blob, TextDecoder, WebSocket: FakeWebSocket,
    location: { href: "http://server:40225/diagnostics" },
    URL: Object.assign(function (value, base) { return new URL(value, base); }, {
      createObjectURL: blob => { const url = `blob:${nextUrl++}:${blob.size}`; objectUrls.add(url); return url; },
      revokeObjectURL: url => objectUrls.delete(url),
    }),
    fetch: async url => { assert.equal(url, "/streams.json"); return { ok: true, json: async () => sources }; },
    setTimeout: callback => { timers.push(callback); return timers.length; }, clearTimeout() {},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/preview.js"), "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));

  const cards = elements.streams.children;
  assert.equal(cards.length, 21);
  assert.match(elements["page-status"].textContent, /21개 입력/);
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url, "ws://server:40225/ws");
  assert.equal(sockets[0].binaryType, "arraybuffer");
  sockets[0].onopen();
  assert.deepEqual(sockets[0].sent, [{ version: 1, type: "subscribe", cameraIds: sources.map(s => s.camera_id),
    video: true, videoFps: 10 }]);

  const image = cards[20].children[1];
  sockets[0].onmessage({ data: frame("cam-20", 1, "jpeg-1") });
  const first = image.src;
  assert.match(first, /^blob:/);
  // Two frames arrive while the first decodes: only the newest is decoded next.
  sockets[0].onmessage({ data: frame("cam-20", 2, "jpeg-2") });
  sockets[0].onmessage({ data: frame("cam-20", 3, "jpeg-three") });
  assert.equal(image.src, first);
  image.onload();
  assert.match(image.src, /:10$/);
  // The shown frame's URL is revoked only after its replacement has loaded.
  assert.ok(objectUrls.has(first));
  image.onload();
  assert.ok(!objectUrls.has(first));
  assert.equal(objectUrls.size, 1);

  sockets[0].onmessage({ data: JSON.stringify({ type: "camera_status", cameraId: "cam-20",
    status: { state: "online", fps: 12.5 } }) });
  assert.match(cards[20].children[2].textContent, /재생 중 · 12\.5 FPS/);
  sockets[0].onmessage({ data: frame("unknown", 1, "ignored") });

  sockets[0].onclose();
  assert.match(cards[20].children[2].textContent, /서버 연결 중/);
  timers.shift()();
  assert.equal(sockets.length, 2);
  sockets[1].onopen();
  assert.equal(sockets[1].sent[0].cameraIds.length, 21);

  window.listeners.pagehide();
  assert.ok(sockets[1].closed);
  assert.equal(objectUrls.size, 0);
  assert.equal(image.src, undefined);
  sockets[1].onclose();
  assert.equal(timers.length, 0);
});
