/*
 * outlook-mail-scraper.js: harvest Outlook on the web mail into whosaid-ingestible JSON.
 *
 * Sibling of contrib/teams-scraper. Runs ENTIRELY in an already signed-in
 * Outlook web tab (outlook.cloud.microsoft or outlook.office.com), driven over
 * CDP or pasted into the console. It walks whatever the message list shows
 * right now: a folder (Inbox, Sent Items) or the results of a search you ran
 * first. It does not type the search itself, because OWA ignores synthetic
 * Enter keypresses; the driver runs the search with a real key event.
 *
 * Same detached/poll shape as the Teams scraper: install window.__os, kick
 * run() without awaiting it, then poll window.__os.{done,error,progress} with
 * short calls (one CDP evaluate is capped at ~15 s).
 *
 * OUTPUT (window.__os.result): raw_by_chat, a map keyed by conversation subject
 * (RE:/FW: stripped), the same shape the Teams scraper emits:
 *   { "<subject>": { chat, chat_id, harvested, kept, reachedCutoff, messages:[
 *       { chat, chat_id, msg_id, author, to, ts, epoch, text } ... ] } }
 * `whosaid outlook ingest` reads it. chat_id is the OWA conversation id, so two
 * threads with the same subject stay apart.
 *
 * VERIFIED DOM CONTRACT (Outlook on the web, 2026-10):
 *   message list   : [role="listbox"] holding rows [role="option"][data-convid];
 *                    virtualized, scroll its inner .customScrollBar to mount more rows
 *   row label      : aria-label carries "Unread"/"Collapsed", sender, subject and
 *                    the list date ("2:06 PM" today, "Mon 1:14 PM" this week,
 *                    "Thu 10/1" this year, "9/2/2026" older)
 *   reading pane   : #ConversationReadingPaneContainer; an element labeled
 *                    "<N> messages" says how many messages the thread holds
 *   subject        : [id^="CONV_"][id$="_SUBJECT"]
 *   one message    : [aria-label="Email message"], with
 *                    #MSG_<id>_FROM     "Display Name<addr@host>"
 *                    #MSG_<id>_TO       "To: Name; Name"
 *                    [data-testid="SentReceivedSavedTime"] "Wed 9/2/2026 2:24 PM"
 *                    [aria-label="Message body"]
 *   quoted history : OWA already folds the quoted reply chain behind a
 *                    "Show original message" control, so the body is the new text.
 *
 * TIMESTAMPS: OWA shows minute resolution and no epoch. The epoch is the parsed
 * minute (browser-local zone) plus a stable 0..59999 ms offset hashed from the
 * message id, so two mails in one minute do not collide on ingest's epoch dedup.
 * The seconds in the rendered transcript are therefore not real.
 *
 * SIDE EFFECT: opening a conversation marks it read, like clicking it by hand.
 * SCOPE: text bodies only; attachments, images and calendar items are skipped.
 */

(function installOutlookScraper() {
  const CUTOFF_DAYS = Number(window.__OS_CUTOFF_DAYS ?? 7);
  const CUTOFF_MS   = Date.now() - CUTOFF_DAYS * 24 * 60 * 60 * 1000;
  const MAX_CONVS   = Number(window.__OS_MAX_CONVS ?? 200);   // hard ceiling per run
  const PANE_WAIT_MS = 8000;  // max wait for a clicked thread to render
  const SETTLE_MS   = 400;
  const OLD_STOP    = 15;     // stop after this many consecutive rows older than the cutoff
  const NOPROG_STOP = 4;      // stop after this many list scrolls that mount no new row

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const oneLine = (s) => String(s || '').replace(/\s+/g, ' ').trim();

  function listRows() { return qsa('[role="listbox"] [role="option"][data-convid]'); }
  function listScroller() {
    const lb = document.querySelector('[role="listbox"]');
    if (!lb) return null;
    return [lb, ...qsa('*', lb)].find((e) => e.scrollHeight > e.clientHeight + 20 &&
      /auto|scroll/.test(getComputedStyle(e).overflowY)) || null;
  }

  // "Wed 9/2/2026 2:24 PM" -> epoch ms (browser-local zone), else NaN.
  function parseHeaderTime(s) {
    const m = oneLine(s).match(/(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)?/i);
    if (!m) return NaN;
    let h = Number(m[4]);
    if (m[6]) h = (h % 12) + (/p/i.test(m[6]) ? 12 : 0);
    return new Date(Number(m[3]), Number(m[1]) - 1, Number(m[2]), h, Number(m[5])).getTime();
  }

  // Newest date a list row shows, as epoch ms at day resolution; NaN when unknown.
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  function parseRowDate(label) {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    let m = label.match(/\b(\d{1,2})\/(\d{1,2})\/(\d{4})\b/);
    if (m) return new Date(Number(m[3]), Number(m[1]) - 1, Number(m[2])).getTime();
    m = label.match(/\b(Sun|Mon|Tue|Wed|Thu|Fri|Sat) (\d{1,2})\/(\d{1,2})\b/);
    if (m) {
      let t = new Date(now.getFullYear(), Number(m[2]) - 1, Number(m[3])).getTime();
      if (t > today + 864e5) t = new Date(now.getFullYear() - 1, Number(m[2]) - 1, Number(m[3])).getTime();
      return t;
    }
    m = label.match(/\b(Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d{1,2}:\d{2}\s*[AP]M\b/);
    if (m) {
      const back = (now.getDay() - DAYS.indexOf(m[1]) + 7) % 7 || 7;
      return today - back * 864e5;
    }
    if (/\b\d{1,2}:\d{2}\s*[AP]M\b/.test(label)) return today;
    return NaN;
  }

  function hashMs(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0) % 60000;
  }

  function normSubject(s) {
    let t = oneLine(s);
    for (let i = 0; i < 5; i++) t = t.replace(/^(re|fw|fwd|aw|wg|sv)\s*:\s*/i, '');
    return t || '(no subject)';
  }

  // Drop banner and Jira-style boilerplate lines; keep the human text.
  const NOISE = [
    /^You don't often get email from .*Learn why this is important\.?$/i,
    /^CAUTION: This email originated from outside/i,
    /^Some content in this message has been blocked/i,
    /^[-\u2014\u2013]{2,}.*$/,
    /^Reply above this line\.?$/i,
    /^Get Outlook for (iOS|Android)$/i,
  ];
  function cleanBody(text) {
    return String(text || '').split('\n').map((l) => l.trim())
      .filter((l) => l && !NOISE.some((re) => re.test(l))).join('\n').trim();
  }

  function paneCount() {
    const el = qsa('#ConversationReadingPaneContainer [aria-label]')
      .find((e) => /^\d+ messages?$/.test(e.getAttribute('aria-label')));
    return el ? Number(el.getAttribute('aria-label').split(' ')[0]) : null;
  }

  // Wait until the pane shows the clicked thread and every message has mounted.
  async function waitForThread(prevSubjectId) {
    const t0 = Date.now();
    let last = -1, stable = 0;
    while (Date.now() - t0 < PANE_WAIT_MS) {
      await sleep(250);
      const subj = document.querySelector('[id^="CONV_"][id$="_SUBJECT"]');
      if (!subj || subj.id === prevSubjectId) continue;
      const n = qsa('[aria-label="Email message"]').length;
      const want = paneCount();
      if (n && want && n >= want) return { subj, n, want };
      if (n === last) { if (++stable >= 8) return { subj, n, want }; } else { stable = 0; last = n; }
    }
    return null;
  }

  function harvestThread(convid) {
    const subjEl = document.querySelector('[id^="CONV_"][id$="_SUBJECT"]');
    const chat = normSubject(subjEl ? subjEl.innerText : '');
    const out = [];
    for (const m of qsa('[aria-label="Email message"]')) {
      const fromEl = m.querySelector('[id^="MSG_"][id$="_FROM"]');
      const msgId = fromEl ? fromEl.id.replace(/^MSG_/, '').replace(/_FROM$/, '') : '';
      const author = oneLine(fromEl ? fromEl.innerText.split('<')[0] : '') || '(unknown)';
      const toEl = m.querySelector('[id^="MSG_"][id$="_TO"]');
      const to = oneLine(toEl ? toEl.innerText : '').replace(/^To:\s*/i, '');
      const timeEl = m.querySelector('[data-testid="SentReceivedSavedTime"]');
      const minute = parseHeaderTime(timeEl ? timeEl.innerText : '');
      if (!Number.isFinite(minute)) continue;
      const bodyEl = m.querySelector('[aria-label="Message body"]');
      const text = cleanBody(bodyEl ? bodyEl.innerText : '');
      if (!text) continue;
      const epoch = minute + hashMs(msgId || author + text.slice(0, 80));
      out.push({ chat, chat_id: convid, msg_id: msgId, author, to,
        ts: new Date(epoch).toISOString(), epoch, text });
    }
    return { chat, messages: out };
  }

  async function run() {
    const os = window.__os;
    os.done = false; os.error = null; os.result = {};
    os.progress = { phase: 'walking', seen: 0, opened: 0, skippedOld: 0, failed: 0 };
    try {
      if (!listRows().length) throw new Error('no message list rows: open a folder or run a search first');
      const scroller = listScroller();
      if (scroller) { scroller.scrollTop = 0; await sleep(SETTLE_MS); }
      const visited = new Set();
      let oldRun = 0, noProg = 0;
      while (os.progress.opened < MAX_CONVS) {
        const fresh = listRows().filter((r) => !visited.has(r.getAttribute('data-convid')));
        if (!fresh.length) {
          if (!scroller) break;
          const before = scroller.scrollTop;
          scroller.scrollTop = before + scroller.clientHeight * 0.8;
          await sleep(SETTLE_MS * 2);
          if (scroller.scrollTop === before && ++noProg >= NOPROG_STOP) break;
          if (!listRows().some((r) => !visited.has(r.getAttribute('data-convid'))) && ++noProg >= NOPROG_STOP) break;
          continue;
        }
        noProg = 0;
        const row = fresh[0];
        const convid = row.getAttribute('data-convid');
        visited.add(convid);
        os.progress.seen++;
        const label = row.getAttribute('aria-label') || '';
        const rowDay = parseRowDate(label);
        if (Number.isFinite(rowDay) && rowDay + 864e5 <= CUTOFF_MS) {
          os.progress.skippedOld++;
          if (++oldRun >= OLD_STOP) { os.progress.stoppedAt = 'cutoff'; break; }
          continue;
        }
        oldRun = 0;
        const prev = document.querySelector('[id^="CONV_"][id$="_SUBJECT"]');
        row.scrollIntoView({ block: 'nearest' });
        row.click();
        const ok = await waitForThread(prev ? prev.id : null);
        if (!ok) { os.progress.failed++; continue; }
        const { chat, messages } = harvestThread(convid);
        const kept = messages.filter((m) => m.epoch >= CUTOFF_MS).sort((a, b) => a.epoch - b.epoch);
        // Key by subject; a second thread with the same subject gets its id appended.
        let key = chat;
        if (os.result[key] && os.result[key].chat_id !== convid) key = chat + ' [' + convid.slice(-8) + ']';
        os.result[key] = { chat, chat_id: convid, harvested: messages.length, kept: kept.length,
          reachedCutoff: messages.some((m) => m.epoch < CUTOFF_MS), messages: kept };
        os.progress.opened++;
        os.progress.lastChat = chat;
      }
    } catch (e) {
      os.error = String((e && e.stack) || e);
    }
    os.done = true;
  }

  window.__os = { done: false, error: null, result: {}, progress: {}, run };
  window.__os.run();
  return 'outlook scraper installed; poll window.__os.done / window.__os.progress';
})();
