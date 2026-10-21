# Speaker-count eval

`test/count_eval/run_count_eval.py` measures the automatic speaker count
(`estimate_speakers` in `lib/diarize_sherpa.py`) against recordings whose real
headcount you know. It exists so that changes to the estimator (issue #59: the
threshold, the recovery ladder) can be checked on real meetings, including the
2-person case that #38 fixed, before they ship.

The eval runs offline on saved estimator inputs. It does not need the audio or
the models, and it runs in about a second per recording.

## The data stays local

A dump holds one voiceprint per speaker turn. That is biometric data about the
people in the meeting, so **dumps are never committed**. Keep them outside the
repository, or under `test/count_eval/data/`, which is gitignored. The repository
ships only the harness and a synthetic self-test (`test/count_eval_test.py`).

## 1. Dump the estimator inputs

Set `WHOSAID_DUMP_EMBEDDINGS` to a directory when you transcribe:

```sh
WHOSAID_DUMP_EMBEDDINGS=~/whosaid-count-eval whosaid standup.m4a
```

Each run that reaches the auto-count estimator writes `<base>.npz` to that
directory. The file holds `X` (the per-turn embeddings), `durations`, `raw_k`,
`threshold`, `cap`, `min_speakers` and `max_speakers`. With anchoring
(`--expected-speakers`) the estimator sees only the turns no anchor claimed, so
that file is `<base>-residual.npz`, and its label is the number of people who were
NOT named. Nothing is written when the variable is unset, or when `--speakers N`
fixes the count, because then the estimator does not run. Every diarization
runs the estimator except `--no-chunk`, which hands the whole file to sherpa's own
clustering. Recordings under 15 minutes go through the same per-turn path as one
window. The dump changes no other output.

## 2. Label

Write `labels.json` next to the dumps. It maps each base name to the true
headcount:

```json
{"standup-2026-10-01": 7, "1on1-2026-10-02": 2}
```

## 3. Run

```sh
uv run --with numpy python test/count_eval/run_count_eval.py ~/whosaid-count-eval
```

It prints one row per labeled recording, then the aggregate:

```text
base             true  raw_k  k   saturated  fallback_k  suggested_max  error
1on1-2026-10-02  2     21     2   True       2           -              0
standup-...      7     44     20  True       -           8              13

recordings: 2  MAE: 6.50  saturation rate: 100%  suggested_max MAE: 1.00
```

- `k` is the count the pipeline would use. `error` is `k - true`. MAE is the mean absolute error.
- `fallback_k` is the duration-filtered recovery count (#38), or `-` when recovery abstained.
- `fallback_k` is also the substantive voice count when a saturated run applied it
  (see below).
- `suggested_max` is the `--max-speakers N` hint (#59) that a saturated, unrecovered
  run puts in `count_warning`. Its MAE is over the rows that have one. By default it
  only appears when the substantive voices alone reach the cap, or with
  `WHOSAID_COUNT_RECOVERY=legacy`.

Options:

- `--threshold T` re-runs at another cosine cut. Compare a candidate value
  against the default on the same files.
- `--use-bounds` replays the `--min-speakers`/`--max-speakers` that each dump
  recorded. By default these are ignored, so the eval measures the auto count.
- `--labels PATH` reads the labels from somewhere else. `--json` prints machine-readable output.

## Saturated counts: the substantive voice count

When the auto count saturates (the primary cut hits the cap of 20), the plateau
recovery abstains and you set no `--max-speakers`, the count is already flagged as
untrustworthy. The estimator now applies the substantive voice count as `k`
instead of only hinting it. The fallback record has method `substantive-count`
and an `absorbed_minor` field.

How it counts, on the turns of at least 1 s:

1. Cut at the calibrated 0.58 and keep the clusters that have at least 3 turns.
2. Treat a kept cluster with under 5% of the kept clusters' total talk as minor
   (`MINOR_TALK_SHARE`). Fold it into the most similar cluster that has more talk
   when their centroid cosine is at least 0.60 (`MINOR_ABSORB_GATE`). Repeat until
   nothing folds.

Why 0.60: on this embedding model a stranger's turns score up to about 0.45
against a voice (p90), and the same person's turns score at least about 0.72
(p10). 0.60 sits between the two. On a real 28.6-minute 2-person recording that
used to come out as 16 speakers, the two minor clusters scored 0.79 and 0.70
against the first person and under 0.30 against the second. The count is now 2.

A distinct third voice still counts. A voice with 5% or more of the talk is
never minor, and a minor voice that is far from everyone is not folded. When
turns are assigned to speakers, a turn under 1 s that is far from every voice
(cosine under 0.40) keeps a label of its own. Under 1 s of talk such a label is
left out of the headcount, so a brief guest keeps their words without adding a
phantom speaker.

To get the old behavior back (`k` left at the bound, `--max-speakers N` only
hinted), set `WHOSAID_COUNT_RECOVERY=legacy`. The variable is read on every run.
An explicit `--max-speakers` takes the #75 bounded path either way.
