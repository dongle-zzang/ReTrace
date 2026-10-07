"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("one WebSocket multiplexes two peers, buffers ICE and rebuilds after reconnect", async () => {
  const sockets = [], peers = [], messages = [], timers = [];
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(data) { this.sent.push(JSON.parse(data)); }
    open() { this.readyState = 1; this.onopen(); }
    close() { this.readyState = 3; this.onclose(); }
  }
  class Peer {
    constructor(config) { this.config = config; this.candidates = []; peers.push(this); }
    close() { this.closed = true; }
    async setRemoteDescription(description) { this.remoteDescription = description; }
    async createAnswer() { return { type: "answer", sdp: "answer-sdp" }; }
    async setLocalDescription(description) { this.localDescription = description; }
    async addIceCandidate(candidate) { this.candidates.push(candidate); }
  }
  const window = {};
  const context = vm.createContext({ window, URL, location: { href: "https://preview.example/" },
    WebSocket: Socket, RTCPeerConnection: Peer, Date, MediaStream: class {},
    setTimeout(callback) { timers.push(callback); return timers.length; }, clearTimeout() {} });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/webrtc.js"), "utf8"), context);
  const realtime = new window.ReTraceRealtime("/ws", message => messages.push(message));
  const video = () => ({ srcObject: null, play: async () => {} });
  const firstVideo = video(), secondVideo = video();
  const stopFirst = realtime.watch("first", firstVideo);
  realtime.watch("second", secondVideo);
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url.protocol, "wss:");
  sockets[0].open();
  const watches = sockets[0].sent.filter(message => message.type === "watch");
  assert.equal(watches.length, 2);
  assert.equal(peers.length, 2);
  const firstId = watches[0].peerId;
  await realtime.receive({ type: "ice", cameraId: "first", peerId: firstId,
    candidate: "candidate:before-sdp", sdpMLineIndex: 0 });
  assert.equal(peers[0].candidates.length, 0);
  await realtime.receive({ type: "offer", cameraId: "first", peerId: firstId, sdp: "offer-sdp" });
  assert.equal(peers[0].candidates.length, 1);
  assert.equal(sockets[0].sent.find(message => message.type === "answer").cameraId, "first");
  peers[0].onicecandidate({ candidate: { candidate: "candidate:browser", sdpMLineIndex: 0 } });
  assert.equal(sockets[0].sent.find(message => message.type === "ice").peerId, firstId);
  peers[0].ontrack({ streams: [{ id: "video-stream" }] });
  assert.equal(firstVideo.srcObject.id, "video-stream");
  await realtime.receive({ type: "detections", cameraId: "second", persons: [] });
  assert.equal(messages.at(-1).cameraId, "second");
  sockets[0].close();
  assert.ok(peers.slice(0, 2).every(peer => peer.closed));
  assert.equal(firstVideo.srcObject, null);
  assert.equal(messages.at(-1).type, "disconnected");
  timers[0]();
  sockets[1].open();
  assert.equal(peers.length, 4);
  assert.equal(sockets[1].sent.filter(message => message.type === "watch").length, 2);
  stopFirst();
  assert.ok(peers[2].closed);
  assert.equal(sockets[1].sent.find(message => message.type === "unwatch").cameraId, "first");
  realtime.close();
  assert.ok(peers[3].closed);
  assert.equal(secondVideo.srcObject, null);
  assert.equal(timers.length, 1);
});
