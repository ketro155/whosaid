#!/usr/bin/env python3
"""Tests for lib/ab_compare.py (whosaid ab). Synthetic fake output dirs only."""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))
import ab_compare as ab  # noqa: E402


def make_run(d: Path, base, segs, asr_segs, num=2, uncounted=(), model="m-turbo",
             wall=12.5, flags="", env=None, emb="nemo_en_titanet_small.onnx"):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{base}.rttm").write_text("".join(
        f"SPEAKER {base} 1 {s:.3f} {e - s:.3f} <NA> <NA> {lab} <NA> <NA>\n" for s, e, lab in segs))
    (d / f"{base}.diarization.json").write_text(json.dumps(
        {"base": base, "emb_model": emb, "num_speakers": num, "uncounted": list(uncounted)}))
    (d / f"{base}.json").write_text(json.dumps(
        {"text": "", "segments": [{"start": s, "end": e, "text": t} for s, e, t in asr_segs]}))
    (d / f"{base}.speakers.txt").write_text(
        f"# Speaker-labeled transcript: {base}\n"
        f"# Diarization: sherpa-onnx (pyannote segmentation-3.0 + NeMo TitaNet-small), local.\n")
    (d / "ab-run.log").write_text(f"whosaid: model={model}  lang=en\n")
    (d / "ab-run.json").write_text(json.dumps({"flags": flags, "env": env or {}, "wall_seconds": wall}))


ASR_A = [(0, 10, "the quick brown fox jumps over the lazy dog"),
         (10, 20, "we should ship the release on friday afternoon")]
ASR_B = [(0, 10, "the quick brown fox jumps over the lazy dog"),
         (10, 20, "we should skip the release on monday morning")]


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        # A: Alice 0-10, Bob 10-20 (named). B: Alice 0-10, Carol 10-16, anon 16-20.
        make_run(self.t / "a", "rec", [(0, 10, "Alice"), (10, 20, "Bob")], ASR_A,
                 num=2, flags="--speakers 2", env={"X": "1"}, wall=30)
        make_run(self.t / "b", "rec", [(0, 10, "Alice"), (10, 16, "Carol"), (16, 20, "SPEAKER_01")],
                 ASR_B, num=2, uncounted=["SPEAKER_01"], model="m-big", wall=45)
        self.rep = ab.compare_dirs(self.t / "a", self.t / "b", 8)

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_fields(self):
        a, b = self.rep["a"], self.rep["b"]
        self.assertEqual((a["num_speakers"], b["num_speakers"]), (2, 2))
        self.assertEqual(b["uncounted"], ["SPEAKER_01"])
        self.assertEqual(a["model"], "m-turbo")
        self.assertEqual(b["model"], "m-big")
        self.assertEqual(a["wall_seconds"], 30)
        self.assertIn("nemo_en_titanet_small.onnx", a["embedder"])
        self.assertIn("NeMo TitaNet-small", a["embedder"])
        self.assertEqual(a["flags"], "--speakers 2")
        self.assertEqual(a["top_shares"][0]["share"], 0.5)
        self.assertAlmostEqual(a["named_share"], 1.0)
        self.assertAlmostEqual(b["named_share"], 16 / 20)
        self.assertAlmostEqual(b["anonymous_share"], 4 / 20)
        md = ab.render_md(self.rep)
        for needle in ("counted speakers", "named talk share", "word agreement", "name agreement"):
            self.assertIn(needle, md)

    def test_word_agreement(self):
        w = self.rep["words"]
        # 17 words each; 3 differ (ship/skip, friday afternoon/monday morning)
        self.assertEqual((w["words_a"], w["words_b"]), (17, 17))
        self.assertEqual(w["agreement_pct"], round(100 * 2 * 14 / 34, 2))

    def test_disagreement_spans(self):
        spans = self.rep["words"]["spans"]
        self.assertEqual(len(spans), 2)
        by = {(s["a"], s["b"]): s for s in spans}
        self.assertIn(("friday afternoon", "monday morning"), by)
        big = by[("friday afternoon", "monday morning")]
        self.assertEqual(big["a_words"], 2)
        self.assertEqual(spans[0], big)  # largest first
        self.assertGreaterEqual(big["time"], 10)  # inside the second segment
        self.assertLess(by[("ship", "skip")]["time"], big["time"])

    def test_top_limits_spans(self):
        rep = ab.compare_dirs(self.t / "a", self.t / "b", 1)
        self.assertEqual(len(rep["words"]["spans"]), 1)
        self.assertEqual(rep["words"]["total_spans"], 2)

    def test_attribution_agreement(self):
        at = self.rep["attribution"]
        # both named: 0-16 (anon in B for 16-20 excluded); same name: Alice 0-10
        self.assertEqual(at["both_named_seconds"], 16.0)
        self.assertEqual(at["same_name_seconds"], 10.0)
        self.assertEqual(at["agreement_pct"], 62.5)
        lab = self.rep["labels"]
        self.assertFalse(lab["same_set"])
        self.assertEqual(lab["only_a"], ["Bob"])
        self.assertEqual(lab["only_b"], ["Carol"])
        self.assertIn("label sets differ", ab.render_md(self.rep))

    def test_identical_dirs(self):
        rep = ab.compare_dirs(self.t / "a", self.t / "a", 8)
        self.assertEqual(rep["words"]["agreement_pct"], 100.0)
        self.assertEqual(rep["attribution"]["agreement_pct"], 100.0)
        self.assertEqual(rep["words"]["spans"], [])

    def test_no_log_gives_dash(self):
        for f in ("ab-run.log", "ab-run.json"):
            (self.t / "a" / f).unlink()
        rep = ab.compare_dirs(self.t / "a", self.t / "b", 8)
        self.assertIsNone(rep["a"]["wall_seconds"])
        self.assertIn("| wall time | - |", ab.render_md(rep))

    def test_compare_cli_writes_report(self):
        out = self.t / "out"
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            ab.main(["--compare", str(self.t / "a"), str(self.t / "b"), "-o", str(out)])
        self.assertIn("whosaid A/B report", buf.getvalue())
        self.assertTrue((out / "ab-report.md").exists())
        self.assertEqual(json.loads((out / "ab-report.json").read_text())["words"]["words_a"], 17)


STUB = """#!/bin/bash
# records env + argv, then writes the minimal output of a whosaid run
d=""; base=$(basename "$2"); base="${base%.*}"; base=$(printf '%s' "$base" | tr -c 'A-Za-z0-9_-' '_')
prev=""; for a in "$@"; do [[ "$prev" == "-o" ]] && d="$a"; prev="$a"; done
{ echo "DB=${WHOSAID_SPEAKER_DB:-}"; echo "WS=${WHOSAID_WORKSPACE:-}"; echo "EXTRA=${AB_EXTRA:-}"
  echo "ARGV=$*"; } > "$d/stub-env.txt"
if [[ -n "${WHOSAID_SPEAKER_DB:-}" ]]; then echo '{"speakers": ["mutated"]}' > "$WHOSAID_SPEAKER_DB"; fi
echo "whosaid: model=stub-model" >&2
[[ -f "$d/$base.json" ]] || echo '{"segments":[{"start":0,"end":2,"text":"hello there"}]}' > "$d/$base.json"
echo hi > "$d/$base.txt"
printf 'SPEAKER %s 1 0.000 2.000 <NA> <NA> SPEAKER_00 <NA> <NA>\\n' "$base" > "$d/$base.rttm"
echo '{"num_speakers": 1, "uncounted": []}' > "$d/$base.diarization.json"
"""


class RunModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.stub = self.t / "stub-whosaid"
        self.stub.write_text(STUB)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IXUSR)
        self.audio = self.t / "my rec.m4a"
        self.audio.write_bytes(b"x")
        self.reg = self.t / "speakers.json"
        self.reg.write_text('{"speakers": []}')
        self.env = dict(os.environ, WHOSAID_AB_BIN=str(self.stub), WHOSAID_SPEAKER_DB=str(self.reg))

    def tearDown(self):
        self.tmp.cleanup()

    def run_ab(self, *args):
        return subprocess.run([sys.executable, str(REPO / "lib" / "ab_compare.py"), *args],
                              env=self.env, capture_output=True, text=True)

    def test_registry_copy_isolation(self):
        out = self.t / "o"
        r = self.run_ab(str(self.audio), "--a=--speakers 2", "--b", "", "-o", str(out),
                        "--env-b", "AB_EXTRA=hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        for arm in "ab":
            env = (out / arm / "stub-env.txt").read_text()
            db = [ln for ln in env.splitlines() if ln.startswith("DB=")][0][3:]
            self.assertNotEqual(db, str(self.reg))
            self.assertTrue(db.startswith(str(out.resolve())))
            self.assertTrue(any(ln.startswith("WS=") and "workspace" in ln for ln in env.splitlines()))
        self.assertIn("EXTRA=hello", (out / "b" / "stub-env.txt").read_text())
        self.assertIn("EXTRA=\n", (out / "a" / "stub-env.txt").read_text())
        self.assertIn("--speakers 2", (out / "a" / "stub-env.txt").read_text())
        self.assertEqual(self.reg.read_text(), '{"speakers": []}')  # real registry untouched
        self.assertTrue((out / "ab-report.md").exists())
        self.assertIn("stub-model", (out / "ab-report.md").read_text())
        self.assertEqual(json.loads((out / "a" / "ab-run.json").read_text())["flags"], "--speakers 2")

    def test_keep_registry_writes(self):
        out = self.t / "o"
        r = self.run_ab(str(self.audio), "--a", "", "--b", "", "-o", str(out), "--keep-registry-writes")
        self.assertEqual(r.returncode, 0, r.stderr)
        env = (out / "a" / "stub-env.txt").read_text()
        self.assertIn(f"DB={self.reg}\n", env)
        self.assertIn("mutated", self.reg.read_text())  # the stub wrote through: not isolated

    def test_same_asr_copies_and_reuses(self):
        out = self.t / "o"
        r = self.run_ab(str(self.audio), "--a=--speakers 2", "--b", "", "-o", str(out), "--same-asr")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--reuse-asr", (out / "b" / "stub-env.txt").read_text())
        self.assertNotIn("--reuse-asr", (out / "a" / "stub-env.txt").read_text())
        self.assertTrue((out / "b" / "my_rec.json").exists())
        self.assertTrue((out / "b" / "my_rec.txt").exists())

    def test_same_asr_refused_with_model_flags(self):
        for bflag in ("--b=--fast", "--b=-m foo", "--b=--accurate --speakers 2", "--b=--model x"):
            out = self.t / "r"
            r = self.run_ab(str(self.audio), "--a", "", bflag, "-o", str(out), "--same-asr")
            self.assertNotEqual(r.returncode, 0, bflag)
            self.assertIn("--same-asr", r.stderr)
            self.assertFalse((out / "a").exists(), "refusal must happen before any run")
        r = self.run_ab(str(self.audio), "--a", "", "--b", "", "-o", str(self.t / "r2"),
                        "--same-asr", "--env-b", "WHOSAID_MODEL=x")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
