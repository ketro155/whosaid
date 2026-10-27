# Plaud audio and OpenBrain summaries

Keep the Plaud recorder and its normal device-to-cloud upload. This fork downloads raw audio through the Plaud 4.0 workspace API, transcribes and separates speakers locally, recognizes previously enrolled voices, and generates evidence-checked meeting notes using the OpenBrain Capture template.

## Processing and privacy

Audio, voiceprints, and intermediate transcripts remain on the designated Mac. Only transcript text and explicitly supplied previous context go to the Codex CLI for summary drafting and independent verification. Codex must be signed in with ChatGPT; inherited API keys are not passed through. These requests consume the account's Codex allowance. No paid-API fallback is enabled. Codex is invoked without user configuration, shell tools, plugins, apps, or child agents, in an ephemeral read-only working directory.

Each recording has a stable Plaud file ID, a private original audio file and hash, and separate download, transcription, and summary checkpoints in SQLite. Replays skip completed recordings. A failed stage retries once and then quarantines that recording. Later stages do not redownload or retranscribe a successful earlier stage. Concurrent passes cannot own the same workspace. Vendor recordings and notes are never edited. Workspace credential refresh is the only POST request to Plaud.

Word timestamps preserve changes of speaker inside a Whisper segment. Words without diarization overlap and uncertain short fragments are rendered as UNASSIGNED. Original cluster labels and confidence diagnostics remain in the private sidecar. This avoids treating unreliable fragments as new known people; it does not establish a human-reviewed diarization accuracy score. Enroll or confirm a person's voice before expecting recurring real-name recognition. Names must never be inferred just from conversational order.

## Install on the processing Mac

Use a stable source checkout and an isolated Python environment:

```sh
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python mlx-whisper==0.4.3 sherpa-onnx==2.0.0 numpy==2.5.4
codex login status
```

Keep the Plaud session outside the checkout with file permissions 0600. Plaud 4.0 sessions contain workspace_id, token, tokenType, expiresAt, refreshToken, and refreshExpiresAt. Use a current session from the user's authorized Plaud login; never commit a token or embed one in a command argument. The client refreshes the workspace token before expiry and atomically stores any rotated refresh token. Revocation or expired refresh credentials require sign-in again. Legacy token-command configuration is also supported for accounts still using the old API.

Example private configuration:

```toml
[plaud]
since = "2026-10-27"
session_file = "/absolute/private/path/plaud-session.json"
max_per_pass = 3

[speech]
asr_model = "mlx-community/whisper-large-v3-turbo"
# Set a speaker-count hint only when independently known:
# max_speakers = 8

[summary]
codex_bin = "/absolute/path/to/codex"
timeout = 900
max_input_chars = 160000
# previous_context_file = "/absolute/private/path/previous-context.md"

[output]
summaries_dir = "/absolute/path/to/vault/Plaud Local"
```

Use an explicit since date to prevent accidental corpus backfills. file_ids can optionally restrict a test pass to exact recording IDs. Previous context is optional; the summary must omit unsupported cross-meeting changes rather than guess them. The source template lives at lib/templates/openbrain.md. Supported sections are Summary, Participants, Decisions, Changes Since Last Time, Action Items, Key Numbers & Data, Open Questions, and Risks & Concerns. Empty sections are omitted. Each item retains supporting source quotes in a private evidence JSON file; an independent verification call rejects unsupported claims. Failed drafts remain private diagnostic artifacts and are never exported as finished notes.

Run one pass using the isolated interpreter:

```sh
.venv/bin/python lib/plaud_pipeline.py sync --workspace /private/workspace --config /private/plaud.toml
```

After a live pass and replay verification, install the 15-minute launchd job on that Mac:

```sh
.venv/bin/python lib/plaud_pipeline.py install --workspace /private/workspace --config /private/plaud.toml
.venv/bin/python lib/plaud_pipeline.py status --workspace /private/workspace
```

The job writes freshness and recording-local logs, with no notifications. status returns nonzero when the last pass failed, quarantine needs attention, or freshness is older than 30 minutes. A quarantined recording can be retried by exact identity after its cause is fixed:

```sh
.venv/bin/python lib/plaud_pipeline.py retry --workspace /private/workspace --file-id EXACT_ID
```

The summaries_dir export contains verified Markdown notes only. Audio, voiceprints, logs, and rejected drafts stay outside the vault. Existing OpenBrain production ingestion is not rewritten or retired by installing this independent processing job.
