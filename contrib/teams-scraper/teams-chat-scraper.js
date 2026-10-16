/*
 * teams-chat-scraper.js — harvest MS Teams web chat history into whosaid-ingestible JSON.
 *
 * Runs ENTIRELY in the Teams web tab (teams.cloud.microsoft) on an already
 * authenticated session. Driven over Chrome DevTools Protocol (CDP), but the
 * logic below is plain page JS — paste it into the tab console to try by hand.
 *
 * WHY THE DETACHED/POLL SHAPE: a single CDP Runtime.evaluate call is capped at
 * ~15 s, but a full multi-chat back-scroll runs minutes. So we DO NOT await the
 * scrape in one call. Instead we install window.__ts, kick run() as a detached
 * promise that stashes progress on window.__ts, and the driver POLLS
 * window.__ts.{done,progress,result} with short calls until done. One long
 * awaited call would time out and lose the return.
 *
 * OUTPUT (window.__ts.result): raw_by_chat, a map keyed by chat display name:
 *   { "<chat name>": { chat, harvested, kept, reachedCutoff, messages:[
 *       { chat, author, ts, epoch, text } ... ] } }
 * The driver then flattens every chat's messages into one chronological list
 * (messages.json / messages.ndjson) and renders rollup.md. epoch (ms, from the
 * message data-mid) is the stable dedup + cutoff key.
 *
 * VERIFIED DOM CONTRACT (Teams web, 2026-09):
 *   chat list rows : [role="treeitem"][data-item-type="chat"]   (real 1:1/group chats;
 *                    folders are data-item-type="custom-folder"/"chats" — skip)
 *   chat list is virtualized -> scroll the tree to enumerate every chat.
 *   open thread row: [data-tid="chat-pane-item"] wrapping [data-tid="chat-pane-message"]
 *   timestamp      : data-mid on the message element is EPOCH-MS
 *                    (a child <time datetime="ISO"> agrees)
 *   author         : [id^="author-"].innerText; grouped consecutive messages share
 *                    one header, so carry the last-seen author forward
 *   body           : [id^="content-"] (fall back to the message innerText)
 *   history scroll : [data-tid="message-pane-list-viewport"]; scrolling up lazy-loads
 *                    older messages and evicts off-screen ones -> harvest EVERY frame
 *                    into a Map keyed by data-mid, never once at the end.
 *
 *   filter pills   : button[aria-pressed="true"] whose innerText is one of
 *                    "Unread" | "Channels" | "Chats" | "Meeting chats" (issue #57;
 *                    observed by the issue author, not re-verified here). Pills are
 *                    STICKY per client; a pressed one (e.g. "Chats") hides chats such
 *                    as meeting chats from the rail. run() clicks them off, waits
 *                    PILL_SETTLE_MS, records window.__ts.pills {cleared, countBefore,
 *                    countAfter, warning, restored}, warns in __ts.progress.warning if
 *                    the chat count changed, and re-presses them when the scrape ends.
 *
 *   row timestamp  : a <time datetime="ISO"> inside the chat row (issue #86; ASSUMED,
 *                    not re-verified here). Used only for the freshness check below;
 *                    when a row has none the check is skipped for that chat.
 *
 * FRESHNESS (issue #86): a freshly opened tab can still render a cached snapshot
 * from the previous session; the rail and threads look complete but miss
 * everything posted since. Two guards:
 *   1. readiness: before listing chats, wait until the rail order (row
 *      aria-labels) has been unchanged for RAIL_STABLE_MS and the tab is at
 *      least READY_FLOOR_MS old (performance.timeOrigin). The rail re-sorts when
 *      the sync lands. Gives up after READY_MAX_MS with a progress warning.
 *   2. per thread: if the newest harvested message is older than the row's
 *      timestamp (minus FRESH_TOLERANCE_MS), wait REFRESH_WAIT_MS, reopen the
 *      chat and harvest once more; if it still disagrees, warn.
 * All timings are overridable via window.__TS_* globals set before injection.
 *
 * SCOPE: left-rail 1:1 and group chats only. Teams CHANNEL posts are a different
 * surface and are not covered. Text bodies only (reactions/attachments/images and
 * quoted-reply bodies are captured as plain text or dropped).
 */

(function installTeamsScraper() {
  const CUTOFF_DAYS = Number(window.__TS_CUTOFF_DAYS ?? 7);          // harvest window
  const CUTOFF_MS   = Date.now() - CUTOFF_DAYS * 24 * 60 * 60 * 1000;
  const SETTLE_MS   = 550;    // wait after each scroll for lazy-load to paint
  const STEP_FRAC   = 0.85;   // scroll up ~0.85 * clientHeight per frame
  const MAX_FRAMES  = 400;    // hard ceiling on scroll frames per chat
  const NOPROG_STOP = 6;      // stop a chat after this many no-new-message frames
  const PILL_SETTLE_MS = Number(window.__TS_PILL_SETTLE_MS ?? 3500);  // rail re-render after clearing a pill
  const PILL_NAMES  = ['Unread', 'Channels', 'Chats', 'Meeting chats'];
  // Issue #86 freshness knobs (ms).
  const RAIL_STABLE_MS     = Number(window.__TS_RAIL_STABLE_MS ?? 20000);     // rail order unchanged this long
  const RAIL_POLL_MS       = Number(window.__TS_RAIL_POLL_MS ?? 1000);        // rail order sample interval
  const READY_FLOOR_MS     = Number(window.__TS_READY_FLOOR_MS ?? 30000);     // min tab age before harvesting
  const READY_MAX_MS       = Number(window.__TS_READY_MAX_MS ?? 120000);      // give up waiting (and warn)
  const FRESH_TOLERANCE_MS = Number(window.__TS_FRESH_TOLERANCE_MS ?? 60000); // row time vs newest message slack
  const REFRESH_WAIT_MS    = Number(window.__TS_REFRESH_WAIT_MS ?? 5000);     // wait before the one re-harvest

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function listChatRows() {
    return qsa('[role="treeitem"][data-item-type="chat"]');
  }

  // Sticky rail filter pills (issue #57). A pressed pill hides chats from the rail.
  function pressedPills() {
    return qsa('button[aria-pressed="true"]').filter((b) => PILL_NAMES.includes(b.innerText.trim()));
  }

  function rowName(row) {
    return row.getAttribute('aria-label') || row.innerText.trim().split('\n')[0];
  }

  // Issue #86: the row's last-activity time, from a machine-readable <time datetime>
  // only (never relative text like "Yesterday"). null when absent or unparsable.
  function rowEpoch(row) {
    const t = row && row.querySelector ? row.querySelector('time[datetime]') : null;
    const v = t && Date.parse(t.getAttribute('datetime'));
    return Number.isFinite(v) ? v : null;
  }

  // Tab age in ms; with no performance API, assume the tab was just opened.
  function tabAgeMs() {
    return (typeof performance !== 'undefined' && performance.timeOrigin) ? Date.now() - performance.timeOrigin : 0;
  }

  // Issue #86 readiness: wait until the tab is READY_FLOOR_MS old AND the visible
  // rail order has stayed unchanged for RAIL_STABLE_MS (both clocks run together). Samples only the mounted rows (no tree
  // scrolling, which would perturb what is being measured).
  async function waitForFreshRail() {
    const r = { polls: 0, waitedMs: 0, orderChanges: 0, gaveUp: false, tabAgeMs: tabAgeMs() };
    let last = null;
    let stableMs = 0;
    for (;;) {
      const order = listChatRows().map(rowName).join('\u0001');
      r.polls++;
      if (order && order === last) {
        stableMs += RAIL_POLL_MS;
      } else {
        if (last !== null) r.orderChanges++;
        stableMs = 0;
        last = order;
      }
      if (order && stableMs >= RAIL_STABLE_MS && r.tabAgeMs + r.waitedMs >= READY_FLOOR_MS) break;
      if (r.waitedMs >= READY_MAX_MS) { r.gaveUp = true; break; }
      await sleep(RAIL_POLL_MS);
      r.waitedMs += RAIL_POLL_MS;
    }
    return r;
  }

  // Harvest every message currently in the DOM of the open thread into `store`
  // (Map data-mid -> record). Returns the count of NEW messages added this frame.
  // `seen.maxMid` tracks the newest data-mid in the DOM, text or not (issue #86:
  // an attachment-only last message must not look like a stale thread).
  function harvestOpenThread(chatName, store, seen) {
    const items = qsa('[data-tid="chat-pane-item"]');
    let added = 0;
    let lastAuthor = null;
    for (const item of items) {
      const msg = item.querySelector('[data-tid="chat-pane-message"]') || item;
      const mid = msg.getAttribute('data-mid');
      const authorEl = item.querySelector('[id^="author-"]');
      if (authorEl && authorEl.innerText.trim()) lastAuthor = authorEl.innerText.trim();
      if (!mid) continue;                       // system/day-divider rows have no data-mid
      const epoch = Number(mid);
      if (!Number.isFinite(epoch)) continue;
      if (seen && !(epoch <= seen.maxMid)) seen.maxMid = epoch;
      const contentEl = item.querySelector('[id^="content-"]');
      const text = (contentEl ? contentEl.innerText : msg.innerText || '').trim();
      if (!text) continue;
      if (store.has(mid)) continue;
      store.set(mid, {
        chat: chatName,
        author: lastAuthor || '(unknown)',
        ts: new Date(epoch).toISOString(),
        epoch,
        text,
      });
      added++;
    }
    return added;
  }

  // Back-scroll one open thread to the cutoff (or top), harvesting each frame.
  async function scrapeOpenThread(chatName) {
    const store = new Map();
    const seen = { maxMid: null };
    const viewport = document.querySelector('[data-tid="message-pane-list-viewport"]');
    if (!viewport) return { chat: chatName, harvested: 0, kept: 0, reachedCutoff: false, messages: [] };

    let reachedCutoff = false;
    let noProgress = 0;
    for (let frame = 0; frame < MAX_FRAMES; frame++) {
      harvestOpenThread(chatName, store, seen);
      const oldest = Math.min(...Array.from(store.values(), (m) => m.epoch), Infinity);
      if (Number.isFinite(oldest) && oldest < CUTOFF_MS) { reachedCutoff = true; break; }

      const before = viewport.scrollTop;
      viewport.scrollTop = Math.max(0, before - viewport.clientHeight * STEP_FRAC);
      await sleep(SETTLE_MS);
      const added = harvestOpenThread(chatName, store, seen);
      const atTop = viewport.scrollTop <= 0 && before <= 0;
      if (added === 0 && (viewport.scrollTop === before || atTop)) {
        if (++noProgress >= NOPROG_STOP) break;
      } else {
        noProgress = 0;
      }
    }

    const all = Array.from(store.values()).sort((a, b) => a.epoch - b.epoch);
    const kept = all.filter((m) => m.epoch >= CUTOFF_MS);
    const newest = seen.maxMid;   // newest data-mid seen, incl. pre-cutoff and text-less messages
    return { chat: chatName, harvested: all.length, kept: kept.length, reachedCutoff, newest, messages: kept };
  }

  async function run() {
    const ts = window.__ts;
    ts.done = false;
    ts.error = null;
    ts.result = {};
    ts.pills = { cleared: [], restored: [], countBefore: null, countAfter: null, warning: null };
    ts.readiness = null;
    ts.stale = [];
    let tree = null;
    const warnings = [];
    const warn = (msg) => { warnings.push(msg); if (ts.progress) ts.progress.warning = warnings.join('; '); };
    // Enumerate chats; scroll the (virtualized) tree so every row mounts once.
    async function enumerateChats() {
      const seen = new Set();
      tree = listChatRows()[0]?.closest('[role="tree"]') || null;
      for (let pass = 0; pass < 40; pass++) {
        for (const row of listChatRows()) {
          const name = rowName(row);
          if (name) seen.add(name);
        }
        if (tree) { tree.scrollTop = Math.min(tree.scrollTop + tree.clientHeight * 0.8, tree.scrollHeight); await sleep(200); }
        else break;
        if (tree && tree.scrollTop + tree.clientHeight >= tree.scrollHeight) break;
      }
      return Array.from(seen);
    }
    // Re-find a chat row by name (the tree re-virtualizes as it scrolls).
    async function findRow(name) {
      let row = listChatRows().find((r) => (r.getAttribute('aria-label') || r.innerText).includes(name));
      if (!row) {
        // scroll the tree looking for it
        for (let s = 0; s < 40 && !row; s++) {
          if (tree) { tree.scrollTop += tree.clientHeight * 0.7; await sleep(150); }
          row = listChatRows().find((r) => (r.getAttribute('aria-label') || r.innerText).includes(name));
        }
      }
      return row || null;
    }
    // Issue #86: true when the row says the chat has activity newer than the thread shows.
    const looksStale = (rowAt, newest) => rowAt !== null && (newest === null || newest < rowAt - FRESH_TOLERANCE_MS);
    try {
      // Issue #57: a pressed filter pill (sticky per client) hides chats from the
      // rail. Count with the pill on, clear it, let the rail settle, count again.
      const pressed = pressedPills();
      let warning = null;
      if (pressed.length) {
        const before = (await enumerateChats()).length;
        ts.pills.cleared = pressed.map((b) => b.innerText.trim());
        ts.pills.countBefore = before;
        pressed.forEach((b) => b.click());
        await sleep(PILL_SETTLE_MS);              // wait for the rail to re-render
        if (tree) tree.scrollTop = 0;
      }
      // Issue #86: a cold tab can show last session's cached rail; wait for the sync.
      ts.progress = { phase: 'waiting-for-fresh-rail' };
      ts.readiness = await waitForFreshRail();
      if (tree) tree.scrollTop = 0;
      const chatNames = await enumerateChats();
      if (pressed.length) {
        ts.pills.countAfter = chatNames.length;
        if (ts.pills.countAfter !== ts.pills.countBefore) {
          warning = 'filter pill(s) [' + ts.pills.cleared.join(', ') + '] were pressed and hid chats: ' +
            ts.pills.countBefore + ' chats before clearing, ' + ts.pills.countAfter +
            ' after; the pill(s) were cleared so the run covers the full rail';
          ts.pills.warning = warning;
        }
      }
      ts.progress = { phase: 'chats-enumerated', total: chatNames.length, doneChats: 0 };
      if (warning) warn(warning);
      if (ts.readiness.gaveUp) {
        warn('chat rail order never settled within ' + READY_MAX_MS + ' ms; the client may still be ' +
          'showing a cached snapshot and the newest messages may be missing');
      }

      for (const name of chatNames) {
        const row = await findRow(name);
        if (!row) { ts.progress.doneChats++; continue; }
        row.click();
        await sleep(900);                         // let the thread pane mount
        let rec = await scrapeOpenThread(name);
        // Issue #86: re-read the row after the harvest and compare with the newest message.
        const after = await findRow(name);
        const rowAt = rowEpoch(after || row);
        const newestBefore = rec.newest;
        let reharvested = false;
        if (looksStale(rowAt, rec.newest)) {
          await sleep(REFRESH_WAIT_MS);           // give the client time to sync this thread
          const again = (await findRow(name)) || row;
          again.click();
          await sleep(900);
          // The first pass left the pane scrolled up; start the re-harvest at the newest end.
          const vp = document.querySelector('[data-tid="message-pane-list-viewport"]');
          if (vp) vp.scrollTop = vp.scrollHeight || 0;
          const rec2 = await scrapeOpenThread(name);
          reharvested = true;
          // Union both harvests (dedup on epoch); keep the fresher view's flags.
          const byEpoch = new Map(rec.messages.concat(rec2.messages).map((m) => [String(m.epoch), m]));
          const merged = Array.from(byEpoch.values()).sort((a, b) => a.epoch - b.epoch);
          rec = {
            ...rec2,
            harvested: Math.max(rec.harvested, rec2.harvested),
            kept: merged.length,
            reachedCutoff: rec.reachedCutoff || rec2.reachedCutoff,
            newest: Math.max(rec.newest ?? -Infinity, rec2.newest ?? -Infinity),
            messages: merged,
          };
          if (!Number.isFinite(rec.newest)) rec.newest = null;
        }
        rec.freshness = { rowEpoch: rowAt, newestBefore, newestAfter: rec.newest, reharvested };
        if (reharvested && looksStale(rowAt, rec.newest)) {
          ts.stale.push(name);
          warn('chat "' + name + '" looks stale: its row shows activity at ' + new Date(rowAt).toISOString() +
            ' but the newest harvested message is ' + (rec.newest === null ? 'none' : new Date(rec.newest).toISOString()) +
            ' after one re-harvest; newest messages may be missing');
        }
        ts.result[name] = rec;
        ts.progress.doneChats++;
        ts.progress.lastChat = name;
      }
    } catch (e) {
      ts.error = String(e && e.stack || e);
    }
    // Put the user's pill(s) back, also after an error.
    try {
      if (ts.pills.cleared.length) {
        const buttons = qsa('button[aria-pressed]');
        for (const name of ts.pills.cleared) {
          const b = buttons.find((x) => x.innerText.trim() === name && x.getAttribute('aria-pressed') !== 'true');
          if (b) { b.click(); ts.pills.restored.push(name); }
        }
      }
    } catch (e) {
      ts.error = ts.error || String(e && e.stack || e);
    }
    ts.done = true;
  }

  window.__ts = { done: false, error: null, result: {}, progress: {}, run };
  window.__ts.run();          // detached: driver polls window.__ts.{done,progress,result}
  return 'teams scraper installed; poll window.__ts.done / window.__ts.progress';
})();

/*
 * DRIVER (outside the page, e.g. CDP or manual):
 *
 * 1. evaluate this whole file in the Teams tab  -> installs + kicks window.__ts.run()
 * 2. poll every few seconds, short calls:
 *        JSON.stringify(window.__ts.progress)              // {doneChats,total,lastChat}
 *        window.__ts.done                                  // true when finished
 *        window.__ts.error                                 // non-null on failure
 * 3. when done, pull the result:
 *        JSON.stringify(window.__ts.result)                // raw_by_chat  -> raw_by_chat.json
 * 4. flatten + render (host side, any language):
 *        messages = Object.values(raw_by_chat).flatMap(c => c.messages)
 *                   .sort((a,b) => a.epoch - b.epoch)       // -> messages.json
 *        messages.ndjson = messages.map(JSON.stringify).join('\n')
 *        rollup.md       = group by chat, then by day, list "[HH:MM:SS] author: text"
 *
 * The whosaid adapter then turns each (chat, day) slice of `messages` into a
 * dated folder with teams.speakers.txt + teams.diarization.json
 * (source.kind="teams-chat"); see README.md here and whosaid issue #49.
 */
