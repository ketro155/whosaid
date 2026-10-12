// Stub-DOM test for contrib/teams-scraper/teams-transcript-scraper.js. Never touches
// real Teams. Simulates the Recap panel's virtualized Fluent List: only a window of
// cells around scrollTop is mounted, so a one-shot DOM read would see a slice.
// Checks the fiber path (all items, no scrolling) and the scroll fallback (every
// entry harvested across frames, events typed, scrollTop restored).
// Run: node test/teams_transcript_scraper_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'contrib', 'teams-scraper', 'teams-transcript-scraper.js'), 'utf8');

const ROW = 30;       // px per cell
const WINDOW = 6;     // cells mounted at once
const N = 40;
const SPEAKERS = ['Doe, Jane', 'Roe Richard', 'Self Example'];

function dur(s) {
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return 'PT' + (h ? h + 'H' : '') + (m ? m + 'M' : '') + (sec || (!h && !m) ? sec + 'S' : '');
}
function label(name, s) {
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  const parts = [];
  if (h) parts.push(h + (h === 1 ? ' hour' : ' hours'));
  parts.push(m + (m === 1 ? ' minute' : ' minutes'));
  if (sec || (!h && !m)) parts.push(sec + (sec === 1 ? ' second' : ' seconds'));
  return name + ' ' + parts.join(' ');
}

const ITEMS = Array.from({ length: N }, (_, i) => {
  const start = i === N - 2 ? 3725 : i === 5 ? 60 : 3 + i * 7;   // 1 h 2 min 5 s; "1 minute"
  const type = i === 0 ? 'StartEvent' : i === N - 1 ? 'StopEvent' : 'Text';
  return {
    speakerDisplayName: SPEAKERS[i % 3], speakerId: 'aad-' + (i % 3), entryId: 'tx/' + (i + 5),
    type, timestamp: dur(start), endTime: type === 'Text' ? dur(start + 2) : undefined,
    text: type === 'Text' ? 'line ' + i + ' said here' : '', spokenLanguageTag: 'en-us',
  };
});

function buildWorld({ fiber }) {
  const scroller = {
    clientHeight: ROW * 3, scrollHeight: ROW * N, _top: 250, writes: 0,
    get scrollTop() { return this._top; },
    set scrollTop(v) { this.writes++; this._top = Math.max(0, Math.min(v, this.scrollHeight - this.clientHeight)); },
  };
  function cell(i) {
    const it = ITEMS[i];
    const isText = it.type === 'Text';
    const startS = i === N - 2 ? 3725 : i === 5 ? 60 : 3 + i * 7;
    const sub = isText ? {
      innerText: ' ' + it.text + ' ',
      getAttribute: (a) => (a === 'aria-posinset' ? String(i + 1) : a === 'aria-setsize' ? String(N) : null),
    } : null;
    const group = { getAttribute: (a) => (a === 'aria-label' ? (isText ? label(it.speakerDisplayName, startS) : ' ') : null) };
    return {
      innerText: isText ? it.text : 'Meeting event',
      getAttribute: (a) => (a === 'data-list-index' ? String(i) : null),
      querySelector(sel) {
        if (sel === '[id^="entry-"][role="group"]') return group;
        if (sel === '[id^="sub-entry-"]') return sub;
        // only the start event carries the class; the stop event is caught by its blank label
        if (sel === '[class*="meetingEvent"]') return it.type === 'StartEvent' ? {} : null;
        return null;
      },
    };
  }
  function mounted() {
    const first = Math.floor(scroller.scrollTop / ROW);
    const out = [];
    for (let i = first; i < Math.min(N, first + WINDOW); i++) out.push(cell(i));
    return out;
  }
  const list = { '__reactFiber$test': { memoizedProps: { role: 'list' },
    return: { memoizedProps: { className: 'x' },
      return: { memoizedProps: fiber ? { items: ITEMS } : { onRenderCell() {} }, return: null } } } };
  const root = { '__reactFiber$test': { memoizedProps: { transcriptId: 'tid-123', locale: 'en-us' }, return: null } };
  const document = {
    getElementById: (id) => (id === 'OneTranscript' ? root : id === 'scrollToTargetTargetedFocusZone' ? scroller : null),
    querySelector(sel) {
      if (sel === '#OneTranscript .ms-List, .ms-List[role="list"]') return list;
      if (sel === '[id^="sub-entry-"][aria-setsize]') return mounted().map((c) => c.querySelector('[id^="sub-entry-"]')).find(Boolean) || null;
      return null;
    },
    querySelectorAll: (sel) => (sel === '.ms-List-cell[data-list-index]' ? mounted() : []),
  };
  const window = { __TT_SETTLE_MS: 0 };
  const ctx = vm.createContext({
    window, document, location: { href: 'https://example.sharepoint.com/recap' },
    Object, Array, Map, Set, Number, String, Math, Promise, Error, JSON,
    setTimeout: (f) => setTimeout(f, 0),
  });
  return { ctx, window, scroller };
}

async function run(opts) {
  const w = buildWorld(opts);
  vm.runInContext(source, w.ctx);
  for (let i = 0; i < 5000 && !w.window.__tt.done; i++) await new Promise((r) => setTimeout(r, 1));
  assert.ok(w.window.__tt.done, 'scraper finished');
  assert.strictEqual(w.window.__tt.error, null, String(w.window.__tt.error));
  return w;
}

(async () => {
  let checks = 0;
  const ok = (c, m) => { assert.ok(c, m); checks++; };

  // Fiber path: the whole list, no scroll writes at all.
  const f = await run({ fiber: true });
  const fr = f.window.__tt.result;
  ok(fr.method === 'fiber', 'fiber method');
  ok(fr.entries.length === N && fr.expected === N && !fr.warning, 'fiber count == aria-setsize');
  ok(fr.transcript_id === 'tid-123' && fr.locale === 'en-us', 'transcript id and locale');
  ok(f.scroller.writes === 0 && f.scroller.scrollTop === 250, 'fiber path never scrolls');
  ok(fr.entries[0].type === 'StartEvent' && fr.entries[N - 1].type === 'StopEvent', 'events kept with type');
  ok(fr.entries[N - 2].start_s === 3725 && fr.entries[5].start_s === 60, 'ISO 8601 durations parsed');
  ok(fr.entries[1].entry_id === 'tx/6' && fr.entries[1].speaker === SPEAKERS[1], 'entry id and speaker');

  // Scroll fallback: React props without items, so it must walk the virtualized list.
  const s = await run({ fiber: false });
  const sr = s.window.__tt.result;
  ok(sr.method === 'scroll', 'scroll method');
  ok(sr.entries.length === N && !sr.warning, `scroll harvested all ${N} (got ${sr.entries.length})`);
  ok(s.scroller.scrollTop === 250 && s.scroller.writes > 3, 'scrollTop restored after harvesting');
  for (const e of sr.entries) {
    const src = ITEMS[e.idx];
    if (src.type === 'Text') {
      ok(e.type === 'Text' && e.speaker === src.speakerDisplayName && e.text === src.text, `text entry ${e.idx}`);
    } else {
      ok(e.type === 'Event' && e.speaker === null, `event entry ${e.idx}`);
    }
  }
  ok(sr.entries[N - 2].start_s === 3725 && sr.entries[5].start_s === 60, 'hour and "1 minute" labels parsed');
  ok(sr.entries.map((e) => e.idx).join() === ITEMS.map((_, i) => i).join(), 'scroll entries in order, no gaps');

  console.log(`teams_transcript_scraper_test: OK (${checks} checks)`);
})().catch((e) => { console.error(e); process.exit(1); });
