/*
 * teams-channel-scraper.js: harvest MS Teams web CHANNEL posts and their replies
 * into whosaid-ingestible JSON. Companion to teams-chat-scraper.js, which covers
 * left-rail 1:1 and group chats only.
 *
 * Same shape as the chat scraper: runs in the Teams web tab on an authenticated
 * session, installs window.__tsc, kicks run() as a detached promise, and the
 * driver POLLS window.__tsc.{done,progress,result} with short calls (one CDP
 * Runtime.evaluate is capped at about 15 s; a scrape runs minutes).
 *
 * OUTPUT (window.__tsc.result): the same raw_by_chat map the chat scraper emits,
 * keyed by "<Team> > <Channel>", so `whosaid teams ingest` reads it unchanged:
 *   { "<Team> > <Channel>": { chat, chat_id, harvested, kept, reachedCutoff, expanded,
 *       messages:[ { chat, chat_id, author, ts, epoch, text, is_reply,
 *                    thread_root_epoch, reply_to?, images? } ... ] } }
 * chat_id is the channel thread id (19:...@thread.tacv2), so two channels named
 * "General" in different teams never merge. Replies carry is_reply, the root's
 * epoch and the root's author (reply_to); with window.__TS_REPLY_PREFIX (default
 * true) the text also starts with "(reply to <author>, post of <ISO>) " so the
 * thread survives in the rendered speakers file, which drops unknown fields.
 *
 * VERIFIED DOM CONTRACT (Teams web, 2026-10-21):
 *   rail filter    : the "Channels" pill (button[aria-pressed], innerText "Channels").
 *                    Pills behave as exclusive here: pressing one releases the other.
 *   channel rows   : [role="treeitem"][data-item-type="channel"] with
 *                    data-testid="sc-channel-list-item-<threadId>". Favorites repeat
 *                    as favorite-channel-list-item-<threadId>; those are skipped.
 *   team           : the row's ancestor [role="treeitem"][data-item-type="team"];
 *                    its name is the first line of its innerText (aria-label is empty).
 *   post list      : [data-tid="channel-pane-viewport"]; each post is
 *                    [data-tid="channel-pane-message"] holding the root and its
 *                    last 3 replies.
 *   message        : the element with data-mid (epoch ms) AND data-reply-chain-id (the
 *                    root's mid; mid == chain id on the root). Its data-testid is
 *                    message-body-flex-wrapper. The outer copies of the mid (the
 *                    channel-pane-message row, reply rows) have no chain id; skipped.
 *   author         : #author-<mid>, only on the first message of a same-author run,
 *                    so the author is carried forward within a chain.
 *   body           : #content-<mid> (innerText is full even when "see more" folds it).
 *   hidden replies : [data-tid="response-summary-button"] ("Open N replies from ...")
 *                    opens the thread in place (URL unchanged) with viewport
 *                    [data-tid="channel-replies-viewport"]; [data-tid="close-l2-view-button"]
 *                    ("Go to channel") returns to the post list.
 *
 * CUTOFF: messages older than CUTOFF_DAYS are dropped. Posts are not strictly
 * ordered by their newest reply, so the back-scroll keeps going until the oldest
 * ROOT is older than the cutoff by GRACE_DAYS (window.__TS_CHANNEL_GRACE_DAYS,
 * default 30). A thread is expanded when any of its visible messages is inside
 * the cutoff (hidden replies are older than the visible ones, so a thread whose
 * visible messages are all old has nothing new hidden).
 *
 * SCOPE: channels mounted in the rail with the Channels pill on (collapsed teams
 * and hidden channels are not opened). Text bodies plus inline images, as in the
 * chat scraper; reactions and file cards are plain text or dropped.
 * window.__TS_CHANNELS = ["substring", ...] limits the run to matching
 * "<Team> > <Channel>" names.
 */

(function installTeamsChannelScraper() {
  const CUTOFF_DAYS = Number(window.__TS_CUTOFF_DAYS ?? 7);
  const DAY_MS      = 24 * 60 * 60 * 1000;
  const CUTOFF_MS   = Date.now() - CUTOFF_DAYS * DAY_MS;
  const GRACE_MS    = Number(window.__TS_CHANNEL_GRACE_DAYS ?? 30) * DAY_MS;
  const SETTLE_MS   = 550;    // wait after each scroll for lazy-load to paint
  const STEP_FRAC   = 0.85;   // scroll up about 0.85 * clientHeight per frame
  const MAX_FRAMES  = 400;    // hard ceiling on scroll frames per channel or thread
  const NOPROG_STOP = 6;      // stop after this many no-new-message frames
  const OPEN_MS     = Number(window.__TS_CHANNEL_OPEN_MS ?? 2500);  // channel or thread pane mount
  const PILL_SETTLE_MS = Number(window.__TS_PILL_SETTLE_MS ?? 3500);
  const PILL_NAMES  = ['Unread', 'Channels', 'Chats', 'Meeting chats', 'Communities'];
  const REPLY_PREFIX = window.__TS_REPLY_PREFIX !== false;
  const ONLY        = Array.isArray(window.__TS_CHANNELS) ? window.__TS_CHANNELS : null;
  const IMAGES          = window.__TS_IMAGES !== false;
  const IMAGE_MAX_CHARS = Number(window.__TS_IMAGE_MAX_CHARS ?? 8000000);
  const IMAGE_SEL       = 'img[itemtype="http://schema.skype.com/AMSImage"]';

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const ROW_PREFIX = 'sc-channel-list-item-';

  function pillButtons() {
    return qsa('button[aria-pressed]').filter((b) => PILL_NAMES.includes(b.innerText.trim()));
  }

  function listChannelRows() {
    return qsa('[role="treeitem"][data-item-type="channel"]')
      .filter((r) => (r.getAttribute('data-testid') || '').startsWith(ROW_PREFIX));
  }

  function channelInfo(row) {
    const id = row.getAttribute('data-testid').slice(ROW_PREFIX.length);
    const channel = (row.innerText || '').trim().split('\n')[0] || id;
    const teamRow = row.parentElement && row.parentElement.closest('[role="treeitem"][data-item-type="team"]');
    const team = teamRow ? (teamRow.innerText || '').trim().split('\n')[0] : '(unknown team)';
    return { id, name: team + ' > ' + channel };
  }

  // Same canvas copy as teams-chat-scraper.js (AMS images are same-origin blob: URLs).
  function imageData(img) {
    if (!img.complete || img.naturalWidth < 4 || img.naturalHeight < 4) return { data: null, skipped: 'not-loaded' };
    try {
      const c = document.createElement('canvas');
      c.width = img.naturalWidth;
      c.height = img.naturalHeight;
      c.getContext('2d').drawImage(img, 0, 0);
      const data = c.toDataURL('image/png');
      return data.length > IMAGE_MAX_CHARS ? { data: null, skipped: 'too-large' } : { data, skipped: null };
    } catch (e) {
      return { data: null, skipped: 'error: ' + String(e && e.message || e) };
    }
  }

  function harvestImages(root) {
    if (!IMAGES || !root) return [];
    return qsa(IMAGE_SEL, root).map((img) => {
      const { data, skipped } = imageData(img);
      const rec = {
        alt: img.getAttribute('alt') || '',
        src: img.getAttribute('data-gallery-src') || img.getAttribute('data-orig-src') || null,
        width: img.naturalWidth || null,
        height: img.naturalHeight || null,
        data,
      };
      if (skipped) rec.skipped = skipped;
      return rec;
    });
  }

  // Harvest every message mounted under `root` into `store` (Map mid -> record).
  // Returns the number of NEW messages. Authors carry forward within a chain.
  function harvest(root, ch, store) {
    let added = 0;
    const lastAuthor = new Map();   // chain id -> last author seen in DOM order
    for (const el of qsa('[data-mid][data-reply-chain-id]', root)) {
      const mid = el.getAttribute('data-mid');
      const chain = el.getAttribute('data-reply-chain-id');
      const epoch = Number(mid);
      if (!Number.isFinite(epoch) || !chain) continue;
      const authorEl = document.getElementById('author-' + mid);
      const named = authorEl && authorEl.innerText.trim();
      if (named) lastAuthor.set(chain, named);
      const have = store.get(mid);
      if (have) {
        // An earlier frame may have met this message without its author header.
        if (have.author === '(unknown)' && lastAuthor.get(chain)) have.author = lastAuthor.get(chain);
        continue;
      }
      const contentEl = document.getElementById('content-' + mid);
      const text = (contentEl ? contentEl.innerText : '').trim();
      const images = harvestImages(contentEl);
      if (!text && !images.length) continue;
      const rec = {
        chat: ch.name,
        chat_id: ch.id,
        author: lastAuthor.get(chain) || '(unknown)',
        ts: new Date(epoch).toISOString(),
        epoch,
        text,
        is_reply: mid !== chain,
        thread_root_epoch: Number(chain),
      };
      if (images.length) rec.images = images;
      store.set(mid, rec);
      added++;
    }
    return added;
  }

  // Newest epoch among the messages of one chain that are mounted under `post`.
  function newestInPost(post) {
    const mids = qsa('[data-mid][data-reply-chain-id]', post).map((e) => Number(e.getAttribute('data-mid')));
    return mids.length ? Math.max(...mids) : null;
  }

  // Open one thread's hidden replies, scrape them, and return to the post list.
  async function expandThread(button, ch, store, viewport) {
    const saved = viewport.scrollTop;
    button.click();
    await sleep(OPEN_MS);
    const rv = document.querySelector('[data-tid="channel-replies-viewport"]');
    if (rv) {
      let noProgress = 0;
      for (let f = 0; f < MAX_FRAMES; f++) {
        const before = rv.scrollTop;
        const added = harvest(rv, ch, store);
        if (before <= 0 && added === 0) { if (++noProgress >= 2) break; } else noProgress = 0;
        rv.scrollTop = Math.max(0, before - rv.clientHeight * STEP_FRAC);
        await sleep(SETTLE_MS);
      }
      harvest(rv, ch, store);
    }
    const back = document.querySelector('[data-tid="close-l2-view-button"]');
    if (back) back.click();
    await sleep(OPEN_MS);
    const vp = document.querySelector('[data-tid="channel-pane-viewport"]') || viewport;
    vp.scrollTop = saved;
    await sleep(SETTLE_MS);
    return { ok: !!rv, viewport: vp };
  }

  async function scrapeChannel(ch) {
    const store = new Map();
    const expanded = new Set();
    let viewport = document.querySelector('[data-tid="channel-pane-viewport"]');
    const out = { chat: ch.name, chat_id: ch.id, harvested: 0, kept: 0, reachedCutoff: false, expanded: 0, messages: [] };
    if (!viewport) return out;
    viewport.scrollTop = viewport.scrollHeight;
    await sleep(SETTLE_MS);
    let noProgress = 0;
    for (let frame = 0; frame < MAX_FRAMES; frame++) {
      let added = harvest(viewport, ch, store);
      // Expand threads whose visible messages reach inside the cutoff.
      for (const post of qsa('[data-tid="channel-pane-message"]', viewport)) {
        const btn = post.querySelector('[data-tid="response-summary-button"]');
        const root = post.querySelector('[data-mid][data-reply-chain-id]');
        const chain = root && root.getAttribute('data-reply-chain-id');
        if (!btn || !chain || expanded.has(chain)) continue;
        const newest = newestInPost(post);
        if (newest === null || newest < CUTOFF_MS) continue;
        expanded.add(chain);
        const r = await expandThread(btn, ch, store, viewport);
        viewport = r.viewport;
        added += harvest(viewport, ch, store);
        break;   // the DOM was re-rendered; rescan posts on the next frame
      }
      const roots = Array.from(store.values()).filter((m) => !m.is_reply).map((m) => m.epoch);
      const oldestRoot = roots.length ? Math.min(...roots) : Infinity;
      if (oldestRoot < CUTOFF_MS - GRACE_MS) { out.reachedCutoff = true; break; }
      const before = viewport.scrollTop;
      viewport.scrollTop = Math.max(0, before - viewport.clientHeight * STEP_FRAC);
      await sleep(SETTLE_MS);
      added += harvest(viewport, ch, store);
      if (added === 0 && viewport.scrollTop === before) {
        if (++noProgress >= NOPROG_STOP) break;
      } else {
        noProgress = 0;
      }
    }
    const roots = new Map(Array.from(store.values()).filter((m) => !m.is_reply).map((m) => [m.epoch, m]));
    const all = Array.from(store.values()).sort((a, b) => a.epoch - b.epoch);
    for (const m of all) {
      if (!m.is_reply) continue;
      const root = roots.get(m.thread_root_epoch);
      if (root) m.reply_to = root.author;
      if (REPLY_PREFIX) {
        m.text = '(reply to ' + (root ? root.author : 'a post') + ', post of ' +
          new Date(m.thread_root_epoch).toISOString().slice(0, 16) + 'Z) ' + m.text;
      }
    }
    out.messages = all.filter((m) => m.epoch >= CUTOFF_MS);
    out.harvested = all.length;
    out.kept = out.messages.length;
    out.expanded = expanded.size;
    return out;
  }

  async function setPills(wanted) {
    // Pills are exclusive, so settle in two passes: release the unwanted, then press the wanted.
    for (const b of pillButtons()) {
      if (b.getAttribute('aria-pressed') === 'true' && !wanted.includes(b.innerText.trim())) { b.click(); await sleep(300); }
    }
    for (const b of pillButtons()) {
      if (b.getAttribute('aria-pressed') !== 'true' && wanted.includes(b.innerText.trim())) { b.click(); await sleep(300); }
    }
    await sleep(PILL_SETTLE_MS);
  }

  async function run() {
    const t = window.__tsc;
    t.done = false;
    t.error = null;
    t.result = {};
    const original = pillButtons().filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.innerText.trim());
    t.pills = { original, restored: null };
    try {
      t.progress = { phase: 'channels-pill' };
      await setPills(['Channels']);
      const seen = new Map();
      for (const row of listChannelRows()) {
        const ch = channelInfo(row);
        if (!seen.has(ch.id)) seen.set(ch.id, ch);
      }
      let channels = Array.from(seen.values());
      if (ONLY) channels = channels.filter((c) => ONLY.some((s) => c.name.includes(s)));
      t.progress = { phase: 'channels-enumerated', total: channels.length, doneChannels: 0, channels: channels.map((c) => c.name) };
      for (const ch of channels) {
        const row = listChannelRows().find((r) => r.getAttribute('data-testid') === ROW_PREFIX + ch.id);
        if (!row) { t.progress.doneChannels++; continue; }
        row.click();
        await sleep(OPEN_MS);
        // A thread left open from an earlier visit hides the post list.
        const back = document.querySelector('[data-tid="close-l2-view-button"]');
        if (back) { back.click(); await sleep(OPEN_MS); }
        t.result[ch.name] = await scrapeChannel(ch);
        t.progress.doneChannels++;
        t.progress.lastChannel = ch.name;
      }
    } catch (e) {
      t.error = String(e && e.stack || e);
    }
    // Put the user's pill(s) back, also after an error.
    try {
      await setPills(original);
      t.pills.restored = pillButtons().filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.innerText.trim());
    } catch (e) {
      t.error = t.error || String(e && e.stack || e);
    }
    t.done = true;
  }

  window.__tsc = { done: false, error: null, result: {}, progress: {}, run };
  window.__tsc.run();
  return 'teams channel scraper installed; poll window.__tsc.done / window.__tsc.progress';
})();

/*
 * DRIVER: as for teams-chat-scraper.js, with window.__tsc in place of window.__ts.
 *   1. optional: window.__TS_CUTOFF_DAYS = 7; window.__TS_CHANNELS = ['Project'];
 *   2. evaluate this file in the Teams tab, then poll JSON.stringify(window.__tsc.progress)
 *      and window.__tsc.done with short calls;
 *   3. JSON.stringify(window.__tsc.result) -> raw.json, then
 *      whosaid teams ingest raw.json --into <dir> --tz <zone> --self "<you>".
 */
