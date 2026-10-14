# Outlook mail scraper

[`outlook-mail-scraper.js`](outlook-mail-scraper.js) is the mail sibling of
[`../teams-scraper`](../teams-scraper): plain page JavaScript that runs inside an
already signed-in Outlook on the web tab (`outlook.cloud.microsoft` or
`outlook.office.com`) and hands whosaid a JSON export. whosaid itself stays
offline and only reads that JSON.

## Running it

1. Open Outlook on the web. Pick a folder, or run a search with a real Enter
   keypress (OWA ignores synthetic key events, so the scraper does not type the
   search for you; over CDP use `Input.insertText` plus `Input.dispatchKeyEvent`).
2. Optionally set `window.__OS_CUTOFF_DAYS = 30` (default 7) and
   `window.__OS_MAX_CONVS = 100` (default 200).
3. Evaluate the whole file in the tab. It installs `window.__os` and starts
   walking the message list without waiting for it to finish.
4. Poll with short calls until `window.__os.done` is `true`, watching
   `JSON.stringify(window.__os.progress)` (`opened`, `seen`, `skippedOld`,
   `failed`). A non-null `window.__os.error` means the walk failed.
5. Save `JSON.stringify(window.__os.result)` and ingest it:

```bash
whosaid outlook ingest raw_by_thread.json --into ~/meetings --tz America/Los_Angeles --self Alice_Example
```

Opening a thread marks it read, the same as clicking it by hand.

## Output

`window.__os.result` has the Teams scraper's raw_by_chat shape, keyed by the
thread subject with `RE:` / `FW:` prefixes stripped:

```json
{"Widget rollout": {"chat": "Widget rollout", "chat_id": "<conversation id>", "harvested": 2, "kept": 2,
  "reachedCutoff": false,
  "messages": [
    {"chat": "Widget rollout", "chat_id": "<conversation id>", "msg_id": "<message id>",
     "author": "Doe, Jane", "to": "Alice Example", "ts": "2026-09-24T16:05:06.000Z",
     "epoch": 1790265906000, "text": "License has been assigned."}]}}
```

OWA shows times to the minute only. `epoch` is that minute plus a stable
0 to 59999 ms offset hashed from the message id, so two mails sent in the same
minute do not collide on ingest's epoch dedup. The seconds in the transcript are
not real.

Ingest writes one folder per thread per local day holding `outlook.speakers.txt`
and `outlook.diarization.json` (`source.kind = "outlook-mail"`). Display names
map through `[outlook.names]` in `whosaid.toml`, falling back to `[teams.names]`,
and roles through `[outlook.roles]` / `[teams.roles]`.

## DOM contract (Outlook on the web, 2026-10)

- **Message list:** `[role="listbox"]` holding rows `[role="option"][data-convid]`.
  Virtualized: scroll its inner `.customScrollBar` element to mount more rows.
- **Row date:** the row `aria-label` ends with the list date (`2:06 PM` today,
  `Mon 1:14 PM` this week, `Thu 10/1` this year, `9/2/2026` older). Rows older
  than the cutoff are skipped without opening them.
- **Reading pane:** `#ConversationReadingPaneContainer`; an element labeled
  `<N> messages` gives the thread length, subject is `[id^="CONV_"][id$="_SUBJECT"]`.
- **One message:** `[aria-label="Email message"]` with `#MSG_<id>_FROM`
  (`Display Name<address>`), `#MSG_<id>_TO`, `[data-testid="SentReceivedSavedTime"]`
  (`Wed 9/2/2026 2:24 PM`) and `[aria-label="Message body"]`.
- **Quoted history:** OWA folds the quoted reply chain behind "Show original
  message", so the body is only the new text. External-sender banners and
  ticketing "Reply above this line" markers are dropped.

Scope: text bodies only; attachments, images and calendar items are skipped.
