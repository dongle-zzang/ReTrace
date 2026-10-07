"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("25 sources play four at a time, recover, release on page change and close on exit", async () => {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; if (name === "src") this.src = undefined; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }
  const elements = {};
  const document = {
    createElement: tag => new Element(tag),
    getElementById: id => elements[id] || (elements[id] = new Element(id))
  };
  const window = { listeners: {}, addEventListener(name, cb) { this.listeners[name] = cb; } };
  const sources = Array.from({length: 25}, (_, id) => ({ id, format: "mjpeg", url: `/mjpeg/source${id}`, status: "connected" }));
  let poll;
  const requests = [];
  const context = vm.createContext({
    document, window, Date, console,
    fetch: async url => { requests.push(url); return { ok: true, json: async () => sources }; },
    setTimeout: () => 1, clearTimeout() {},
    setInterval(callback) { poll = callback; return 1; }, clearInterval() {},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/preview.js"), "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements["output-mode"].textContent, "현재 출력: MJPEG");
  const cards = elements.streams.children;
  assert.equal(cards.length, 4);
  cards.forEach((card, id) => {
    assert.equal(card.children[1].tag, "img");
    assert.match(card.children[1].src, new RegExp(`^/mjpeg/source${id}\\?v=`));
  });
  sources[0].status = "error";
  await poll();
  assert.equal(cards[0].children[1].src, undefined);
  assert.match(cards[0].children[2].textContent, /중단/);
  assert.match(cards[1].children[1].src, /^\/mjpeg\/source1/);
  assert.deepEqual(requests, ["/streams.json", "/streams.json"]);
  sources[0].status = "connected";
  sources[0].runtime = { state: "online", fps: 12.5 };
  await poll();
  assert.match(cards[0].children[1].src, /^\/mjpeg\/source0/);
  assert.match(cards[0].children[2].textContent, /12\.5 FPS/);
  const playingUrl = cards[0].children[1].src;
  sources[0].status = "starting";
  sources[0].runtime = { state: "reconnecting", fps: 0 };
  await poll();
  assert.match(cards[0].children[2].textContent, /재연결/);
  assert.equal(cards[0].children[1].src, playingUrl);
  elements["next-page"].listeners.click();
  cards.forEach(card => assert.equal(card.children[1].src, undefined));
  assert.equal(elements.streams.children.length, 4);
  elements.streams.children.forEach((card, index) => {
    assert.match(card.children[1].src, new RegExp(`^/mjpeg/source${index + 4}\\?v=`));
  });
  assert.match(elements["page-status"].textContent, /25개 입력 · 2\/7 페이지/);
  elements["previous-page"].listeners.click();
  assert.match(elements.streams.children[0].children[1].src, /^\/mjpeg\/source0/);
  window.listeners.pagehide();
  elements.streams.children.forEach(card => assert.equal(card.children[1].src, undefined));
});
