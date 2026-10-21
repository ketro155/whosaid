# Teams chat scraper

`whosaid teams ingest` reads a JSON export of Microsoft Teams chat messages.
[`teams-chat-scraper.js`](teams-chat-scraper.js) produces that export: it is
plain page JavaScript that runs inside an already signed-in Teams web tab. You
can drive it over the Chrome DevTools Protocol from any client (cdp-toolkit's
`evaluate_script`, Playwright, a raw CDP socket), or paste it into the tab's
console. It lives in `contrib/` because it needs a live browser session; whosaid
itself stays offline and only reads the JSON the scraper hands over.

## Running it

1. Open `teams.cloud.microsoft` and sign in. Optionally set
   `window.__TS_CUTOFF_DAYS = 14` first (the default is 7).
2. Evaluate the whole file in the tab. It installs `window.__ts` and starts the
   scrape without waiting for it to finish (see *Long scrapes* below). It first
   waits for the client to be fresh (see *Readiness* below), so expect at least
   about 30 s with `progress.phase` = `waiting-for-fresh-rail` on a new tab.
3. Poll with short calls until `window.__ts.done` is `true`, watching
   `JSON.stringify(window.__ts.progress)`. A non-null `window.__ts.error` means
   the scrape failed.
4. Save `JSON.stringify(window.__ts.result)` to a file and ingest it:

```bash
whosaid teams ingest raw_by_chat.json --into ~/meetings --tz America/Los_Angeles --self Alice_Example
```

## Output: the hand-off JSON

`window.__ts.result` is `raw_by_chat`, an object keyed by chat name:

```json
{"Project sync": {"chat": "Project sync", "harvested": 40, "kept": 12, "reachedCutoff": true,
  "messages": [
    {"chat": "Project sync", "author": "Doe, Jane (Vendor, consultant)",
     "ts": "2026-09-24T16:05:06.000Z", "epoch": 1790265906000,
     "text": "i got the telemetry for the widget"}]}}
```

`whosaid teams ingest` accepts that map, and also a flat list of the same
records (`messages.json`), NDJSON with one record per line (`messages.ndjson`),
or `{"messages": [...]}`. Each record's fields:

| Field | Meaning |
|---|---|
| `chat` | Chat display name (left rail). |
| `author` | Author display name, as Teams renders it. |
| `epoch` (or `epoch_ms`) | The message's `data-mid`: epoch milliseconds, and the dedup key. |
| `ts` (or `timestamp_iso`) | The same instant as ISO 8601. It is used only when there is no epoch. |
| `text` | The message body as plain text (`""` for an image-only message). |
| `images` | Optional: inline images, see *Images* below. |
| `chat_id`, `url` | Optional: the Teams conversation id and page URL. |

Records may be unsorted and may repeat. Ingest dedups on the epoch, so
re-running a scrape over an overlapping window is safe.

Each chat entry also carries `newest` (epoch of the newest message seen in the
thread, including ones older than the cutoff or without text) and `freshness` (`rowEpoch`,
`newestBefore`, `newestAfter`, `reharvested`; see *Readiness* below). Ingest
ignores both.

**Delta runs: overlap by at least 36 h.** If a driver scrapes on a schedule,
set the cutoff to at least the run interval plus 36 h, so consecutive windows
overlap by 36 h or more (for example `window.__TS_CUTOFF_DAYS = 3` for a daily
run, a 48 h overlap). Every message is
then covered by two independent runs, so one run that hit a stale client
(issue #86) does not lose it. Ingest dedups on the epoch, so the overlap is
free.

## Readiness: wait for a fresh client (issue #86)

A freshly opened Teams tab can render a cached snapshot from the previous
session for a while. The rail row count is already right, but the rail and the
threads miss everything posted since. A scrape at that point reports
`reachedCutoff=true` and `kept=0` for a chat whose newest messages are simply
not in the DOM yet. Waiting for the row count to settle is not enough. The
scraper has two guards:

1. **Rail order.** First, before the pill check and before listing chats, it
   samples the order of the mounted rail rows (their `aria-label`s) and waits until it has been unchanged for
   20 s. The rail re-sorts when the sync lands, so a stable order is the
   freshness signal. It also waits until the tab is at least 30 s old
   (measured from `performance.timeOrigin`). The two waits overlap: a cold tab
   injected at 9 s with a stable rail is ready at about 30 s, a warm tab after
   about 20 s. If the order has not settled after
   120 s it carries on and sets `window.__ts.progress.warning`. The outcome is
   in `window.__ts.readiness` (`polls`, `waitedMs`, `tabAgeMs` at start,
   `orderChanges`, `gaveUp`).
2. **Per thread.** After harvesting a chat it re-reads the chat row's
   `<time datetime>` (assumed DOM, see below). If the newest harvested message is
   more than 60 s older than that, it waits 5 s, reopens the chat and harvests
   once more, merging both passes. If the thread still looks older than the row,
   the chat name goes into `window.__ts.stale` and `window.__ts.progress.warning`
   says so. Rows without a machine-readable time skip this check.

Warnings from the pill check, the rail and stale threads are joined with `; `
in `window.__ts.progress.warning`. Treat any warning as "re-run later".

All timings can be overridden by setting globals before injecting the file:

| Global | Default | Meaning |
|---|---|---|
| `__TS_RAIL_STABLE_MS` | 20000 | Rail order must be unchanged this long. |
| `__TS_RAIL_POLL_MS` | 1000 | Rail order sample interval. |
| `__TS_READY_FLOOR_MS` | 30000 | Minimum tab age before harvesting. |
| `__TS_READY_MAX_MS` | 120000 | Stop waiting for the rail (and warn) after this. |
| `__TS_FRESH_TOLERANCE_MS` | 60000 | Allowed gap between row time and newest message. |
| `__TS_REFRESH_WAIT_MS` | 5000 | Wait before the single re-harvest of a stale thread. |

## DOM contract (Teams web, `teams.cloud.microsoft`)

- **Chat list:** `[role="treeitem"][data-item-type="chat"]`. Folders are
  `data-item-type="custom-folder"` or `"chats"`. The left tree is virtualized,
  so scroll it to enumerate every chat.
- **Open thread rows:** `[data-tid="chat-pane-item"]` wrapping
  `[data-tid="chat-pane-message"]`.
- **Timestamp:** `data-mid` on the message element is epoch milliseconds, and a
  child `<time datetime="ISO">` agrees with it.
- **Author:** `[id^="author-"]` innerText. Consecutive messages from one author
  share one header, so carry the last-seen author forward.
- **Body:** `[id^="content-"]`, falling back to the message innerText.
- **History is virtualized:** the scroll container is
  `[data-tid="message-pane-list-viewport"]`. Scrolling up lazy-loads older
  messages and evicts off-screen ones, so harvest into a map keyed by
  `data-mid` on every scroll frame, not once at the end.
- **Filter pills (issue #57):** the rail's `Unread`, `Channels`, `Chats` and
  `Meeting chats` pills are `button[aria-pressed="true"]` when active. They are
  **sticky per client**, and a pressed pill hides chats from the rail (for example,
  `Chats` hides meeting chats). The scraper clears any pressed pill before it lists
  chats, waits for the rail to settle, and records what it cleared in
  `window.__ts.pills`. If the chat count changed after clearing it sets
  `window.__ts.progress.warning`, so a run that would have skipped chats does not
  look complete. Both counts are taken after the readiness wait below, so a cold
  tab whose cached rail grows when the sync lands does not raise a false warning.
  It presses your pill(s) again when the scrape ends.
- **Row time (issue #86, assumed, not re-verified):** a `<time datetime="ISO">`
  inside the chat row gives the chat's last activity. Only this machine-readable
  value is used, never relative text like "Yesterday".

Scrape loop per chat: click the chat row, wait about 900 ms, then scroll the viewport up in steps
of about 0.85 × `clientHeight` with a ~550 ms settle, re-harvesting each frame.
Stop when the oldest `data-mid` is older than the cutoff, the top is reached, or
several frames make no progress. Then run the per-thread freshness check above.

**Long scrapes and the CDP call limit.** A single `Runtime.evaluate` call is
capped at about 15 s, and a multi-chat scrape runs for minutes. Start the async
scrape detached (start the promise, return at once, keep results on a `window`
object), then poll that object with short calls. One awaited long call times
out and loses its return value.

## Scope

- Left-rail 1:1 and group chats only. Channel posts are a different surface;
  use [`teams-channel-scraper.js`](teams-channel-scraper.js) for them (see
  *Channel posts* below).
- Text bodies plus inline images (see *Images* below). Reactions, file
  attachments and quoted-reply bodies are flattened to text or dropped.

## Images

Inline pasted images (`img[itemtype="http://schema.skype.com/AMSImage"]`) are
copied through a canvas into a PNG data URL on the message, at the size Teams
rendered them. A message holding only images is kept with `text` `""`:

```json
{"images": [{"alt": "image", "src": "<full-size AMS URL>", "width": 379, "height": 800,
             "data": "data:image/png;base64,..."}]}
```

Teams shows a 1x1 placeholder until an image's blob arrives, so after the
back-scroll the scraper returns to the newest end and walks up again for any
image still loading (`window.__TS_IMAGE_RETRIES`, default 2). `data` is `null`
with `skipped` set to `not-loaded`, `too-large` (over
`window.__TS_IMAGE_MAX_CHARS`, default 8,000,000 characters) or `error: ...`.
Ingest writes each image to `<folder>/teams-images/<epoch>-<n>.png` and appends
`[image: teams-images/<epoch>-<n>.png]` to the message in `teams.speakers.txt`
(`[image not captured]` when there was no data); the sidecar segment lists the
paths under `images`. A later scrape without the data keeps the earlier
references. Set `window.__TS_IMAGES = false` before injecting to stay text-only.
Images stay on disk next to the transcript; nothing is uploaded.

## Channel posts

[`teams-channel-scraper.js`](teams-channel-scraper.js) covers team channels:
the root posts and their replies, including replies Teams hides behind
"Open N replies". It works like the chat scraper, but its handle is
`window.__tsc`, and it emits the same `raw_by_chat` shape keyed by
`"<Team> > <Channel>"`, so `whosaid teams ingest` reads it unchanged.

1. Optionally set `window.__TS_CUTOFF_DAYS` (default 7) and
   `window.__TS_CHANNELS = ['substring', ...]` to limit the run to matching
   `"<Team> > <Channel>"` names.
2. Evaluate the file, then poll `window.__tsc.done` and
   `JSON.stringify(window.__tsc.progress)` with short calls.
3. Save `JSON.stringify(window.__tsc.result)` and run `whosaid teams ingest` on it.

How it reads a channel:

- It presses the **Channels** pill, lists every
  `[data-item-type="channel"]` row (`data-testid="sc-channel-list-item-<threadId>"`;
  favorites repeat and are skipped), and puts the user's pills back at the end.
- Each message is the element carrying both `data-mid` (epoch ms) and
  `data-reply-chain-id` (the root's mid). The author header `#author-<mid>` only
  appears on the first message of a same-author run, so the author is carried
  forward within a thread.
- An inline post shows only its last 3 replies. When any visible message of a
  thread is inside the cutoff, the scraper clicks
  `[data-tid="response-summary-button"]`, scrapes
  `[data-tid="channel-replies-viewport"]`, and returns with
  `[data-tid="close-l2-view-button"]` ("Go to channel").
- An old post can get a new reply, so the back-scroll goes past the cutoff by
  `window.__TS_CHANNEL_GRACE_DAYS` (default 30) before it stops.
- Each record also carries `chat_id` (the channel thread id, so two "General"
  channels never merge), `is_reply`, `thread_root_epoch` and `reply_to`. The
  speakers file drops unknown fields, so reply text starts with
  `(reply to <author>, post of <ISO>)`; set `window.__TS_REPLY_PREFIX = false` to
  turn that off.

Verified on 2026-10-21 against a 6-message thread (3 replies hidden) plus a
post with one reply: all 8 messages, with the right authors.

## Meeting transcripts

[`teams-transcript-scraper.js`](teams-transcript-scraper.js) exports the
Transcript panel of a meeting's Recap page for `whosaid teams ingest-transcript`.

### Where the transcript lives

The panel is not in the Teams page. It is a cross-origin SharePoint iframe
(the "Recap" frame, a `*.sharepoint.com` URL), so evaluate the scraper in that
frame. Over CDP, list targets with iframes included and drive the iframe's own
target id; a script in the Teams top frame cannot reach into it.

### Running it

1. Open the meeting's Recap in Teams web and select the Transcript tab.
2. In the **Teams top frame**, evaluate `TOP_FRAME_SNIPPET` (in the comment at the
   end of the scraper). It returns `{title, scheduled, start_iso}` from the recap
   header and `document.title`. Set the result as `window.__TT_MEETING` in the
   iframe, or pass `--start` at ingest instead.
3. In the **Recap iframe**, evaluate the whole scraper. It installs
   `window.__tt` and runs detached; poll until `window.__tt.done`, and check
   `window.__tt.error` and `window.__tt.result.warning`.
4. Save `JSON.stringify(window.__tt.result)` and ingest it:

```bash
whosaid teams ingest-transcript transcript.json --into ~/meetings --tz America/Los_Angeles
```

Options, set on `window` before evaluating: `__TT_METHOD` (`"fiber"` or
`"scroll"`, to force one path), `__TT_SETTLE_MS` (scroll settle, default 450).

### Output

```json
{"kind": "teams-transcript", "method": "fiber", "transcript_id": "e64e...", "locale": "en-us",
 "recap_url": "https://...sharepoint.com/...", "expected": 476,
 "meeting": {"title": "Weekly Sync", "scheduled": "Monday, October 12, 2026 6:30 AM - 7:30 AM",
             "start_iso": "2026-10-12T13:30:00.000Z"},
 "entries": [{"idx": 4, "entry_id": "...", "type": "Text", "speaker": "Doe, Jane",
              "speaker_id": "...", "start_s": 32, "end_s": 35, "text": "...", "lang": "en-us"}]}
```

`type` is `Text`, `StartEvent` or `StopEvent` on the fiber path, and `Text` or
`Event` on the scroll path. `start_s` and `end_s` are seconds from the meeting
start. A `warning` appears when the entry count differs from `expected`.

### DOM contract (the virtualization)

- **Scroller:** `#scrollToTargetTargetedFocusZone`. **List:**
  `.ms-List[role="list"]` inside `#OneTranscript` (Fluent UI v8 `List`).
- **Virtualized:** only about 30 `.ms-List-cell[data-list-index]` cells are
  mounted at once, so a single DOM read sees a slice.
- **Primary path (fiber):** walk the list element's `__reactFiber$*` `.return`
  chain to the props holding `items`. That array is every entry
  (`speakerDisplayName`, `speakerId`, `entryId`, `type`, `timestamp` as an ISO
  8601 duration such as `PT1M5S`, `endTime`, `text`, `spokenLanguageTag`). It
  reads without scrolling, so the panel does not move. `#OneTranscript`'s props
  carry `transcriptId`.
- **Fallback (scroll):** jump to the top, then step the scroller by about 0.85 x
  `clientHeight`, harvesting every frame into a map keyed by `#sub-entry-N`'s
  `aria-posinset`. Stop when the scroller has not moved for 3 frames (the
  bottom), then restore the original `scrollTop`. Speaker and offset come from `#entry-N[role="group"]`'s
  `aria-label` ("Doe, Jane 1 minute 5 seconds"). Event rows have a blank label.
- **Completeness:** `aria-setsize` on any `#sub-entry-N` is the total, recorded as
  `expected`.
