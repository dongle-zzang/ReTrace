"use strict";

const players = [];
// Reserve browser HTTP connections for JSON polling; MJPEG responses stay open.
const pageSize = 4;
let streams = [];
let page = 0;
let realtime = null;

function updatePageStatus() {
  const pages = Math.max(1, Math.ceil(streams.length / pageSize));
  document.getElementById("page-status").textContent = `${streams.length}개 입력 · ${page + 1}/${pages} 페이지`;
  document.getElementById("previous-page").disabled = page === 0;
  document.getElementById("next-page").disabled = page + 1 >= pages;
}

function showPage(nextPage) {
  const pages = Math.max(1, Math.ceil(streams.length / pageSize));
  page = Math.max(0, Math.min(nextPage, pages - 1));
  players.splice(0).forEach(player => player.stop());
  document.getElementById("streams").replaceChildren();
  streams.slice(page * pageSize, (page + 1) * pageSize).forEach(stream => {
    if (stream.format === "webrtc") createWebrtcPlayer(stream);
    else createMjpegPlayer(stream);
  });
  updatePageStatus();
}

function createWebrtcPlayer(stream) {
  const card = document.createElement("article");
  const header = document.createElement("header");
  const title = document.createElement("h2");
  title.textContent = `${stream.name || stream.camera_id} · WebRTC`;
  const retry = document.createElement("button");
  retry.textContent = "다시 재생";
  const frame = document.createElement("div");
  frame.className = "video-frame";
  const video = document.createElement("video");
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;
  video.controls = true;
  const ns = "http://www.w3.org/2000/svg";
  const overlay = document.createElementNS(ns, "svg");
  overlay.setAttribute("viewBox", "0 0 1 1");
  overlay.setAttribute("preserveAspectRatio", "none");
  const status = document.createElement("p");
  status.className = "status";
  let transportStatus = "WebRTC 연결 중…";
  let cameraStatus = "";
  const renderStatus = () => { status.textContent = `${transportStatus}${cameraStatus ? ` · ${cameraStatus}` : ""}`; };
  let stop = () => {};
  let lastMetadata = 0;
  const clear = () => overlay.replaceChildren();
  const player = {
    id: stream.id, cameraId: stream.camera_id,
    update(_next, runtime) {
      if (runtime && !["online", "degraded"].includes(runtime.state)) clear();
    },
    metadata(message) {
      if (message.type === "camera_status") {
        cameraStatus = `${message.status.state} · ${message.status.fps.toFixed(1)} FPS`;
        renderStatus();
        if (!["online", "degraded"].includes(message.status.state)) clear();
      } else if (message.type === "detections") {
        lastMetadata = Date.now();
        clear();
        for (const person of message.persons) {
          const rect = document.createElementNS(ns, "rect");
          for (const [key, value] of Object.entries(person.bbox)) rect.setAttribute(key, value);
          rect.setAttribute("fill", "none");
          rect.setAttribute("stroke", "#32ff77");
          rect.setAttribute("stroke-width", "0.003");
          const text = document.createElementNS(ns, "text");
          text.setAttribute("x", person.bbox.x);
          text.setAttribute("y", Math.max(0.025, person.bbox.y - 0.008));
          text.setAttribute("fill", "#32ff77");
          text.setAttribute("font-size", "0.025");
          text.textContent = person.trackId === null ? "Person pending" : `Person ${person.trackId}`;
          overlay.append(rect, text);
        }
      }
    },
    stop() { clearInterval(expiry); stop(); clear(); }
  };
  const expiry = setInterval(() => { if (Date.now() - lastMetadata > 1000) clear(); }, 250);
  function start() {
    stop();
    clear();
    stop = realtime.watch(stream.camera_id, video, text => { transportStatus = text; renderStatus(); });
  }
  video.addEventListener("loadedmetadata", () => {
    if (video.videoWidth && video.videoHeight) frame.style.aspectRatio = `${video.videoWidth}/${video.videoHeight}`;
  });
  retry.addEventListener("click", start);
  header.append(title, retry);
  frame.append(video, overlay);
  card.append(header, frame, status);
  document.getElementById("streams").append(card);
  players.push(player);
  start();
}

function createMjpegPlayer(stream, container = document.getElementById("streams")) {
  const card = document.createElement("article");
  const header = document.createElement("header");
  const title = document.createElement("h2");
  title.textContent = `${stream.name || `source ${stream.id}`} · MJPEG`;
  const retry = document.createElement("button");
  retry.textContent = "다시 재생";
  const image = document.createElement("img");
  image.className = "mjpeg";
  image.alt = `source ${stream.id} 실시간 영상`;
  const status = document.createElement("p");
  status.className = "status";
  status.setAttribute("role", "status");
  header.append(title, retry);
  card.append(header, image, status);
  container.append(card);
  let timer = null;
  let sourceStatus = stream.status;
  let runtime = stream.runtime;
  let stopped = false;
  let loaded = false;
  function renderStatus() {
    const states = { connecting: "카메라 연결 중", reconnecting: "카메라 재연결 중",
      offline: "카메라 오프라인", degraded: "영상 수신 지연", online: "재생 중 (MJPEG)" };
    if (runtime && states[runtime.state]) {
      const fps = Number.isFinite(runtime.fps) ? ` · ${runtime.fps.toFixed(1)} FPS` : "";
      status.textContent = states[runtime.state] + fps;
    } else {
      status.textContent = loaded ? "재생 중 (MJPEG)" : "MJPEG 영상 연결 중…";
    }
  }
  function start() {
    clearTimeout(timer);
    image.removeAttribute("src");
    if (stopped) return;
    if (sourceStatus === "error" || sourceStatus === "eos") {
      status.textContent = "입력이 중단되었습니다. 서버의 source 번호 로그를 확인하세요.";
      return;
    }
    loaded = false;
    renderStatus();
    image.src = `${stream.url}?v=${Date.now()}`;
  }
  image.addEventListener("load", () => { loaded = true; renderStatus(); });
  image.addEventListener("error", () => {
    if (stopped || sourceStatus === "error" || sourceStatus === "eos") return;
    status.textContent = "영상 연결이 끊겼습니다. 다시 연결하는 중…";
    clearTimeout(timer);
    timer = setTimeout(start, 2000);
  });
  retry.addEventListener("click", start);
  players.push({
    id: stream.id,
    update(next, nextRuntime) {
      const changed = next !== sourceStatus;
      sourceStatus = next;
      runtime = nextRuntime;
      if (changed && (next === "error" || next === "eos" || !image.src)) start();
      else if (next !== "error" && next !== "eos") renderStatus();
    },
    stop() { stopped = true; clearTimeout(timer); image.removeAttribute("src"); }
  });
  start();
}

async function loadStreams() {
  const response = await fetch("/streams.json", { cache: "no-store" });
  if (!response.ok) throw new Error("preview server unavailable");
  return response.json();
}

loadStreams().then(initial => {
  streams = initial;
  const usesWebrtc = streams.some(stream => stream.format === "webrtc");
  if (usesWebrtc) realtime = new window.ReTraceRealtime("/ws", message => {
    if (message.type === "disconnected") {
      players.forEach(player => player.update("starting", { state: "reconnecting" }));
    } else {
      players.filter(player => player.cameraId === message.cameraId).forEach(player => player.metadata(message));
    }
  });
  document.getElementById("output-mode").textContent = usesWebrtc ? "현재 출력: WebRTC + WebSocket" : "현재 출력: MJPEG";
  showPage(0);
  document.getElementById("previous-page").addEventListener("click", () => showPage(page - 1));
  document.getElementById("next-page").addEventListener("click", () => showPage(page + 1));
  const poll = setInterval(async () => {
    try {
      const latest = await loadStreams();
      streams = latest;
      updatePageStatus();
      for (const stream of latest) {
        players.filter(item => item.id === stream.id).forEach(player => player.update(stream.status, stream.runtime));
      }
    } catch {
      document.getElementById("page-status").textContent = "미리보기 서버 연결을 확인하세요.";
    }
  }, 3000);
  window.addEventListener("pagehide", () => { clearInterval(poll); players.forEach(player => player.stop()); if (realtime) realtime.close(); });
}).catch(() => {
  document.getElementById("page-status").textContent = "입력 목록을 불러올 수 없습니다. 페이지를 새로고침하세요.";
});
