# Model survey, October 2026: transcription and diarization

Notes from an A/B of newer local models against whosaid 1.14.0 (`d7f9343`). Nothing here has
shipped; the TODO list at the end is what's still being considered.

## Setup

- Harness: [Synthetic-meeting diarization eval](diarize-eval.md). It scores WER from the ASR
  `.json` text, and DER, turn accuracy and names from the diarizer segments, so an ASR arm moves
  only WER and wall time. The diarization metrics of every ASR arm matched the baseline exactly.
- Meetings: `build.py --seed 32 --count 8 --profile hard` ("val-hard", 8 meetings, 0.5 h) and
  `realistic.py --seed 406 --count 4 --minutes 30-120 --roster in-m` (4 meetings, 3.5 h, six to
  eight same-accent voices, `--max-speakers 8`). Modes: blind and refs on val-hard, refs on
  realistic. A frozen `git archive` of HEAD for every arm, one arm at a time, on an M5 Max.
- TTS voices, not real people: treat every number as directional.
- Candidates came from the Open ASR Leaderboard (results CSV of 2026-10-17) and the sherpa-onnx
  release assets (`speaker-segmentation-models`, `speaker-recongition-models`).

## Transcription

| model | runtime | val-hard WER | wall, val-hard (16 runs) | wall, realistic (3.5 h) |
|---|---|---|---|---|
| whisper-large-v3-turbo (current default) | mlx-whisper | 2.4 to 4.2% | 258 s | 277 s |
| whisper-large-v3 (3.08 GB fp16) | mlx-whisper | 1.4% | 309 s | 512 s |
| Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B (5.92 GB bf16) | mlx-qwen3-asr 0.4.4 | 1.2% | 253 s | 497 s |
| parakeet-tdt-0.6b-v2 (2.47 GB F32) | parakeet-mlx 0.5.3 | 10.2% | 165 s | 221 s |

- **Turbo is not stable on hard audio.** Across seven runs of the same meeting (m32-004) its WER
  ranged from 2.6% to 17.2%. large-v3 scored 1.4% every time, and Qwen3 1.1%.
- **Qwen3-ASR ties large-v3 here.** The leaderboard has it well ahead on real speech (5.75 vs
  7.84 average WER), but clean TTS cannot separate the two. Its aligner drops punctuation, so
  pause-only segmentation produces coarser segments that can absorb another speaker's short
  reply. The harness does not score that text-to-speaker merge.
- **Parakeet goes silent on noisy, overlapping stretches** (up to 56 s of nothing in m32-006).
  30 s chunking halves the error but still leaves 7 to 13% on the worst meetings.
- whosaid's merge (`build_turns` in `lib/diarize_sherpa.py`) uses segment timestamps, not word
  timestamps, so a non-Whisper model only has to emit a Whisper-shaped `{text, segments}` json.

## Diarization (speaker embedder)

`DIARIZE_EMB_NAME` swap only, thresholds unchanged.

| embedder | realistic DER / named wrong | val-hard DER blind / refs | wall vs current |
|---|---|---|---|
| nemo_en_titanet_small (current) | 18.2% / 2.4% | 15.1% / 12.4% | 1x |
| nemo_en_titanet_large (96 MB) | 15.2% / 1.1% | 18.6% / 14.1% | about 1.4x |
| wespeaker_en_voxceleb_resnet293_LM (109 MB) | not run | 55.3% / 70.3% | about 7x |
| wespeaker_en_voxceleb_CAM++_LM (27 MB) | 80.9% / - | 61.4% / 75.7% | about 1x |

Every threshold (`--absorb-threshold`, `--match-threshold`, `--ref-threshold` and the rest) was
tuned on TitaNet-small's cosine scale, so these rows measure "drops in", not "better model".
TitaNet-large's realistic gain comes mostly from one meeting (m406-001, 29.8% to 13.2% DER).
Switching embedders also means re-enrolling every saved voiceprint.

Not tested:

- Nemotron-3-Diarization (NVIDIA, Sep 2026): best published DER, up to 8 speakers, but NeMo only
  and no confirmed Apple Silicon runtime (sherpa-onnx issue #4006).
- pyannote speaker-diarization-community-1: gated on HF; needs an ONNX export of its
  segmentation model or the FluidAudio CoreML port.
- sherpa-onnx-reverb-diarization-v1/v2 segmentation: license unclear, possibly non-commercial.
- ReDimNet2 embedders: best published EER, but no sherpa-onnx asset.

## Eval bug found

`realistic.py` places voiced fragments (`phrases.json` spans) of a line but labels the turn with
the whole line's text, so a 2.25 s turn carries a 54-word reference. Every model scores about 72%
WER on realistic meetings for that reason alone. DER, turn accuracy and names are unaffected.

## TODO (considered, not decided)

- [x] Change the default ASR model to `mlx-community/whisper-large-v3-mlx` (`whosaid` default,
      README). Cost: about 1.85x transcription time on long meetings. Needs a release. Done: default is now large-v3; `--fast` or `WHOSAID_MODEL` restores turbo.
- [x] Fix realistic-meeting WER: give each turn the fragment's own text, or `None` so
      `score.py` skips WER for it. Fixed: realistic meetings now report no WER.
- [ ] Retune thresholds for `nemo_en_titanet_large` on the tune sets, then score on val and
      realistic, before judging it.
- [ ] Re-check Qwen3-ASR on real recordings (the TTS floor hides the gap), with punctuation-aware
      segmentation so short replies keep their own segment.
- [ ] Watch sherpa-onnx #4006 (Nemotron-3-Diarization) and #3497 (Sortformer) for a drop-in
      segmentation path.
