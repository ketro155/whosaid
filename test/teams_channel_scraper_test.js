// Stub-DOM test for the Teams channel scraper. Never touches real Teams.
// Run: node test/teams_channel_scraper_test.js [scraper.js]
// Optional argv[2] / TEAMS_CHANNEL_SCRAPER_PATH points at another scraper copy (RED proof).
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const scraperPath = process.argv[2] || process.env.TEAMS_CHANNEL_SCRAPER_PATH ||
  path.join(__dirname, '..', 'contrib', 'teams-scraper', 'teams-channel-scraper.js');
const source = fs.readFileSync(scraperPath, 'utf8');

const NOW = Date.now();
const H = 60 * 60 * 1000;
const D = 24 * H;
// Each message: mid (epoch), author header (null = same author as the one before), text.
// `hidden` replies show only in the thread view; the inline post shows the last 3.
const POSTS = [
  { root: [NOW - 5 * H, 'Alice', 'root one'], button: true, replies: [
    [NOW - 4 * H, 'Bob', 'hidden r1', true],
    [NOW - 4 * H + 1000, null, 'hidden r2, still Bob', true],
    [NOW - 3 * H, 'Carol', 'visible r3'],
    [NOW - 3 * H + 1000, null, 'visible r4, still Carol'],
    [NOW - 2 * H, 'Alice', 'visible r5'],
  ] },
  // An old root (outside the 7-day cutoff, inside the grace) with a fresh reply.
  { root: [NOW - 10 * D, 'Eve', 'old root'], replies: [[NOW - 1 * D, 'Dave', 'fresh reply']] },
  // Every visible message is old, so the hidden replies are older still: no expand.
  { root: [NOW - 20 * D, 'Gina', 'stale thread'], button: true, replies: [[NOW - 19 * D, 'Hal', 'old reply']] },
  // Older than cutoff + grace: the back-scroll stops here.
  { root: [NOW - 40 * D, 'Ivy', 'ancient'], replies: [] },
];

function buildWorld(initiallyPressed) {
  const clicks = [];
  const pressed = new Set(initiallyPressed);
  // Pills are exclusive: pressing one releases the others.
  const pills = ['Unread', 'Channels', 'Chats', 'Meeting chats', 'Communities'].map((label) => ({
    innerText: label,
    getAttribute: (a) => (a === 'aria-pressed' ? String(pressed.has(label)) : null),
    click() {
      clicks.push(label);
      if (pressed.has(label)) pressed.delete(label);
      else { pressed.clear(); pressed.add(label); }
    },
  }));
  let threadOpen = null;   // index of the post whose thread view is open
  const expandedPosts = [];
  const ids = new Map();   // element id -> stub
  const msgEl = (mid, chain) => ({ getAttribute: (a) => (a === 'data-mid' ? String(mid) : a === 'data-reply-chain-id' ? String(chain) : null) });
  function register(mid, author, text) {
    if (author) ids.set('author-' + mid, { innerText: author });
    ids.set('content-' + mid, { innerText: text, querySelectorAll: () => [] });
  }
  const postEls = POSTS.map((p, i) => {
    const chain = p.root[0];
    register(p.root[0], p.root[1], p.root[2]);
    p.replies.forEach(([mid, author, text]) => register(mid, author, text));
    const inline = [msgEl(chain, chain)].concat(p.replies.filter((r) => !r[3]).slice(-3).map((r) => msgEl(r[0], chain)));
    const all = [msgEl(chain, chain)].concat(p.replies.map((r) => msgEl(r[0], chain)));
    const button = p.button ? { click() { threadOpen = i; expandedPosts.push(i); } } : null;
    return {
      all,
      el: {
        querySelector(sel) {
          if (sel === '[data-tid="response-summary-button"]') return button;
          if (sel === '[data-mid][data-reply-chain-id]') return inline[0];
          return null;
        },
        querySelectorAll: (sel) => (sel === '[data-mid][data-reply-chain-id]' ? inline : []),
        inline,
      },
    };
  });
  const viewport = {
    scrollTop: 0, clientHeight: 500, scrollHeight: 0,
    querySelectorAll(sel) {
      if (sel === '[data-mid][data-reply-chain-id]') return postEls.flatMap((p) => p.el.inline);
      if (sel === '[data-tid="channel-pane-message"]') return postEls.map((p) => p.el);
      return [];
    },
  };
  const repliesViewport = {
    scrollTop: 0, clientHeight: 500,
    querySelectorAll: (sel) => (sel === '[data-mid][data-reply-chain-id]' && threadOpen !== null ? postEls[threadOpen].all : []),
  };
  const teamRow = { innerText: 'Team A\nGeneral' };
  const row = (testid) => ({
    innerText: 'General',
    getAttribute: (a) => (a === 'data-testid' ? testid : null),
    parentElement: { closest: () => teamRow },
    click() { clicks.push('open:' + testid); },
  });
  const rows = [row('sc-channel-list-item-19:abc@thread.tacv2'), row('favorite-channel-list-item-19:abc@thread.tacv2')];
  const document = {
    getElementById: (id) => ids.get(id) || null,
    querySelector(sel) {
      if (sel === '[data-tid="channel-pane-viewport"]') return threadOpen === null ? viewport : null;
      if (sel === '[data-tid="channel-replies-viewport"]') return threadOpen === null ? null : repliesViewport;
      if (sel === '[data-tid="close-l2-view-button"]') return threadOpen === null ? null : { click() { threadOpen = null; } };
      return null;
    },
    querySelectorAll(sel) {
      if (sel === 'button[aria-pressed]') return pills;
      if (sel === '[role="treeitem"][data-item-type="channel"]') return pressed.has('Channels') ? rows : [];
      return [];
    },
  };
  const window = { __TS_CUTOFF_DAYS: 7, __TS_PILL_SETTLE_MS: 1, __TS_CHANNEL_OPEN_MS: 1 };
  const ctx = vm.createContext({
    window, document, Date, Math, Number, Array, Set, Map, String, Infinity, Promise,
    setTimeout: (f) => setTimeout(f, 0),   // collapse all scraper waits
  });
  return { ctx, window, clicks, pressed, expandedPosts };
}

async function runScraper(initiallyPressed, globals = {}) {
  const w = buildWorld(initiallyPressed);
  Object.assign(w.window, globals);
  vm.runInContext(source, w.ctx);
  for (let i = 0; i < 5000 && !w.window.__tsc.done; i++) await new Promise((r) => setTimeout(r, 1));
  assert.ok(w.window.__tsc.done, 'scraper never finished');
  assert.strictEqual(w.window.__tsc.error, null, 'scraper errored: ' + w.window.__tsc.error);
  return w;
}

(async () => {
  const a = await runScraper(['Meeting chats']);
  const res = a.window.__tsc.result;
  // JSON compare: objects built inside the vm context have a different prototype.
  assert.strictEqual(JSON.stringify(Object.keys(res)), '["Team A > General"]', 'one channel, favorite copy skipped');
  const ch = res['Team A > General'];
  assert.strictEqual(ch.chat_id, '19:abc@thread.tacv2');
  assert.ok(ch.reachedCutoff, 'stops at the root older than cutoff + grace');
  const got = ch.messages.map((m) => m.author + ': ' + m.text.replace(/^\(reply to [^)]*\) /, ''));
  assert.strictEqual(JSON.stringify(got), JSON.stringify([   // chronological
    'Dave: fresh reply', 'Alice: root one', 'Bob: hidden r1', 'Bob: hidden r2, still Bob', 'Carol: visible r3',
    'Carol: visible r4, still Carol', 'Alice: visible r5',
  ]), 'hidden replies fetched, authors carried forward, old messages dropped; got ' + JSON.stringify(got));
  console.log('ok A: hidden replies expanded, authors carried, cutoff applied');

  assert.strictEqual(JSON.stringify(a.expandedPosts), '[0]', 'only the thread reaching inside the cutoff is expanded');
  assert.strictEqual(ch.expanded, 1);
  console.log('ok B: a thread whose visible messages are all old is not expanded');

  const dave = ch.messages.find((m) => m.author === 'Dave');
  assert.ok(dave.is_reply && dave.reply_to === 'Eve' && dave.thread_root_epoch === POSTS[1].root[0]);
  assert.ok(dave.text.startsWith('(reply to Eve, post of '), 'reply prefix names the root author');
  assert.ok(!ch.messages.find((m) => m.text === 'root one').is_reply, 'roots are not replies');
  console.log('ok C: reply metadata and prefix on a fresh reply to an old root');

  assert.ok(a.pressed.has('Meeting chats') && a.pressed.size === 1, 'user pill restored');
  assert.strictEqual(JSON.stringify(a.window.__tsc.pills.restored), '["Meeting chats"]');
  assert.strictEqual(JSON.stringify(a.clicks.filter((c) => !c.startsWith('open:'))),
    '["Meeting chats","Channels","Channels","Meeting chats"]', 'release, press Channels, then restore');
  console.log('ok D: Channels pill pressed for the run, user pill restored after');

  const b = await runScraper([], { __TS_REPLY_PREFIX: false, __TS_CHANNELS: ['nope'] });
  assert.strictEqual(Object.keys(b.window.__tsc.result).length, 0, '__TS_CHANNELS filters channels');
  assert.strictEqual(b.pressed.size, 0, 'no pill pressed before: none pressed after');
  const c = await runScraper([], { __TS_REPLY_PREFIX: false });
  assert.ok(c.window.__tsc.result['Team A > General'].messages.every((m) => !m.text.startsWith('(reply to')));
  console.log('ok E: channel filter, empty pill state, prefix off');
  console.log('PASS');
})().catch((e) => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
