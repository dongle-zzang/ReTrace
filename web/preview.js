"use strict";

// One WebSocket carries every camera's latest JPEG and status, so the browser's
// per-origin HTTP connection limit (6 for HTTP/1.1) no longer caps the camera count.
const VIDEO_FPS = 10;
const STATES = { connecting: "카메라 연결 중", reconnecting: "카메라 재연결 중",
  offline: "카메라 오프라인", degraded: "영상 수신 지연", online: "재생 중" };
const cards = new Map();
let socket = null;
let closed = false;
let reconnectTimer = null;

// Binary frame: version u8, cameraId length u8, cameraId UTF-8, sequence u32 BE, JPEG.
function parseFrame(buffer) {
  const bytes = new Uint8Array(buffer);
  if (bytes.length < 6 || bytes[0] !== 1 || bytes.length < 6 + bytes[1]) return null;
  const length = bytes[1];
  return { cameraId: new TextDecoder().decode(bytes.subarray(2, 2 + length)), jpeg: bytes.subarray(6 + length) };
}

function renderStatus(card) {
  const runtime = card.runtime;
  let text = "카메라 상태 확인 중";
  if (runtime && STATES[runtime.state]) {
    text = STATES[runtime.state] + (Number.isFinite(runtime.fps) ? ` · ${runtime.fps.toFixed(1)} FPS` : "");
  }
  if (!card.connected) text += " · 서버 연결 중…";
  else if (!card.shown) text += " · 영상 대기 중…";
  card.status.textContent = text;
}

function createCard(stream) {
  const article = document.createElement("article");
  const header = document.createElement("header");
  const title = document.createElement("h2");
  title.textContent = stream.name || stream.camera_id;
  const image = document.createElement("img");
  image.className = "mjpeg";
  image.alt = `${stream.name || stream.camera_id} 실시간 영상`;
  const status = document.createElement("p");
  status.className = "status";
  status.setAttribute("role", "status");
  header.append(title);
  article.append(header, image, status);
  document.getElementById("streams").append(article);
  const card = { image, status, runtime: stream.runtime, connected: false, shown: false,
    url: null, next: null, loading: false };
  cards.set(stream.camera_id, card);
  renderStatus(card);
}

// Decode one JPEG at a time per camera; frames arriving meanwhile replace each other.
function showFrame(card, blob) {
  card.next = blob;
  if (!card.loading) loadNext(card);
}

function loadNext(card) {
  const blob = card.next;
  card.next = null;
  if (!blob || closed) {
    card.loading = false;
    return;
  }
  card.loading = true;
  const previous = card.url;
  card.url = URL.createObjectURL(blob);
  card.image.onload = card.image.onerror = () => {
    if (previous) URL.revokeObjectURL(previous);
    if (!card.shown) {
      card.shown = true;
      renderStatus(card);
    }
    loadNext(card);
  };
  card.image.src = card.url;
}

function connect() {
  const url = new URL("/ws", location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  socket.onopen = () => {
    socket.send(JSON.stringify({ version: 1, type: "subscribe", cameraIds: [...cards.keys()],
      video: true, videoFps: VIDEO_FPS }));
    cards.forEach(card => { card.connected = true; renderStatus(card); });
  };
  socket.onmessage = event => {
    if (typeof event.data !== "string") {
      const frame = parseFrame(event.data);
      const card = frame && cards.get(frame.cameraId);
      if (card) showFrame(card, new Blob([frame.jpeg], { type: "image/jpeg" }));
      return;
    }
    const message = JSON.parse(event.data);
    const card = cards.get(message.cameraId);
    if (card && message.type === "camera_status") {
      card.runtime = message.status;
      renderStatus(card);
    }
  };
  socket.onclose = () => {
    cards.forEach(card => { card.connected = false; renderStatus(card); });
    if (!closed) reconnectTimer = setTimeout(connect, 2000);
  };
}

fetch("/streams.json", { cache: "no-store" }).then(response => {
  if (!response.ok) throw new Error("preview server unavailable");
  return response.json();
}).then(streams => {
  streams.forEach(createCard);
  document.getElementById("page-status").textContent = `${streams.length}개 입력`;
  connect();
  window.addEventListener("pagehide", () => {
    closed = true;
    clearTimeout(reconnectTimer);
    if (socket) socket.close();
    cards.forEach(card => {
      if (card.url) URL.revokeObjectURL(card.url);
      card.image.removeAttribute("src");
    });
  });
}).catch(() => {
  document.getElementById("page-status").textContent = "입력 목록을 불러올 수 없습니다. 페이지를 새로고침하세요.";
});
