/*
 * teams-transcript-scraper.js: harvest a Teams meeting transcript (the Recap
 * "Transcript" panel) into whosaid-ingestible JSON.
 *
 * WHERE TO RUN IT: NOT in the Teams page. The Recap view embeds the transcript in
 * a cross-origin SharePoint iframe (name "RecapxPlatIframe", URL
 * https://<tenant>-my.sharepoint.com/.../_layouts/15/xplatplugins.aspx?...). The
 * Teams top frame cannot read its document, so evaluate this file IN THAT IFRAME:
 * over CDP, attach to the iframe target (list targets with all/iframes on; drive
 * its targetId like a tab); by hand, pick the iframe in the console's context menu.
 *
 * WHY IT IS CAREFUL: the panel is a Fluent UI virtualized List. Of ~500 entries
 * only ~10 cells are in the DOM; the rest are blank placeholder pages, and
 * scrolling evicts cells as it mounts new ones. Reading the DOM once gets a slice.
 *
 *   primary  ("fiber"):  the React props of the List element hold the WHOLE
 *            transcript as `items` (speaker, offset, text, entry id). Read it with
 *            no scrolling and nothing changes on screen. The count is checked
 *            against the cells' aria-setsize.
 *   fallback ("scroll"): if React internals change, scroll the list container
 *            ~0.85 viewport per frame, harvest EVERY frame into a Map keyed by
 *            aria-posinset, then restore the original scrollTop.
 *
 * Same detached/poll shape as teams-chat-scraper.js: install window.__tt, kick
 * run() without awaiting it (a CDP Runtime.evaluate is capped at ~15 s), and poll
 * window.__tt.{done,error,progress} with short calls, then read window.__tt.result.
 *
 * Meeting title / time live in the Teams top frame, not here. Either set
 * window.__TT_MEETING = {title, start_iso, scheduled} in this frame before running,
 * or run TOP_FRAME_SNIPPET (below) in the Teams tab and pass its fields to
 * `whosaid teams ingest-transcript` (--title / --start).
 *
 * VERIFIED DOM CONTRACT (SharePoint xplat Recap, 2026-10-12):
 *   panel         : [aria-label="Transcript"] > #OneTranscript (fiber props: transcriptId, locale)
 *   scroller      : #scrollToTargetTargetedFocusZone (the overflow container)
 *   list          : .ms-List[role="list"]; fiber .return chain carries memoizedProps.items:
 *                   [{speakerDisplayName, speakerId, entryId, type: "Text"|"StartEvent"|"StopEvent",
 *                     timestamp: "PT1M27S", endTime, text, spokenLanguageTag}]
 *   cell          : .ms-List-cell[data-list-index] > #listItem-N
 *   speaker+time  : #entry-N[role="group"] aria-label "<Speaker> M minutes S seconds"
 *                   ("1 minute" when S is 0; a single space for meeting events). The
 *                   speaker avatar shows only on a speaker change, but the label
 *                   names the speaker on every entry
 *   text          : #sub-entry-N[role="listitem"][aria-setsize][aria-posinset]
 *
 * OUTPUT (window.__tt.result):
 *   { kind: "teams-transcript", method: "fiber"|"scroll", transcript_id, locale,
 *     recap_url, meeting: {...}|null, expected, entries: [
 *       { idx, entry_id, type, speaker, speaker_id, start_s, end_s, text, lang } ] }
 * Read-only: it never clicks, types, or saves.
 */

(function installTeamsTranscriptScraper() {
  const SETTLE_MS   = Number(window.__TT_SETTLE_MS ?? 450);   // wait after each scroll frame
  const STEP_FRAC   = 0.85;
  const MAX_FRAMES  = 2000;
  const FORCE       = window.__TT_METHOD || null;              // "fiber" | "scroll" to force one path

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // "PT1H2M3.5S" -> seconds (float), null when absent.
  function isoDur(v) {
    if (typeof v !== 'string') return null;
    const m = /^P(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$/.exec(v);
    if (!m) return null;
    return (Number(m[1] || 0) * 3600) + (Number(m[2] || 0) * 60) + Number(m[3] || 0);
  }

  // "Jane Doe 1 hour 2 minutes 3 seconds" -> {speaker, start_s}
  function parseEntryLabel(label) {
    const m = /^(.*?)\s*(?:(\d+)\s+hours?\s*)?(?:(\d+)\s+minutes?\s*)?(?:(\d+)\s+seconds?)?\s*$/.exec(label || '');
    if (!m || (m[2] == null && m[3] == null && m[4] == null)) return { speaker: (label || '').trim() || null, start_s: null };
    return { speaker: m[1].trim() || null,
             start_s: Number(m[2] || 0) * 3600 + Number(m[3] || 0) * 60 + Number(m[4] || 0) };
  }

  function fiberOf(el) {
    if (!el) return null;
    const k = Object.keys(el).find((x) => x.startsWith('__reactFiber$') || x.startsWith('__reactInternalInstance$'));
    return k ? el[k] : null;
  }

  // Walk up from a DOM node's fiber to the first memoizedProps that passes `pick`.
  function findProps(el, pick, maxDepth = 40) {
    let f = fiberOf(el);
    for (let d = 0; f && d < maxDepth; d++, f = f.return) {
      const p = f.memoizedProps;
      if (p && typeof p === 'object' && pick(p)) return p;
    }
    return null;
  }

  function expectedCount() {
    const el = document.querySelector('[id^="sub-entry-"][aria-setsize]');
    const n = el ? Number(el.getAttribute('aria-setsize')) : NaN;
    return Number.isFinite(n) && n > 0 ? n : null;
  }

  function transcriptMeta() {
    const root = document.getElementById('OneTranscript');
    const p = findProps(root, (x) => typeof x.transcriptId === 'string') || {};
    return { transcript_id: p.transcriptId || null, locale: p.locale || null };
  }

  function normItem(it, idx) {
    return {
      idx,
      entry_id: it.entryId || it.id || null,
      type: it.type || 'Text',
      speaker: it.speakerDisplayName || null,
      speaker_id: it.speakerId || null,
      start_s: isoDur(it.timestamp),
      end_s: isoDur(it.endTime),
      text: typeof it.text === 'string' ? it.text : '',
      lang: it.spokenLanguageTag || null,
    };
  }

  function readFiber() {
    const list = document.querySelector('#OneTranscript .ms-List, .ms-List[role="list"]');
    const p = findProps(list, (x) => Array.isArray(x.items) && x.items.length &&
      x.items.every((it) => it && typeof it === 'object' && ('text' in it || 'type' in it)));
    if (!p) return null;
    return p.items.map(normItem);
  }

  // Harvest the mounted cells into `store` (Map posinset -> entry). Returns NEW count.
  function harvestFrame(store, carry) {
    let added = 0;
    for (const cell of qsa('.ms-List-cell[data-list-index]')) {
      const listIdx = Number(cell.getAttribute('data-list-index'));
      const group = cell.querySelector('[id^="entry-"][role="group"]');
      const sub = cell.querySelector('[id^="sub-entry-"]');
      const pos = sub ? Number(sub.getAttribute('aria-posinset')) : NaN;
      const key = Number.isFinite(pos) ? pos : listIdx + 1;
      const label = group ? group.getAttribute('aria-label') || '' : '';
      const parsed = parseEntryLabel(label);
      const isEvent = !label.trim() || !!cell.querySelector('[class*="meetingEvent"]');
      if (parsed.speaker) carry.speaker = parsed.speaker;
      if (store.has(key)) continue;
      const text = (sub ? sub.innerText : cell.innerText || '').trim();
      store.set(key, {
        idx: key - 1, entry_id: null,
        type: isEvent ? 'Event' : 'Text',
        speaker: isEvent ? null : (parsed.speaker || carry.speaker || null),
        speaker_id: null, start_s: parsed.start_s, end_s: null, text,
      });
      added++;
    }
    return added;
  }

  async function readScroll(tt) {
    const sc = document.getElementById('scrollToTargetTargetedFocusZone') ||
      document.querySelector('#OneTranscript [data-is-scrollable="true"]');
    if (!sc) throw new Error('transcript scroller not found');
    const saved = sc.scrollTop;
    const store = new Map();
    const carry = { speaker: null };
    try {
      sc.scrollTop = 0;
      await sleep(SETTLE_MS);
      let still = 0;
      for (let f = 0; f < MAX_FRAMES; f++) {
        harvestFrame(store, carry);
        tt.progress = { phase: 'scroll', frame: f, harvested: store.size, expected: tt.result.expected };
        const before = sc.scrollTop;
        sc.scrollTop = Math.min(before + sc.clientHeight * STEP_FRAC, sc.scrollHeight);
        await sleep(SETTLE_MS);
        if (sc.scrollTop === before && ++still >= 3) break;
        if (sc.scrollTop !== before) still = 0;
      }
      harvestFrame(store, carry);
    } finally {
      sc.scrollTop = saved;                    // leave the panel where the user had it
    }
    return Array.from(store.values()).sort((a, b) => a.idx - b.idx);
  }

  async function run() {
    const tt = window.__tt;
    tt.done = false;
    tt.error = null;
    tt.result = { kind: 'teams-transcript', method: null, ...transcriptMeta(),
                  recap_url: location.href, meeting: window.__TT_MEETING || null,
                  expected: expectedCount(), entries: [] };
    try {
      let entries = null;
      if (FORCE !== 'scroll') {
        tt.progress = { phase: 'fiber' };
        entries = readFiber();
        if (entries) tt.result.method = 'fiber';
      }
      if (!entries && FORCE !== 'fiber') {
        entries = await readScroll(tt);
        tt.result.method = 'scroll';
      }
      if (!entries) throw new Error('no transcript entries found (is the Transcript panel open?)');
      tt.result.entries = entries;
      if (!tt.result.locale) tt.result.locale = (entries.find((x) => x.lang) || {}).lang || null;
      const exp = tt.result.expected;
      // aria-setsize counts the same rows the List renders, events included.
      if (exp != null && exp !== entries.length) {
        tt.result.warning = `entry count ${entries.length} != aria-setsize ${exp}`;
      }
      tt.progress = { phase: 'done', entries: entries.length, expected: exp, method: tt.result.method };
    } catch (e) {
      tt.error = String(e && e.stack || e);
    }
    tt.done = true;
  }

  window.__tt = { done: false, error: null, result: null, progress: {}, run };
  window.__tt.run();          // detached: driver polls window.__tt.{done,progress,error}
  return 'teams transcript scraper installed; poll window.__tt.done / window.__tt.progress';
})();

/*
 * TOP_FRAME_SNIPPET: run in the Teams tab (top frame) to get the meeting title and
 * scheduled slot from the Recap header. Read-only. The browser parses the slot in
 * its own time zone, so start_iso is that zone's wall clock as UTC.
 *
 *   (() => {
 *     const t = (document.querySelector('[data-tid="intelligent-recap-header"]') ||
 *                document.body).innerText;
 *     const m = /(\w+day),\s+(\w+ \d{1,2}, \d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)/.exec(t);
 *     const title = (document.title.split('|')[1] || '').trim() || null;
 *     const start = m ? new Date(m[2] + ' ' + m[3]) : null;
 *     return JSON.stringify({ title, scheduled: m ? m[0] : null,
 *       start_iso: start && !isNaN(start) ? start.toISOString() : null });
 *   })()
 *
 * DRIVER:
 *   1. (optional) in the iframe: window.__TT_MEETING = <TOP_FRAME_SNIPPET result>
 *   2. evaluate this file in the Recap iframe target
 *   3. poll JSON.stringify({done: window.__tt.done, error: window.__tt.error, progress: window.__tt.progress})
 *   4. save JSON.stringify(window.__tt.result) to transcript.json
 *   5. whosaid teams ingest-transcript transcript.json --into WS [--start ISO] [--title T]
 */
