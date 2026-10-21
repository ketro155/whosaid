# `whosaid ab`: compare two settings on your own recordings

You changed a setting (speaker count, an env knob, a model) and want to know whether the
result is better on YOUR audio, where there is no ground truth. `whosaid ab` runs whosaid
twice on one recording and reports where the two runs differ. It cannot say which arm is
right; it tells you where to look and how much changed.

## Usage

```bash
# Two settings, one recording (flags are ordinary whosaid flags; use --a=... for a value starting with --)
whosaid ab standup.m4a --a "--speakers 2" --b ""

# Diarization-only A/B: transcribe once, reuse A's transcript in B
whosaid ab standup.m4a --a "--speakers 2" --b "" --same-asr

# Per-arm environment (repeatable)
whosaid ab standup.m4a --a "" --b "" --env-b WHOSAID_COUNT_RECOVERY=legacy

# Compare two existing output dirs (each holds one whosaid run)
whosaid ab --compare run-old/ run-new/ -o compare-out/
```

Options: `-o DIR` (default `./whosaid-ab-<base>-<timestamp>`, printed), `--same-asr`,
`--env-a K=V`, `--env-b K=V`, `--keep-registry-writes`, `--top N` (disagreement spans, default 8).

## Isolation

By default each arm gets its own COPY of the active speaker registry
(`WHOSAID_SPEAKER_DB`, else `~/.config/whosaid/speakers.json`) and an unused, empty
`WHOSAID_WORKSPACE`, so an A/B never saves a voiceprint or ingests into a workspace. Your
real registry is never modified. `--keep-registry-writes` turns the copy off. An explicit
`--env-a WHOSAID_SPEAKER_DB=...` wins over the copy.

`--same-asr` refuses when B changes the ASR (`-m`, `--model`, `--fast`, `--accurate`, or
`WHOSAID_MODEL` in `--env-b`), since B would not use that model.

## Output

`DIR/a`, `DIR/b` (full whosaid outputs plus `ab-run.log`/`ab-run.json` with flags, env and
wall time), `DIR/ab-report.md` and `DIR/ab-report.json`. The report has:

- Per arm: flags, env, wall time, ASR model, embedder, counted speakers (`num_speakers`) and
  `uncounted` clusters, top-5 talk shares, named vs anonymous talk share.
- Transcript word agreement (%) and the largest disagreement spans with timestamps.
- Speaker attribution: share of time both arms give the same name, measured only where both
  have a real (non `SPEAKER_NN`) name, plus a note when the label sets differ. `SPEAKER_NN`
  numbers are not comparable across arms.

In `--compare` mode, wall time and model are read from `ab-run.json`/`ab-run.log` when
present, else shown as `-`.

## Reading it

- Same words, different speakers: a diarization-only change. Check the count and the
  anonymous share first; many small anonymous clusters often mean over-splitting.
- Name agreement well below 100%: some turns got a different name; listen to those spans
  (`whosaid samples`) before trusting either arm.
- A drop in wall time or uncounted clusters is only a win if the other numbers hold.
