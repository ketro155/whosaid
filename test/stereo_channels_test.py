#!/usr/bin/env python3
"""
Offline test for lib/stereo_channels.py (mic-vs-app attribution for
`whosaid record --app` stereo files). Synthetic signals, plus one tiny tagged
stereo file built with ffmpeg to cover tag detection and decoding (skipped
without ffmpeg). No models, no network.

Run:
    uv run --with numpy python test/stereo_channels_test.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import stereo_channels as sc  # noqa: E402

SR = sc.SAMPLE_RATE
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def tone(seconds, db, seed):
    r = np.random.RandomState(seed)
    x = r.randn(int(seconds * SR)).astype(np.float32)
    x /= np.sqrt(np.mean(x ** 2))
    return x * (10 ** (db / 20))


# A 12 s call: 0-4 s local talker (mic -25 dB, app silent), 4-8 s remote talker
# (app -20 dB, bleeding into the mic at -45 dB), 8-12 s a second remote talker.
left = np.concatenate([tone(4, -25, 1), tone(4, -45, 2), tone(4, -45, 3)])
right = np.concatenate([np.zeros(4 * SR, np.float32), tone(4, -20, 4), tone(4, -22, 5)])

# ---- turn_side ----
check(sc.turn_side(left, right, 0.5, 3.5) == "local", "mic-only turn is local")
check(sc.turn_side(left, right, 4.5, 7.5) == "remote", "app turn with mic bleed is remote")
quiet = np.zeros(SR, np.float32)
check(sc.turn_side(quiet, quiet, 0.0, 1.0) is None, "silent turn has no side")

# ---- cluster_sides ----
segs = [
    {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
    {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_00"},
    {"start": 4.0, "end": 8.0, "speaker": "SPEAKER_01"},
    {"start": 8.0, "end": 12.0, "speaker": "SPEAKER_02"},
]
sides = sc.cluster_sides(segs, left, right)
check(sides["SPEAKER_00"]["side"] == "local", f"SPEAKER_00 local: {sides['SPEAKER_00']}")
check(sides["SPEAKER_01"]["side"] == "remote", "SPEAKER_01 remote")
check(sides["SPEAKER_02"]["side"] == "remote", "SPEAKER_02 remote")
check(sides["SPEAKER_00"]["local_s"] == 4.0, "local seconds counted")

# A blend cluster split evenly across sides is mixed.
mixed = sc.cluster_sides([{"start": 0.0, "end": 2.0, "speaker": "X"},
                          {"start": 4.0, "end": 6.0, "speaker": "X"}], left, right)
check(mixed["X"]["side"] == "mixed", f"blend is mixed: {mixed['X']}")

# ---- apply_sides ----
names = {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "Owner", "SPEAKER_02": "SPEAKER_02"}
info = sc.apply_sides(sides, names, "Owner")
check(names["SPEAKER_01"] == "SPEAKER_01", "a remote cluster named as the owner is undone")
check(info["owner_refused"] == ["SPEAKER_01"], "refusal is recorded")
check(names["SPEAKER_00"] == "Owner", "the single unnamed local cluster becomes the owner")
check(info["owner_named"] == "SPEAKER_00", "naming is recorded")

names = {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "Bob", "SPEAKER_02": "SPEAKER_02"}
info = sc.apply_sides(sides, names, None)
check(names == {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "Bob", "SPEAKER_02": "SPEAKER_02"},
      "no owner: names untouched")
check(info["owner_named"] is None and info["owner_refused"] == [], "no owner: nothing recorded")

two_local = {"A": {"side": "local"}, "B": {"side": "local"}, "C": {"side": "remote"}}
names = {"A": "A", "B": "B", "C": "C"}
sc.apply_sides(two_local, names, "Owner")
check(names == {"A": "A", "B": "B", "C": "C"}, "two local clusters (a room): no owner guess")

names = {"A": "Owner", "B": "B"}
sc.apply_sides({"A": {"side": "local"}, "B": {"side": "local"}}, names, "Owner")
check(names == {"A": "Owner", "B": "B"}, "owner already placed locally: no second owner")

# ---- _runs ----
L_, R_ = "local", "remote"
check(sc._runs([L_] * 10 + [None] * 2 + [R_] * 3 + [L_] * 10) == [(L_, 0, 25)],
      "a short remote blip inside local speech is absorbed")
check(sc._runs([L_] * 8 + [R_] * 8) == [(L_, 0, 8), (R_, 8, 8)], "two real runs stay split")
check(sc._runs([None] * 4) == [(None, 0, 4)], "an all-silent turn is one sideless run")

# ---- split_by_side: the mono diarizer merged the local talker into a remote cluster ----
merged = [
    {"start": 0.0, "end": 8.0, "speaker": "SPEAKER_00"},   # local 0-4 + remote 4-8 in one turn
    {"start": 8.0, "end": 12.0, "speaker": "SPEAKER_01"},
]
new, moved = sc.split_by_side(merged, left, right)
check(moved is not None and moved["to"] == "SPEAKER_02" and moved["from"] == ["SPEAKER_00"],
      f"local run moved to a new cluster: {moved}")
check(abs(moved["seconds"] - 4.0) < 0.21, f"about 4 s moved: {moved}")
by = {}
for x in new:
    by.setdefault(x["speaker"], 0.0)
    by[x["speaker"]] += x["end"] - x["start"]
check(abs(by["SPEAKER_02"] - 4.0) < 0.21 and abs(by["SPEAKER_00"] - 4.0) < 0.21,
      f"seconds split by side: {by}")
check(sorted(x["start"] for x in new) == [x["start"] for x in new], "segments stay in time order")

# An existing mostly-local cluster receives the runs instead of a new one.
with_local = [
    {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_05"},
    {"start": 2.0, "end": 8.0, "speaker": "SPEAKER_00"},
    {"start": 8.0, "end": 12.0, "speaker": "SPEAKER_01"},
]
new, moved = sc.split_by_side(with_local, left, right)
check(moved and moved["to"] == "SPEAKER_05", f"runs go to the existing local cluster: {moved}")

# Clean clusters: nothing moves, the input comes back unchanged.
same, moved = sc.split_by_side(segs, left, right)
check(moved is None and same is segs, "already-separated clusters are untouched")

# Two local clusters (a room): no guess.
room = [{"start": 0.0, "end": 2.0, "speaker": "A"}, {"start": 2.0, "end": 4.0, "speaker": "B"},
        {"start": 0.5, "end": 8.0, "speaker": "C"}]
same, moved = sc.split_by_side(room, left, right)
check(moved is None, "two local clusters: nothing moved")

# ---- tag detection + decode on a real file ----
if shutil.which("ffmpeg") and shutil.which("ffprobe"):
    with tempfile.TemporaryDirectory() as d:
        lp, rp = Path(d, "l.raw"), Path(d, "r.raw")
        left.tofile(lp)
        right.tofile(rp)
        base = ["ffmpeg", "-v", "error", "-y",
                "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(lp),
                "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(rp),
                "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]", "-map", "[a]",
                "-c:a", "aac", "-b:a", "128k"]
        tagged, plain = Path(d, "tagged.m4a"), Path(d, "plain.m4a")
        subprocess.run(base + ["-metadata", f"comment={sc.CHANNEL_TAG}", str(tagged)], check=True)
        subprocess.run(base + [str(plain)], check=True)
        mono = Path(d, "mono.m4a")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tagged), "-ac", "1",
                        "-metadata", f"comment={sc.CHANNEL_TAG}", str(mono)], check=True)
        check(sc.is_tagged(str(tagged)), "tagged stereo file is detected")
        check(not sc.is_tagged(str(plain)), "untagged stereo file is ignored")
        check(not sc.is_tagged(str(mono)), "tagged mono file is ignored")
        names = {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01", "SPEAKER_02": "SPEAKER_02"}
        _, info = sc.attribute(str(tagged), segs, names, "Owner")
        check(info is not None and info["clusters"]["SPEAKER_00"]["side"] == "local",
              "decoded file: local talker found through AAC")
        check(info["clusters"]["SPEAKER_01"]["side"] == "remote", "decoded file: remote talker")
        check(names["SPEAKER_00"] == "Owner", "decoded file: owner named")
        names = {"SPEAKER_00": "SPEAKER_00"}
        check(sc.attribute(str(plain), segs, names, "Owner") == (segs, None)
              and names == {"SPEAKER_00": "SPEAKER_00"},
              "untagged file: no pass, no change")
else:
    print("SKIP: ffmpeg/ffprobe not found (tag detection + decode)")

print(f"PASS: stereo_channels_test ({CHECKS} checks)")
