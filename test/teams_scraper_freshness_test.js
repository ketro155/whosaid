// Stub-DOM test for the Teams scraper's freshness guards (issue #86): rail order
// readiness and the per-thread row-time check. Never touches real Teams.
// Run: node test/teams_scraper_freshness_test.js [scraper.js]
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

// chats: [{ name, rowAt (epoch|null), gens: [[epoch...] per open; last one repeats] }]
// orders: rail orders over time; the rail shows orders[i] until flipEvery*(i+1) samples.
function buildWorld({ chats, orders, flipEvery = Infinity, knobs = {} }) {
  const clicks = {};
  let open = null;
  let railCalls = 0;
  const byName = Object.fromEntries(chats.map((c) => [c.name, c]));
  const tree = { scrollTop: 0, clientHeight: 0, scrollHeight: 0 };
  const viewport = { scrollTop: 0, clientHeight: 0, scrollHeight: 0 };
  function currentOrder() {
    const i = Math.min(Math.floor(railCalls / flipEvery), orders.length - 1);
    railCalls++;
    return orders[i];
  }
  function row(c) {
    return {
      innerText: c.name,
      getAttribute: (a) => (a === 'aria-label' ? c.name : null),
      querySelector: (sel) => (sel === 'time[datetime]' && c.rowAt !== null
        ? { getAttribute: (a) => (a === 'datetime' ? new Date(c.rowAt).toISOString() : null) } : null),
      click() { clicks[c.name] = (clicks[c.name] || 0) + 1; open = c.name; },
      closest: () => tree,
    };
  }
  function paneItems() {
    if (!open) return [];
    const c = byName[open];
    const epochs = c.gens[Math.min(clicks[open] - 1, c.gens.length - 1)];
    return epochs.map((e) => {
      const blank = e < 0;                      // negative epoch = text-less (attachment-only) message
      e = Math.abs(e);
      const msg = { innerText: blank ? '' : 'm' + e, getAttribute: (a) => (a === 'data-mid' ? String(e) : null) };
      return {
        querySelector(sel) {
          if (sel === '[data-tid="chat-pane-message"]') return msg;
          if (sel === '[id^="author-"]') return { innerText: 'Author A' };
          if (sel === '[id^="content-"]') return { innerText: blank ? '' : 'message at ' + e };
          return null;
        },
      };
    });
  }
  const document = {
    querySelector: (sel) => (sel === '[data-tid="message-pane-list-viewport"]' ? viewport : null),
    querySelectorAll(sel) {
      if (sel === '[role="treeitem"][data-item-type="chat"]') return currentOrder().map((n) => row(byName[n]));
      if (sel === '[data-tid="chat-pane-item"]') return paneItems();
      return [];
    },
  };
  const window = Object.assign({
    __TS_CUTOFF_DAYS: 7, __TS_PILL_SETTLE_MS: 1,
    __TS_RAIL_STABLE_MS: 3, __TS_RAIL_POLL_MS: 1, __TS_READY_FLOOR_MS: 0, __TS_READY_MAX_MS: 1000,
    __TS_FRESH_TOLERANCE_MS: MIN, __TS_REFRESH_WAIT_MS: 1,
  }, knobs);
  const ctx = vm.createContext({
    window, document, Date, Math, Number, Array, Set, Map, String, Object, Infinity, Promise,
    setTimeout: (f) => setTimeout(f, 0),   // collapse all scraper waits
  });
  return { ctx, window, clicks };
}

async function run(opts) {
  const w = buildWorld(opts);
  vm.runInContext(source, w.ctx);
  for (let i = 0; i < 5000 && !w.window.__ts.done; i++) await new Promise((r) => setTimeout(r, 1));
  assert.ok(w.window.__ts.done, 'scraper never finished');
  assert.strictEqual(w.window.__ts.error, null, 'scraper errored: ' + w.window.__ts.error);
  return w;
}

const simple = (name) => ({ name, rowAt: null, gens: [[NOW - 10 * MIN]] });

(async () => {
  // A (control): rail order stable from the first sample -> no order change, minimal polls.
  const a = await run({ chats: [simple('Alpha'), simple('Bravo')], orders: [['Alpha', 'Bravo']] });
  const ra = a.window.__ts.readiness;
  assert.strictEqual(ra.orderChanges, 0, 'stable rail: no order changes');
  assert.strictEqual(ra.gaveUp, false);
  assert.strictEqual(JSON.stringify(Object.keys(a.window.__ts.result)), '["Alpha","Bravo"]');
  console.log('ok A: stable rail -> ready after ' + ra.polls + ' polls, no order change');

  // B: cached (stale) rail order re-sorts when the sync lands -> readiness waits for it,
  //    and chats are enumerated in the FRESH order.
  const b = await run({
    chats: [simple('Alpha'), simple('Bravo')],
    orders: [['Bravo', 'Alpha'], ['Alpha', 'Bravo']], flipEvery: 3,
  });
  const rb = b.window.__ts.readiness;
  assert.strictEqual(rb.orderChanges, 1, 'stale-then-fresh rail: one order change seen');
  assert.ok(rb.polls > ra.polls, 'readiness delayed past the re-sort (' + rb.polls + ' vs ' + ra.polls + ')');
  assert.strictEqual(rb.gaveUp, false);
  assert.strictEqual(JSON.stringify(Object.keys(b.window.__ts.result)), '["Alpha","Bravo"]',
    'enumeration must follow the fresh rail order');
  assert.ok(!b.window.__ts.progress.warning, 'settled rail: no warning');
  console.log('ok B: stale-then-fresh rail order delays readiness (' + rb.polls + ' polls) and fresh order is used');

  // C: rail never settles -> gives up at the ceiling and warns instead of hanging.
  const c = await run({
    chats: [simple('Alpha'), simple('Bravo')],
    orders: Array.from({ length: 400 }, (_, i) => (i % 2 ? ['Alpha', 'Bravo'] : ['Bravo', 'Alpha'])),
    flipEvery: 1, knobs: { __TS_READY_MAX_MS: 20 },
  });
  assert.strictEqual(c.window.__ts.readiness.gaveUp, true, 'unsettled rail: gaveUp');
  assert.ok(/never settled/.test(c.window.__ts.progress.warning || ''), 'unsettled rail warns, got: ' +
    c.window.__ts.progress.warning);
  console.log('ok C: rail that never settles -> gaveUp + warning');

  // C2: tab-age floor holds readiness even when the rail is stable (no performance API -> age 0).
  const c2 = await run({ chats: [simple('Alpha')], orders: [['Alpha']], knobs: { __TS_READY_FLOOR_MS: 12 } });
  assert.ok(c2.window.__ts.readiness.waitedMs >= 12, 'floor respected, waited ' + c2.window.__ts.readiness.waitedMs);
  console.log('ok C2: tab-age floor delays readiness on a stable rail');

  // D: per-thread freshness check.
  const chats = [
    // Fresh: newest message within tolerance of the row time -> one open, no re-harvest.
    { name: 'Fresh', rowAt: NOW - 10 * MIN + 30 * 1000, gens: [[NOW - 20 * MIN, NOW - 10 * MIN]] },
    // Lagging: first open shows the cached thread, the second has the new message.
    { name: 'Lagging', rowAt: NOW - 5 * MIN, gens: [[NOW - 30 * MIN], [NOW - 30 * MIN, NOW - 5 * MIN]] },
    // Stuck: still older than the row after the re-harvest -> warning.
    { name: 'Stuck', rowAt: NOW - 5 * MIN, gens: [[NOW - 60 * MIN]] },
    // Attach: newest item is text-less (attachment-only) but matches the row -> not stale.
    { name: 'Attach', rowAt: NOW - 3 * MIN, gens: [[NOW - 20 * MIN, -(NOW - 3 * MIN)]] },
    // NoTime: row has no machine-readable time -> check skipped.
    { name: 'NoTime', rowAt: null, gens: [[NOW - 60 * MIN]] },
  ];
  const d = await run({ chats, orders: [chats.map((x) => x.name)] });
  const ts = d.window.__ts;
  assert.strictEqual(d.clicks.Fresh, 1, 'fresh thread must not be re-harvested');
  assert.strictEqual(ts.result.Fresh.freshness.reharvested, false);
  assert.strictEqual(d.clicks.NoTime, 1, 'no row time: no re-harvest');
  assert.strictEqual(d.clicks.Attach, 1, 'text-less newest message still counts as fresh');
  assert.strictEqual(d.clicks.Lagging, 2, 'stale thread re-harvested exactly once');
  assert.strictEqual(ts.result.Lagging.freshness.reharvested, true);
  assert.strictEqual(ts.result.Lagging.kept, 2, 'missed message recovered by the re-harvest');
  assert.strictEqual(ts.result.Lagging.newest, NOW - 5 * MIN);
  assert.strictEqual(d.clicks.Stuck, 2, 'persistent mismatch: still only one re-harvest');
  assert.strictEqual(JSON.stringify(ts.stale), '["Stuck"]', 'only the stuck chat is flagged stale');
  const warning = ts.progress.warning || '';
  assert.ok(/"Stuck" looks stale/.test(warning), 'stuck chat warns, got: ' + warning);
  assert.ok(!/Lagging|Fresh|NoTime|Attach/.test(warning), 'recovered/fresh chats must not warn, got: ' + warning);
  console.log('ok D: fresh not re-harvested; lagging re-harvested once and recovered; stuck warns');
  console.log('PASS');
})().catch((e) => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
