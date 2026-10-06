"use strict";

const players = [];

function createMjpegPlayer(stream, container = document.getElementById("streams")) {
  const card = document.createElement("article");
  const header = document.createElement("header");
  const title = document.createElement("h2");
  title.textContent = `source ${stream.id} · MJPEG`;
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
  let stopped = false;
  function start() {
    clearTimeout(timer);
    image.removeAttribute("src");
    if (stopped) return;
    if (sourceStatus === "error" || sourceStatus === "eos") {
      status.textContent = "입력이 중단되었습니다. 서버의 source 번호 로그를 확인하세요.";
      return;
    }
    status.textContent = "MJPEG 영상 연결 중…";
    image.src = `${stream.url}?v=${Date.now()}`;
  }
  image.addEventListener("load", () => { status.textContent = "재생 중 (MJPEG)"; });
  image.addEventListener("error", () => {
    if (stopped || sourceStatus === "error" || sourceStatus === "eos") return;
    status.textContent = "영상 연결이 끊겼습니다. 다시 연결하는 중…";
    clearTimeout(timer);
    timer = setTimeout(start, 2000);
  });
  retry.addEventListener("click", start);
  players.push({
    id: stream.id,
    update(next) {
      const changed = next !== sourceStatus;
      sourceStatus = next;
      if (changed && (next === "error" || next === "eos")) start();
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

loadStreams().then(streams => {
  document.getElementById("output-mode").textContent = "현재 출력: MJPEG";
  document.getElementById("page-status").textContent = `${streams.length}개 입력`;
  streams.forEach(stream => createMjpegPlayer(stream));
  const poll = setInterval(async () => {
    try {
      const latest = await loadStreams();
      document.getElementById("page-status").textContent = `${latest.length}개 입력`;
      for (const stream of latest) {
        players.filter(item => item.id === stream.id).forEach(player => player.update(stream.status));
      }
    } catch {
      document.getElementById("page-status").textContent = "미리보기 서버 연결을 확인하세요.";
    }
  }, 3000);
  window.addEventListener("pagehide", () => { clearInterval(poll); players.forEach(player => player.stop()); });
}).catch(() => {
  document.getElementById("page-status").textContent = "입력 목록을 불러올 수 없습니다. 페이지를 새로고침하세요.";
});
