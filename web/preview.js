"use strict";

const players = [];
// Reserve browser HTTP connections for JSON polling; MJPEG responses stay open.
const pageSize = 4;
let streams = [];
let page = 0;

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
  streams.slice(page * pageSize, (page + 1) * pageSize).forEach(stream => createMjpegPlayer(stream));
  updatePageStatus();
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
  document.getElementById("output-mode").textContent = "현재 출력: MJPEG";
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
  window.addEventListener("pagehide", () => { clearInterval(poll); players.forEach(player => player.stop()); });
}).catch(() => {
  document.getElementById("page-status").textContent = "입력 목록을 불러올 수 없습니다. 페이지를 새로고침하세요.";
});
