#!/usr/bin/env python3
"""
Unit test for runner_up_placed_alone on placed-voice absorb records (GitHub issue #77).

runner_up_placed lets the meeting's other placed clusters compete on top of every
enrolled voiceprint, so an enrolled voice ABSENT from the meeting can win a blend turn
and hide the partner's cluster. runner_up_placed_alone scores the same turns against
ONLY the other voices' placed-cluster centroids. Recorded only: names never change.
Synthetic one-hot embeddings; no models.

Run:
    uv run --with numpy --with mcp python test/placed_alone_test.py
"""

import sys
from pathlib import Path

import numpy as np

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import diarize_sherpa as d  # noqa: E402

E = np.eye(64, dtype=np.float32)
THRESH = 0.70


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


# P (e0) placed via SPEAKER_00, Q (e1) placed via SPEAKER_01, F (e7) enrolled but absent.
C00 = unit(E[0] + 0.1 * E[5] + 0.6 * E[9])   # P's own cluster, a close sibling of 02
C01 = unit(E[1] + 0.1 * E[6])                # Q's cluster in this meeting
C02 = unit(0.75 * E[0] + 0.66 * E[9])        # the cluster under test: 0.75 to P


def run(turns, durs, with_q=True):
    emb = {"SPEAKER_00": C00, "SPEAKER_02": C02}
    if with_q:
        emb["SPEAKER_01"] = C01
    ents = [{"name": n, "embedding": E[i].tolist()} for n, i in (("P", 0), ("Q", 1), ("F", 7))]
    t, spans = 0.0, []
    for dur in durs:
        spans.append((t, t + dur))
        t += dur + 1.0
    rep = []
    out = d.name_clusters(emb, 0.5, 0.85, ents, [], report=rep,
                          placed_absorb_threshold=THRESH,
                          turn_emb={"SPEAKER_02": turns}, turn_spans={"SPEAKER_02": spans})
    hit = [r for r in rep if r["pass"] == "absorb" and r["cluster"] == "SPEAKER_02"]
    assert len(hit) == 1, rep
    return out, hit[0]


PTURN = unit(0.6 * E[0] + 0.8 * E[9])        # a P turn: 0.6 to P, 0.92 to SPEAKER_00
# A blend turn: 0.60 to absent F's voiceprint, 0.45 to Q's, 0.51 to Q's cluster centroid.
_r = np.sqrt(1 - 0.1 ** 2 - 0.45 ** 2 - 0.6 ** 2)
BTURN = unit(0.1 * E[0] + 0.45 * E[1] + 0.6 * E[7] + _r * E[6])


def test_absent_voice_masks_runner_up_placed_but_not_alone():
    out, rec = run([PTURN] * 3 + [BTURN], durs=[3.0] * 4)
    # runner_up_placed: absent F's voiceprint wins the blend turn and hides Q's cluster
    assert rec["runner_up_placed"] == "F" and rec["runner_up_placed_sim"] is None, rec
    # placed alone: only Q's in-meeting cluster competes, and it takes the turn
    assert rec["runner_up_placed_alone"] == "SPEAKER_01", rec
    assert rec["runner_up_placed_alone_name"] == "Q", rec
    assert rec["runner_up_placed_alone_share"] == 0.25, rec           # 3 of 12 s
    assert abs(rec["runner_up_placed_alone_sim"]) < 1e-3, rec         # Q's cluster vs P
    # recorded only: the absorb still goes through
    assert rec["matched"] and out["SPEAKER_02"] == "P", (rec, out)


def test_seconds_weighted():
    _, rec = run([PTURN] * 3 + [BTURN], durs=[1.0, 1.0, 1.0, 9.0])
    assert rec["runner_up_placed_alone_share"] == 0.75, rec


def test_candidate_only_placed_voice_is_none():
    # no other voice placed: nothing competes, no crash, all four values empty
    out, rec = run([PTURN] * 3 + [BTURN], durs=[3.0] * 4, with_q=False)
    assert rec["runner_up_placed_alone"] is None, rec
    assert rec["runner_up_placed_alone_name"] is None, rec
    assert rec["runner_up_placed_alone_share"] == 0.0, rec
    assert rec["runner_up_placed_alone_sim"] is None, rec
    assert out["SPEAKER_02"] == "P", out


def test_candidates_own_clusters_never_compete():
    # P's own SPEAKER_00 scores every P turn at 0.92, yet never competes
    assert float(np.dot(PTURN, C00)) > 0.9
    out, rec = run([PTURN] * 4, durs=[3.0] * 4)
    assert out["SPEAKER_02"] == "P", out
    assert rec["runner_up_placed_alone"] is None, rec
    assert rec["runner_up_placed_alone_share"] == 0.0, rec


def test_turns_under_floor_do_not_win():
    # a turn scoring 0.30 against Q's cluster (under the 0.40 floor) wins nothing
    low = unit(0.3 * E[1] + np.sqrt(1 - 0.09) * E[20])
    _, rec = run([PTURN] * 3 + [low], durs=[3.0] * 4)
    assert rec["runner_up_placed_alone"] is None, rec
    assert rec["runner_up_placed_alone_share"] == 0.0, rec


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"{n} passed")
