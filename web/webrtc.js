"use strict";

// Reuse one connection for metadata and signaling for every visible camera.
// Video remains in RTCPeerConnection, never in WebSocket messages.
class ReTraceRealtime {
  constructor(path = "/ws", onMessage = () => {}, rtcConfig = { iceServers: [] }) {
    this.path = path;
    this.onMessage = onMessage;
    this.rtcConfig = rtcConfig;
    this.players = new Map();
    this.closed = false;
    this.counter = 0;
    this.connect();
  }

  connect() {
    const url = new URL(this.path, location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    this.socket = new WebSocket(url);
    this.socket.onopen = () => {
      this.subscribe();
      for (const player of this.players.values()) this.startPeer(player);
    };
    this.socket.onmessage = event => {
      this.receive(JSON.parse(event.data)).catch(() => {
        const message = JSON.parse(event.data);
        const player = this.players.get(message.peerId);
        if (player) player.onState("연결 실패 · 다시 재생을 눌러 주세요.");
      });
    };
    this.socket.onclose = () => {
      for (const player of this.players.values()) {
        this.stopPeer(player);
        player.onState("서버 재연결 중…");
      }
      this.onMessage({ type: "disconnected" });
      if (!this.closed) this.timer = setTimeout(() => this.connect(), 2000);
    };
  }

  send(message) {
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ version: 1, ...message }));
  }

  subscribe() {
    this.send({ type: "subscribe", cameraIds: [...new Set([...this.players.values()].map(player => player.cameraId))] });
  }

  watch(cameraId, video, onState = () => {}) {
    const peerId = `preview-${Date.now()}-${++this.counter}`;
    const player = { cameraId, peerId, video, onState, pc: null, ice: [] };
    this.players.set(peerId, player);
    this.subscribe();
    if (this.socket.readyState === WebSocket.OPEN) this.startPeer(player);
    return () => {
      this.send({ type: "unwatch", cameraId, peerId });
      this.stopPeer(player);
      this.players.delete(peerId);
      this.subscribe();
    };
  }

  startPeer(player) {
    this.stopPeer(player);
    // LAN/Tailscale: host candidates, no public STUN/TURN dependency.
    const pc = new RTCPeerConnection(this.rtcConfig);
    player.pc = pc;
    player.ice = [];
    pc.ontrack = event => {
      if (player.pc !== pc) return;
      player.video.srcObject = event.streams[0] || new MediaStream([event.track]);
      player.video.play().catch(() => player.onState("재생 버튼을 눌러 주세요."));
    };
    pc.onicecandidate = event => {
      if (player.pc !== pc || !event.candidate) return;
      this.send({ type: "ice", cameraId: player.cameraId, peerId: player.peerId,
        candidate: event.candidate.candidate, sdpMLineIndex: event.candidate.sdpMLineIndex });
    };
    pc.onconnectionstatechange = () => {
      if (player.pc !== pc) return;
      player.onState(pc.connectionState === "connected" ? "WebRTC 연결됨" : `WebRTC ${pc.connectionState}`);
    };
    player.onState("WebRTC 연결 중…");
    this.send({ type: "watch", cameraId: player.cameraId, peerId: player.peerId });
  }

  stopPeer(player) {
    if (player.pc) player.pc.close();
    player.pc = null;
    player.video.srcObject = null;
  }

  async receive(message) {
    const player = this.players.get(message.peerId);
    if (player && player.cameraId === message.cameraId && player.pc) {
      const pc = player.pc;
      if (message.type === "offer") {
        await pc.setRemoteDescription({ type: "offer", sdp: message.sdp });
        if (player.pc !== pc) return;
        for (const ice of player.ice.splice(0)) await pc.addIceCandidate(ice);
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        if (player.pc !== pc) return;
        this.send({ type: "answer", cameraId: player.cameraId, peerId: player.peerId, sdp: pc.localDescription.sdp });
      } else if (message.type === "ice") {
        const ice = { candidate: message.candidate, sdpMLineIndex: message.sdpMLineIndex };
        if (pc.remoteDescription) await pc.addIceCandidate(ice);
        else player.ice.push(ice);
      } else if (message.type === "error") {
        player.onState(`WebRTC 오류: ${message.code}`);
      }
    }
    this.onMessage(message);
  }

  close() {
    this.closed = true;
    clearTimeout(this.timer);
    for (const player of this.players.values()) this.stopPeer(player);
    this.players.clear();
    this.socket.close();
  }
}

window.ReTraceRealtime = ReTraceRealtime;
