// Stub-DOM test for the Teams scraper's inline image capture. Never touches real Teams.
// Run: node test/teams_scraper_images_test.js [scraper.js]
// Optional argv[2] / TEAMS_SCRAPER_PATH points at another scraper copy (RED proof).
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const scraperPath = process.argv[2] || process.env.TEAMS_SCRAPER_PATH ||
  path.join(__dirname, '..', 'contrib', 'teams-scraper', 'teams-chat-scraper.js');
const source = fs.readFileSync(scraperPath, 'utf8');

const NOW = Date.now();
const MIN = 60 * 1000;
const AMS = 'img[itemtype="http://schema.skype.com/AMSImage"]';

// msgs: [{ epoch, text, imgs: [{ id, w, h, loadAfter (harvest calls before it loads), bytes }] }]
function buildWorld(msgs, knobs = {}) {
  let harvests = 0;
  let open = false;
  const tree = { scrollTop: 0, clientHeight: 0, scrollHeight: 0 };
  const viewport = { scrollTop: 0, clientHeight: 0, scrollHeight: 0 };
  const row = {
    innerText: 'Chat A',
    getAttribute: (a) => (a === 'aria-label' ? 'Chat A' : null),
    querySelector: () => null,
    click() { open = true; },
    closest: () => tree,
  };
  function imgStub(i) {
    const loaded = harvests > (i.loadAfter || 0);
    const attrs = { alt: 'image', 'data-gallery-src': 'https://example.invalid/' + i.id + '/full' };
    return {
      complete: loaded, naturalWidth: loaded ? i.w : 0, naturalHeight: loaded ? i.h : 0,
      __bytes: i.bytes || 10, __id: i.id,
      getAttribute: (a) => (a in attrs ? attrs[a] : null),
    };
  }
  function paneItems() {
    if (!open) return [];
    harvests++;
    return msgs.map((m) => {
      const msg = { innerText: m.text, getAttribute: (a) => (a === 'data-mid' ? String(m.epoch) : null) };
      const content = {
        innerText: m.text,
        querySelectorAll: (sel) => (sel === AMS ? (m.imgs || []).map(imgStub) : []),
      };
      return {
        querySelector(sel) {
          if (sel === '[data-tid="chat-pane-message"]') return msg;
          if (sel === '[id^="author-"]') return { innerText: 'Author A' };
          if (sel === '[id^="content-"]') return content;
          return null;
        },
      };
    });
  }
  const document = {
    querySelector: (sel) => (sel === '[data-tid="message-pane-list-viewport"]' ? viewport : null),
    querySelectorAll(sel) {
      if (sel === '[role="treeitem"][data-item-type="chat"]') return [row];
      if (sel === '[data-tid="chat-pane-item"]') return paneItems();
      return [];
    },
    createElement(tag) {
      assert.strictEqual(tag, 'canvas');
      let drawn = null;
      return {
        width: 0, height: 0,
        getContext: () => ({ drawImage(img) { drawn = img; } }),
        toDataURL: (type) => 'data:' + type + ';base64,' + 'A'.repeat(drawn.__bytes) + '#' + drawn.__id,
      };
    },
  };
  const window = Object.assign({
    __TS_CUTOFF_DAYS: 7, __TS_PILL_SETTLE_MS: 1,
    __TS_RAIL_STABLE_MS: 3, __TS_RAIL_POLL_MS: 1, __TS_READY_FLOOR_MS: 0, __TS_READY_MAX_MS: 1000,
  }, knobs);
  const ctx = vm.createContext({
    window, document, Date, Math, Number, Array, Set, Map, String, Object, Infinity, Promise, JSON,
    setTimeout: (f) => setTimeout(f, 0),
  });
  return { ctx, window };
}

async function run(msgs, knobs) {
  const w = buildWorld(msgs, knobs);
  vm.runInContext(source, w.ctx);
  for (let i = 0; i < 5000 && !w.window.__ts.done; i++) await new Promise((r) => setTimeout(r, 1));
  assert.ok(w.window.__ts.done, 'scraper never finished');
  assert.strictEqual(w.window.__ts.error, null, 'scraper errored: ' + w.window.__ts.error);
  return JSON.parse(JSON.stringify(w.window.__ts.result['Chat A'].messages));
}

(async () => {
  const msgs = [
    { epoch: NOW - 5 * MIN, text: 'look at this', imgs: [{ id: 'one', w: 400, h: 300 }] },
    { epoch: NOW - 4 * MIN, text: '', imgs: [{ id: 'two', w: 10, h: 20 }, { id: 'late', w: 30, h: 40, loadAfter: 2 }] },
    { epoch: NOW - 3 * MIN, text: 'plain text' },
  ];

  // A: images captured as data URLs, an image-only message is kept, a lazy image fills in later.
  const a = await run(msgs);
  assert.strictEqual(a.length, 3, 'image-only message must be kept');
  assert.strictEqual(a[0].images.length, 1);
  assert.strictEqual(a[0].images[0].data, 'data:image/png;base64,' + 'A'.repeat(10) + '#one');
  assert.strictEqual(a[0].images[0].width, 400);
  assert.strictEqual(a[0].images[0].height, 300);
  assert.strictEqual(a[0].images[0].alt, 'image');
  assert.strictEqual(a[0].images[0].src, 'https://example.invalid/one/full');
  assert.strictEqual(a[1].text, '');
  assert.deepStrictEqual(a[1].images.map((i) => i.data.split('#')[1]), ['two', 'late'],
    'lazy image loaded on a later frame must be filled in');
  assert.ok(!('images' in a[2]), 'text-only message carries no images key');
  console.log('ok A: images captured, image-only kept, lazy image filled');

  // B: __TS_IMAGES = false keeps the old text-only behavior.
  const b = await run(msgs, { __TS_IMAGES: false });
  assert.strictEqual(b.length, 2, 'image capture off: image-only message dropped as before');
  assert.ok(b.every((m) => !('images' in m)), 'image capture off: no images key');
  console.log('ok B: __TS_IMAGES=false is text-only');

  // C: an image over __TS_IMAGE_MAX_CHARS keeps its metadata but no data.
  const c = await run([{ epoch: NOW - MIN, text: 'big', imgs: [{ id: 'big', w: 5, h: 5, bytes: 500 }] }],
    { __TS_IMAGE_MAX_CHARS: 100 });
  assert.strictEqual(c[0].images[0].data, null);
  assert.strictEqual(c[0].images[0].skipped, 'too-large');
  console.log('ok C: oversized image recorded without data');
})().catch((e) => { console.error(e); process.exit(1); });
