# Plaud audio and OpenBrain summaries

Keep the Plaud recorder and its normal device-to-cloud upload. This fork downloads raw audio through the Plaud 4.0 workspace API, transcribes and separates speakers locally, recognizes previously enrolled voices, and generates evidence-checked meeting notes using the OpenBrain Capture template.

## Processing and privacy

Audio, voiceprints, and intermediate transcripts remain on the designated Mac. Only transcript text and explicitly supplied previous context go to the Codex CLI for summary drafting and independent verification. Codex must be signed in with ChatGPT; inherited API keys are not passed through. These requests consume the account's Codex allowance. No paid-API fallback is enabled. Codex is invoked without user configuration, shell tools, plugins, apps, or child agents, in an ephemeral read-only working directory.

Each recording has a stable Plaud file ID, a private original audio file and hash, and separate download, transcription, and summary checkpoints in SQLite. Replays skip completed recordings. A failed stage retries once and then quarantines that recording. Later stages do not redownload or retranscribe a successful earlier stage. Persisted unfinished recordings resume even if they leave the vendor inventory. Inventory errors remain failed passes while local checkpoints can progress. Concurrent passes cannot own the same workspace. Vendor recordings and notes are never edited.

## Supported intake pilot

Install the official packages in an isolated prefix on the processing Mac:

```sh
npm install --prefix "$HOME/.local/share/whosaid-plaud" --ignore-scripts --no-audit --no-fund @plaud-ai/cli@0.3.15 @plaud-ai/mcp@0.3.14
```

The adapter uses official MCP stdio JSON, not the CLI's truncated display table. Sign in once through the official MCP `login` tool. CLI and MCP use different OAuth clients and credential files; CLI login alone does not authenticate MCP. On a headless host, forward callback port 8199 before opening consent. The vendor login listener expires after two minutes. Credential refresh/storage belongs to the vendor package; the adapter does not read credentials or launch sign-in during an unattended pass. Telemetry is disabled in the adapter's child environment. No silent fallback to the web-session transport occurs.

Use this configuration for an isolated pilot workspace. Supply actual absolute paths for this host:

```toml
[plaud]
transport = "official-mcp"
since = "2026-10-27"
timezone = "America/New_York"
mcp_command = ["/absolute/path/to/node", "/absolute/prefix/node_modules/@plaud-ai/mcp/dist/index.js"]
mcp_timeout = 90
max_per_pass = 3
backlog_sla_seconds = 14400
# file_ids = ["exact-pilot-recording-id"]

[delivery]
mode = "prepare"
```

Inventory pages continue until an empty page. Repeated pages, malformed output and page-bound exhaustion fail instead of claiming completeness. `pending`, oldest pending age and overdue backlog are included in freshness. `sync` and `status` fail on overdue backlog. A successful empty inventory alone is not end-to-end fresh-arrival acceptance.

## Direct OpenBrain delivery pilot

`lib/openbrain_delivery.py` prepares a durable private outbox from verified summary/evidence files. Preparation performs no remote write. Replayed unchanged files reuse the same revision. Substantive changes create the next revision, including a deliberate return to older content; incidental local metadata does not create a new revision. Tampered summaries or mismatched transcripts cannot be queued.

```sh
.venv/bin/python lib/openbrain_delivery.py prepare --workspace /private/pilot --folder /private/pilot/meetings/exact-folder
.venv/bin/python lib/openbrain_delivery.py status --workspace /private/pilot
```

Sending requires a separate explicit configuration:

```toml
[delivery]
mode = "pilot"
file_ids = ["exact-approved-recording-id"]
url = "https://your-project.supabase.co/functions/v1/open-brain-mcp/mcp"
token_command = ["/absolute/path/to/scoped-oauth-token-provider"]
timeout = 90
```

```sh
.venv/bin/python lib/openbrain_delivery.py send --workspace /private/pilot --config /private/delivery.toml
```

The token provider must print an existing authorized OpenBrain OAuth access token. Do not use a database service-role key. The pilot writes one meeting-level observation per revision with origin `whosaid-local`, stable source identity and hashes; no transcript body or biometric sidecar is sent. Requests are frozen before transmission. The server must provide the ownership-aware `meeting_ownership` lookup and meeting capture contract in addition to durable request deduplication. A native-owned recording or an older server without that contract is held before capture. The client freezes the owner epoch and authoritative revision, then requires both the shared revision receipt and independent `get_context` readback of exact content/currentness. Every predecessor, including native section thoughts adopted on first local capture, must show confirmed old-to-new supersession before delivery is acknowledged. A new workspace can replay identical current content, but cannot overwrite an unknown remote revision; reconcile that revision first. A dropped or stalled response retains pending state. Pending delivery causes `delivery status` to exit nonzero, independently of intake freshness.

The HTTP receiver in the tests is an isolated protocol fixture. Passing it does not establish hosted authentication, server-side deduplication or production source exclusion. Those require an allowlisted live pilot before cutover. This client does not turn off the existing Plaud ingestion lane. Coordinate source authority and historical correction handling before enabling production delivery; supersession lineage alone is not proof every retrieval surface excludes historical facts.

To correct a completed local recording, preserve its existing raw audio and select the exact checkpoint to rerun:

```sh
.venv/bin/python lib/plaud_pipeline.py revise --workspace /private/pilot --file-id EXACT_ID --from-stage transcribed
```

Use `transcribed` after correcting the speaker transcript to regenerate the summary; use `downloaded` to rerun speaker processing from retained audio/ASR. A last good summary remains available until its replacement passes verification. Vendor transcript edits are deliberately not imported over the authoritative local transcript. Automatic detection of replacement vendor audio under an unchanged ID is not implemented.

## Held-out speaker acceptance

`lib/speaker_acceptance.py` evaluates private JSON manifests with enrolled names, enrollment recording IDs, and at least three independent recordings. Each meeting declares evaluation start/end, complete contiguous human-confirmed truth intervals, and prediction intervals in seconds. A null truth speaker denotes an unregistered voice; `UNASSIGNED` and `SPEAKER_*` predictions are abstentions. Overlap must be evaluated separately. Predictions outside declared evaluation windows are refused. `of_` ID aliases cannot bypass the enrollment exclusion.

```json
{"enrolled_names":["Example Person"],"enrollment_recording_ids":["enrollment-id"],"meetings":[{"recording_id":"heldout-id","annotation_status":"human-confirmed","evaluation_start":0,"evaluation_end":20,"truth":[{"start":0,"end":10,"speaker":"Example Person"},{"start":10,"end":20,"speaker":null}],"prediction":[{"start":0,"end":10,"speaker":"Example Person"},{"start":10,"end":20,"speaker":"UNASSIGNED"}]}]}
```

The one-meeting example cannot pass the three-meeting gate. The pilot requires 95% duration-weighted identity precision, 80% known-speaker assignment coverage and zero false known identities for unregistered speech in the evaluated windows. Every enrolled person must appear in at least two held-out meetings and meet the precision/coverage thresholds individually, so strong self-recognition cannot qualify an untested colleague. Report the declared cohort and window coverage; subset or excerpt checks are not whole-meeting accuracy. Unconfirmed labels, empty annotations, enrollment leakage and all-unknown predictions cannot establish acceptance.

```sh
.venv/bin/python lib/speaker_acceptance.py /private/human-confirmed-manifest.json
```

Word timestamps preserve changes of speaker inside a Whisper segment. Words without diarization overlap and uncertain short fragments are rendered as UNASSIGNED. Original cluster labels and confidence diagnostics remain in the private sidecar. This avoids treating unreliable fragments as new known people; it does not establish a human-reviewed diarization accuracy score. Enroll or confirm a person's voice before expecting recurring real-name recognition. Names must never be inferred just from conversational order.

## Install on the processing Mac

Use a stable source checkout and an isolated Python environment:

```sh
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python mlx-whisper==0.4.3 sherpa-onnx==2.0.0 numpy==2.5.4
codex login status
```

Keep the Plaud session outside the checkout with file permissions 0600. Plaud 4.0 sessions contain workspace_id, token, tokenType, expiresAt, refreshToken, refreshExpiresAt, and the originating deviceId. Use a separate authorized login for the automation, rather than sharing token rotation with an actively used browser session. Close the temporary login tab after securely transferring its session to the processing host. Never commit a token or embed one in a command argument. The client refreshes the workspace token before expiry and atomically stores any rotated refresh token. Revocation or expired refresh credentials require sign-in again. Legacy token-command configuration is also supported for accounts still using the old API.

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

## Ownership and transcription boundaries

Local processing is the intended authoritative Plaud producer after an explicit per-recording cutover. The shared database remains native-owned by default. Assignments are operator actions; the client never assigns or switches an owner. Separate workspaces/hosts use the same server-side revision receipt. Native ingestion must consult that assignment and its final capture must enforce the same epoch. Failure does not switch back to native. Sending old frozen requests without the new contract is refused.

Local ASR and local audio-based speaker recognition remain enabled. The official MCP raw `transaction` transcript can be read completely through `PlaudMcpClient.transcript`, with identity, page-count and cursor checks. `lib/plaud_transcript.py` evaluates timing without replacing the authoritative transcript or trusting vendor speaker names. The two-recording timing probe had no word timestamps and median utterances of 17.39 and 23.08 seconds; some utterances overlapped multiple locally detected speakers. These overlaps are diagnostics, not human ground truth. Reusing those words for local speaker attribution needs a separately tested alignment step; polished transcripts and native summaries are not substitute factual evidence.

### Automatic claim correction

Verification identifies each unsupported item's exact section and zero-based array index, with a specific reason. The application permits the repair model to rewrite or remove only those flagged items; supported, unflagged items retain their content and order. Repairs can correct citations, remove an invented rationale/program attribution, or preserve a stated figure with its measure explicitly unidentified. Every candidate must satisfy the template/evidence structure and pass a fresh verification of the entire summary before publication.

By default, two targeted correction rounds are allowed. If claims remain unsupported, the application removes only the items explicitly flagged by the latest review and verifies the remainder once more. It never publishes an empty core Summary, an unverified remainder, an edit outside the flagged set, or malformed verifier/repair output. The last good note stays in place if the bounded workflow cannot finish successfully.

```toml
[summary]
repair_rounds = 2
max_summary_seconds = 600
```

`repair_rounds` accepts 0–3. A fresh accepted draft uses two provider requests. The default repair path uses at most seven: draft, up to four whole-summary reviews and up to two repairs. Three configured repair rounds allow at most nine requests. A matching retained draft skips the initial generation request. An absolute deadline bounds both the Codex login-status check and model subprocess, with remaining time recomputed before each. The application also checks the budget before/after every provider request; exhaustion withholds publication.

Retained rejected drafts resume only when transcript hash, template hash, source passages, previous context, prompt policy, meeting date/identity and revision generation match exactly. Successful publication invalidates older rejected drafts; explicit revision increments the generation. Changed input starts a new draft. Delivery requires the exact meeting metadata used during verification; corrected dates require reverification. Legacy queued summaries without this binding cannot send until reverified and prepared again, and frozen outbox content must still match the verified note. Private evidence records the initial draft, each review, every patch/pruned item, provider usage and the final verified draft with exact source quotes. This audit is not inserted into the meeting note. Repair is driven by transcript evidence, not by web search or unsupported outside context. Source/model failures can still require review after the bounded attempts; correction does not disable factual verification.

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

## Production date policy

The approved production scope is recordings dated October 27, 2026 onward in America/New_York. The server owns this rule. Both producers register the vendor event timestamp before capture; registration cannot choose an owner or override an explicit assignment. Conflicting recording dates fail closed. Older recordings remain native-owned. Queued legacy native writes are checked against the same date policy inside atomic capture.

Production configuration uses `[delivery] mode = "production"`, `since = "2026-10-27"`, the production MCP URL and an explicit OAuth token command. Each scheduled sync prepares verified notes, sends the outbox and records pending delivery in freshness; pending delivery fails status. Production delivery checks the same local date boundary before registering a recording. The intake uses official MCP transport. A private confidential machine OAuth client supplies access tokens; its credential is stored outside the checkout with mode 0600. No database service-role credential is installed on the processing Mac.

To stop publication, unload the Studio launch agent. Preserve ownership and receipts: stopping local processing never silently transfers a meeting back to native. The last verified note remains available. Historical versions stay visible with supersession and current-head pointers.

## Speaker recognition shadow evaluation

`lib/speaker_turn_identity.py` is an offline candidate and is not imported by the production transcription or publication paths. It considers one interval at a time: at least four seconds, cosine similarity at least 0.50, runner-up margin at least 0.15, and no overlapping speaker. Invalid embeddings, duplicate profiles and model mismatches fail closed. `non_overlapping_spans` subtracts competing speech before the remaining spans are embedded and scored. A clear interval does not name its cluster or neighbouring short fragments.

Candidate profiles, audio excerpts, biometric features and human labels belong in a private evaluation directory, outside Git. Freeze the training recording identities before scoring. A different interval from the same recording is not an independent held-out recording. Unknown identity is not proof of an unregistered speaker: use human-confirmed unregistered voices for negative controls. Candidate-selected excerpts remain unlabelled until a human identifies them; proposal scores are not truth.

Run `python3 -m unittest discover -s test -p speaker_turn_identity_test.py` for the candidate mechanism's rejection controls. The existing `speaker_acceptance.py` gates still govern promotion: neither one recovered diagnostic interval nor a correct training-clip match establishes whole-meeting accuracy or recurring-name coverage. All production profiles and thresholds remain in force until the independent evaluation qualifies a change.

## Restricted private speaker pilot

The optional `[speaker_pilot]` hook runs after normal verified delivery. It admits only the first two completed SMART/FMDS recordings dated on or after its configured cutoff, pins source/profile/implementation hashes, and runs both identity arms twice. The candidate can name clear anonymous intervals for its two explicitly allowed identities; it preserves existing names and every source word. The original production registry is retained. The pilot has no summary-generation, vault-export or OpenBrain-delivery path. Candidate text and decisions stay in its separate private workspace pending human review.

`docs/speaker-shadow-gates.json` owns the pre-committed comparison gates. Two preflight recordings exercise the mechanism; they do not establish broad promotion quality. New pilot admissions are fixed, rather than replaced when an item is difficult. A changed policy, modified profile, incomplete result or altered snapshot fails closed. The intake status reports pilot failure separately and exits nonzero. A waiting or unreviewed pilot is not a quality pass. Disable the optional configuration to stop pilot comparisons while retaining normal delivery and private evidence.
