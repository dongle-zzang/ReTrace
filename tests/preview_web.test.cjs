"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("all sources use MJPEG, stop independently, and close on page exit", async () => {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; }
    append(...children) { this.children.push(...children); }
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
  const sources = [0, 1].map(id => ({ id, format: "mjpeg", url: `/mjpeg/source${id}`, status: "connected" }));
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
  assert.equal(cards.length, 2);
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
  window.listeners.pagehide();
  cards.forEach(card => assert.equal(card.children[1].src, undefined));
});
