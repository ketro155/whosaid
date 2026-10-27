# Imported message identity

Search keeps every original transcript turn and source pointer. Overlapping
Teams or Outlook imports can therefore be inspected independently without
counting the same source message several times in search results.

## How messages match

The strongest identity is the source kind, conversation ID, and message ID.
Import records can supply `chat_id` and `message_id`; the importer preserves
them in the `.diarization.json` sidecar. Different message IDs remain separate,
including messages with identical text or the same timestamp.

Legacy sidecars often contain a `mid` synthesized from `epoch_ms`. That value
is treated as a timestamp. Without a real message ID, identity uses the source
kind, conversation context, exact clock, speaker, and complete message text.
Conversation context is a conversation ID when available, otherwise the saved
chat name. Only whitespace and whitespace adjacent to punctuation are normalized.
Different words, clocks, speakers, or conversations remain separate. Missing
context, missing clocks, and ambiguous transcript matches do not establish a
cross-import identity. Folder dates and suffixes are not identity evidence.

## Confirming legacy conversation aliases

An export may save the same conversation under different display names. Confirm
the overlap by comparing complete messages, source clocks, and speakers across
the snapshots. One generic reply such as "Thanks" is insufficient evidence.
After confirmation, add an explicit local mapping to the workspace's
`whosaid.toml`:

```toml
[messages.conversation_aliases]
"Planning" = "confirmed-planning-conversation"
"Planning copied export" = "confirmed-planning-conversation"
```

Aliases apply only when a sidecar lacks a real conversation ID. Run the normal
index rebuild to refresh search output. Original transcripts and
sidecars remain unchanged. An alias confirms conversation context. When no real
message ID exists, messages must still match the exact source clock, speaker,
and normalized full body to merge.
Later repetitions remain independent messages.

The Python API exposes unconfirmed candidates without merging them:

```python
from message_identity import MessageIndex

messages = MessageIndex(workspace)
for candidate in messages.possible_duplicates:
    # Exact clock, speaker and long text, but different conversation contexts.
    assert candidate["confirmed"] is False
    print(candidate["message_ids"], candidate["provenance"])
```

This candidate list is intentionally conservative. It requires at least 80
characters of normalized text and is not a complete duplicate detector. Keep
workspace names, text, and candidate reports local if they contain private data.

## Quotes and source attribution

Search retains verbatim `text`. `assertion_text` contains the text left after
removing recognized blockquote lines or a supplied `quoted_text` span.
The importer saves this distinction before flattening messages into one line.
When a legacy flattened reply begins with a speaker/date/time attribution
header and the quote boundary cannot be recovered, `assertion_text` is empty
and `attribution_uncertain` is true. Such text is searchable but must not be
treated as the current author's promise.

Search JSON includes `message_id`, `provenance`, `assertion_text`, `quoted`, and
`attribution_uncertain`. Each provenance pointer identifies the original
meeting folder, file, transcript line, and timestamp. Filtering search to a
particular folder can still return that folder's copy.

## Resolving source occurrences

`MessageIndex.resolve_occurrence()` accepts source pointers and can resolve a
precise `HH:MM:SS` timestamp retained inside an action-item summary's brackets.
Even a bracket marked `~` must match an exact second and one unique source
message. There is no nearest-time matching. An action-item document line is
never interpreted as a transcript line. Unresolved or ambiguous pointers keep
their independent meeting identity.

`MessageIndex.occurrence_keys()` returns unique keys for consumers that count
recurrence: imported chat/mail messages count once across confirmed copies,
while audio transcripts count once per meeting. Repeated sentences within one
audio meeting share a key. A later distinct source message or a later audio
meeting can still contribute another key. Original source pointers remain
available through `MessageIndex.provenance()`.
