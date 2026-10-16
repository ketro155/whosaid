"""Mic-vs-app channel attribution for `whosaid record --app` recordings.

`whosaid record --app` saves stereo: left = the local mic, right = the captured
app (the other side of the call), and tags the file's comment with CHANNEL_TAG.
The app leg never carries the local voice (ScreenCaptureKit excludes it, and
call apps don't echo you back), while the mic hears the room plus some speaker
bleed. So per turn:

- the right channel well below the left  -> the local speaker (on the mic)
- otherwise                              -> a remote speaker (in the app)

The mono diarizer can still merge the local voice with a remote one (they are
mixed into one channel before clustering). So before sides are counted, each
turn is cut at side changes in 100 ms frames, and mic-only runs found inside a
mostly-remote cluster move to the local speaker: the one mostly-local cluster,
or a new cluster when none exists. With two or more local clusters (a room of
people) runs are not moved, since there is no telling whose they are.

A cluster whose talk time is mostly one side gets that side. Naming uses it two
ways, both only when an owner name is given (--me / WHOSAID_OWNER):

- a remote cluster can't be the owner: a registry match naming one is undone;
- if exactly one cluster is local and it is still unnamed, it becomes the owner.
  More than one local cluster (a room of people, or an over-split) is left alone.

Untagged or mono files are ignored, so nothing changes for any other input.
"""

from __future__ import annotations

import json
import subprocess

import numpy as np

CHANNEL_TAG = "whosaid:channels=mic,app"
SAMPLE_RATE = 16000
# Right (app) this far below left (mic) means the turn came in on the mic.
# Remote speech in the app sits around -20 dBFS and leaks into the mic 20-30 dB
# lower, so the margin splits the two cases with room on both sides.
LOCAL_MARGIN_DB = 15.0
# Below this on both channels the turn is too quiet to call.
SILENCE_DB = -60.0
# A cluster takes a side only when that side holds this share of its talk.
SIDE_SHARE = 0.7


def is_tagged(path: str) -> bool:
    """True for a 2-channel file carrying CHANNEL_TAG in its comment."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=channels:format_tags=comment",
             "-of", "json", path], capture_output=True, text=True, timeout=30).stdout
        info = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    channels = max((s.get("channels", 0) for s in info.get("streams", [])), default=0)
    comment = (info.get("format", {}).get("tags", {}) or {}).get("comment", "")
    return channels == 2 and CHANNEL_TAG in comment


def load_channels(path: str) -> tuple[np.ndarray, np.ndarray]:
    """Decode to 16 kHz float32 (left, right)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "2", "-ar", str(SAMPLE_RATE),
         "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"],
        capture_output=True, check=True).stdout
    both = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)
    return both[:, 0], both[:, 1]


def _db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * np.log10(rms) if rms > 1e-6 else -120.0


def turn_side(left: np.ndarray, right: np.ndarray, start: float, end: float) -> str | None:
    """'local', 'remote', or None when the turn is silent on both channels."""
    a, b = int(start * SAMPLE_RATE), int(end * SAMPLE_RATE)
    l_db, r_db = _db(left[a:b]), _db(right[a:b])
    if max(l_db, r_db) < SILENCE_DB:
        return None
    return "local" if l_db - r_db >= LOCAL_MARGIN_DB else "remote"


FRAME_S = 0.1
# Side runs shorter than this are absorbed by their neighbours, so a breath or a
# plosive never splits a turn.
MIN_RUN_S = 0.5


def frame_sides(left: np.ndarray, right: np.ndarray, start: float, end: float) -> list:
    """Side ('local' / 'remote' / None) for each FRAME_S frame of [start, end)."""
    out, t = [], start
    while t < end - 1e-9:
        out.append(turn_side(left, right, t, min(end, t + FRAME_S)))
        t += FRAME_S
    return out


def _runs(sides: list) -> list:
    """[(side, first_frame, n_frames)] with silent frames given their neighbour's
    side and runs under MIN_RUN_S merged into the longer neighbour."""
    known = [sd for sd in sides if sd]
    if not known:
        return [(None, 0, len(sides))] if sides else []
    filled, last = [], known[0]
    for sd in sides:  # silence takes the side before it (the first known side at the start)
        last = sd or last
        filled.append(last)
    runs: list = []
    for i, sd in enumerate(filled):
        if runs and runs[-1][0] == sd:
            runs[-1][2] += 1
        else:
            runs.append([sd, i, 1])
    min_frames = int(round(MIN_RUN_S / FRAME_S))
    while len(runs) > 1:
        i = min(range(len(runs)), key=lambda k: runs[k][2])
        if runs[i][2] >= min_frames:
            break
        j = i - 1 if i == len(runs) - 1 or (i > 0 and runs[i - 1][2] >= runs[i + 1][2]) else i + 1
        lo, hi = min(i, j), max(i, j)
        runs[lo:hi + 1] = [[runs[j][0], runs[lo][1], runs[lo][2] + runs[hi][2]]]
        # merging can leave two same-side runs adjacent
        k = 1
        while k < len(runs):
            if runs[k][0] == runs[k - 1][0]:
                runs[k - 1][2] += runs[k][2]
                del runs[k]
            else:
                k += 1
    return [tuple(r) for r in runs]


def split_by_side(segs: list, left: np.ndarray, right: np.ndarray) -> tuple:
    """Move mic-only runs out of mostly-remote clusters (see the module doc).

    Returns (new_segs, moved): moved = {"to": cluster, "seconds": s, "from": [...]}
    or None when nothing moved.
    """
    pieces = []  # (seg, side) per run
    for s in segs:
        runs = _runs(frame_sides(left, right, s["start"], s["end"]))
        if len(runs) <= 1:
            pieces.append((s, runs[0][0] if runs else None))
            continue
        for sd, first, n in runs:
            a = s["start"] + first * FRAME_S
            b = min(s["end"], s["start"] + (first + n) * FRAME_S)
            if b - a > 1e-6:
                pieces.append((dict(s, start=round(a, 3), end=round(b, 3)), sd))
    secs: dict = {}
    for p, sd in pieces:
        rec = secs.setdefault(p["speaker"], {"local": 0.0, "remote": 0.0})
        if sd:
            rec[sd] += p["end"] - p["start"]

    def side_of(sp):
        rec = secs[sp]
        tot = rec["local"] + rec["remote"]
        if tot <= 0:
            return None
        return "local" if rec["local"] / tot >= SIDE_SHARE else (
            "remote" if rec["remote"] / tot >= SIDE_SHARE else "mixed")

    local = [sp for sp in secs if side_of(sp) == "local"]
    if len(local) > 1:
        return segs, None
    if local:
        target = local[0]
    else:
        n = 0
        while f"SPEAKER_{n:02d}" in secs:
            n += 1
        target = f"SPEAKER_{n:02d}"
    moved_s, sources, out = 0.0, set(), []
    for p, sd in pieces:
        if sd == "local" and p["speaker"] != target and side_of(p["speaker"]) != "local":
            moved_s += p["end"] - p["start"]
            sources.add(p["speaker"])
            p = dict(p, speaker=target)
        out.append(p)
    if moved_s < MIN_RUN_S:
        return segs, None
    # Re-join consecutive pieces of the same speaker that a split left adjacent.
    merged: list = []
    for p in sorted(out, key=lambda x: x["start"]):
        if merged and merged[-1]["speaker"] == p["speaker"] and abs(merged[-1]["end"] - p["start"]) < 1e-3:
            merged[-1] = dict(merged[-1], end=p["end"])
        else:
            merged.append(p)
    return merged, {"to": target, "seconds": round(moved_s, 2), "from": sorted(sources)}


def cluster_sides(segs: list, left: np.ndarray, right: np.ndarray) -> dict:
    """Per cluster: seconds heard on each side and the side it takes (or 'mixed')."""
    acc: dict = {}
    for s in segs:
        side = turn_side(left, right, s["start"], s["end"])
        rec = acc.setdefault(s["speaker"], {"local_s": 0.0, "remote_s": 0.0})
        if side:
            rec[f"{side}_s"] += s["end"] - s["start"]
    for rec in acc.values():
        total = rec["local_s"] + rec["remote_s"]
        if total <= 0:
            rec["side"] = None
        elif rec["local_s"] / total >= SIDE_SHARE:
            rec["side"] = "local"
        elif rec["remote_s"] / total >= SIDE_SHARE:
            rec["side"] = "remote"
        else:
            rec["side"] = "mixed"
        rec["local_s"] = round(rec["local_s"], 2)
        rec["remote_s"] = round(rec["remote_s"], 2)
    return acc


def apply_sides(sides: dict, names: dict, owner: str | None) -> dict:
    """Adjust `names` in place from `sides`; return what changed, for the sidecar."""
    refused, named = [], None
    if owner:
        for sp, rec in sides.items():
            if rec["side"] == "remote" and names.get(sp) == owner:
                names[sp] = sp
                refused.append(sp)
        local = [sp for sp, rec in sides.items() if rec["side"] == "local"]
        owner_placed = any(names.get(sp) == owner for sp in sides)
        if len(local) == 1 and names.get(local[0]) == local[0] and not owner_placed:
            names[local[0]] = owner
            named = local[0]
    return {"tag": CHANNEL_TAG, "owner": owner or None, "clusters": sides,
            "owner_named": named, "owner_refused": refused}


def attribute(path: str, segs: list, names: dict, owner: str | None, log=None):
    """Run the whole pass on a tagged file.

    Returns (segs, info): the possibly re-split segments and the sidecar record.
    For anything but a tagged stereo file: (segs, None), nothing changed.
    `names` gains an entry for a newly created local cluster.
    """
    if not is_tagged(path):
        return segs, None
    left, right = load_channels(path)
    segs, moved = split_by_side(segs, left, right)
    if moved:
        names.setdefault(moved["to"], moved["to"])
    info = apply_sides(cluster_sides(segs, left, right), names, owner)
    info["moved"] = moved
    if log:
        if moved:
            log(f"channels: moved {moved['seconds']}s heard only on the mic from "
                f"{', '.join(moved['from'])} to {moved['to']}")
        for sp, rec in sorted(info["clusters"].items()):
            log(f"channels: {sp} {rec['side'] or 'silent'} (mic {rec['local_s']}s, app {rec['remote_s']}s)")
        if info["owner_named"]:
            log(f"channels: {info['owner_named']} is the only local voice -> '{owner}'")
        for sp in info["owner_refused"]:
            log(f"channels: {sp} speaks in the app, so it can't be '{owner}'; left unnamed")
    return segs, info
