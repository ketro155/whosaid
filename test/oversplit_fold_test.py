#!/usr/bin/env python3
"""Over-split fix: a saturated auto count applies the substantive voice count.

A real 28.6-minute 2-person recording came out as 16 speakers: the auto count
saturated at the cap of 20, the plateau recovery abstained, and with no
--max-speakers the substantive voice count (4) was only hinted. Two of those 4
clusters were the first person in another register (centroid cosine 0.79 and
0.70 to them), each holding a >= 2 s turn so the brief fold skipped them.

Synthetic embeddings only (no models, no audio, no real voiceprints). The
fixture: two substantive voices (15 s turns), "minor" same-voice clusters
(2.5 s turns whose centroid sits at cosine ~0.64 to one voice, far enough that
the 0.58 average-linkage cut keeps them apart, near enough for
MINOR_ABSORB_GATE 0.60), short noisy 0.6 s turns that saturate the primary cut,
and one brief distinct guest so the plateau's short-turn guard abstains.

Run:
    uv run --with numpy python test/oversplit_fold_test.py
"""

import contextlib
import copy
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import diarize_sherpa as d  # noqa: E402

DIM = 192
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def unit(v):
    return v / (np.linalg.norm(v) + 1e-9)


@contextlib.contextmanager
def recovery_env(value):
    """Set (or clear, for None) WHOSAID_COUNT_RECOVERY for the block."""
    saved = os.environ.get("WHOSAID_COUNT_RECOVERY")
    if value is None:
        os.environ.pop("WHOSAID_COUNT_RECOVERY", None)
    else:
        os.environ["WHOSAID_COUNT_RECOVERY"] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("WHOSAID_COUNT_RECOVERY", None)
        else:
            os.environ["WHOSAID_COUNT_RECOVERY"] = saved


def near(rng, base, cos):
    """A unit vector at exactly cosine `cos` to unit `base`."""
    n = rng.normal(size=DIM)
    n = unit(n - float(n @ base) * base)
    return cos * base + np.sqrt(1 - cos ** 2) * n


def fixture(third_talk_turns: int = 0, minor_third: bool = False, seed: int = 7):
    """(X, durations, groups) where groups names each row's true source.

    third_talk_turns > 0 adds a distinct third voice with that many 15 s turns;
    minor_third adds a distinct third voice with only 4 x 2.5 s turns."""
    rng = np.random.default_rng(seed)
    channel = unit(rng.normal(size=DIM))
    n_voices = 3 if (third_talk_turns or minor_third) else 2
    bases = [unit(0.40 * channel + 0.60 * unit(rng.normal(size=DIM))) for _ in range(n_voices)]
    rows, durs, groups = [], [], []

    def add(v, dur, g):
        rows.append(unit(v))
        durs.append(dur)
        groups.append(g)

    for vi in range(2):
        for _ in range(40):
            add(bases[vi] + rng.normal(scale=0.041, size=DIM), 15.0, f"v{vi}")
    if third_talk_turns:
        for _ in range(third_talk_turns):
            add(bases[2] + rng.normal(scale=0.041, size=DIM), 15.0, "v2")
    if minor_third:
        for _ in range(4):
            add(bases[2] + rng.normal(scale=0.02, size=DIM), 2.5, "v2")
    # Two minor same-voice clusters of voice 0 (another register).
    for m in range(2):
        sub = near(rng, bases[0], 0.64)
        for _ in range(4):
            add(sub + rng.normal(scale=0.02, size=DIM), 2.5, "v0")
    # Short noisy turns of both voices, below the brief fold's gate: they
    # saturate the primary cut (each opens a cluster of its own).
    for i in range(60):
        add(near(rng, bases[i % 2], 0.30), 0.6, f"v{i % 2}")
    # One brief distinct guest, far from everyone.
    add(unit(np.random.default_rng(1234).normal(size=DIM)), 0.6, "guest")
    return np.array(rows, dtype=np.float32), np.array(durs), groups


def segments(X, durations):
    out, t = [], 0.0
    for v, dur in zip(X, durations):
        out.append({"start": t, "end": t + float(dur), "emb": [float(x) for x in v]})
        t += float(dur) + 0.5
    return out


def test_same_voice_minors_fold_to_two():
    X, dur, _ = fixture()
    with recovery_env(None):
        est = d.estimate_speakers(X, durations=dur)
    check(est["saturated"] is True, f"fixture must saturate: raw_k {est['raw_k']}")
    fb = est["fallback"]
    check(fb is not None and fb["method"] == "substantive-count", f"new fallback applies: {est}")
    check(est["k"] == 2 and fb["k"] == 2, f"two voices: {est}")
    check(fb["absorbed_minor"] == 2, f"both same-voice minors absorbed: {fb}")
    check(est["suggested_max"] is None, "no hint once the count is applied")
    check(fb["reliable_turns"] + fb["excluded_short_turns"] == len(X), f"turn accounting: {fb}")
    warning = d.count_recovery_warning(est, 2, 2)
    check("substantive voices" in warning and "duration-filtered" not in warning, warning)
    # Determinism.
    with recovery_env(None):
        check(d.estimate_speakers(X, durations=dur)["fallback"] == fb, "stable")


def test_distinct_third_voice_survives():
    X, dur, _ = fixture(third_talk_turns=10)   # 150 s of 1370 s: ~11% of the talk
    with recovery_env(None):
        est = d.estimate_speakers(X, durations=dur)
    check(est["saturated"] and est["fallback"]["method"] == "substantive-count", f"{est}")
    check(est["k"] == 3, f"a third voice with >= 5% talk stays: {est}")
    # A third voice that is minor by talk but far from everyone also stays.
    X, dur, _ = fixture(minor_third=True)
    with recovery_env(None):
        est = d.estimate_speakers(X, durations=dur)
    check(est["k"] == 3 and est["fallback"]["absorbed_minor"] == 2,
          f"a distinct minor voice is not absorbed: {est}")


def test_brief_guest_keeps_own_label():
    X, dur, groups = fixture()
    with recovery_env(None):
        est = d.estimate_speakers(X, durations=dur)
        lab = d.cluster_estimated(X, est, dur)
    g = groups.index("guest")
    check(int((lab == lab[g]).sum()) == 1, "the guest turn has a label of its own")
    main = {int(x) for x in lab[[i for i, s in enumerate(groups) if s in ("v0", "v1") and dur[i] >= 1.0]]}
    check(len(main) == 2 and lab[g] not in main, f"substantive turns on 2 labels: {main}")
    # Every substantive turn lands on its own voice (purity).
    for v in ("v0", "v1"):
        labs = {int(lab[i]) for i, s in enumerate(groups) if s == v and dur[i] >= 1.0}
        check(len(labs) == 1, f"{v} substantive turns on one label: {labs}")
    # Through the real call site: the guest is a SPEAKER_NN of its own, and
    # being under 1 s of talk it is uncounted, so the headcount is 2.
    with recovery_env(None):
        segs, _emb, speakers, seg_est, _ = d.cluster_segments(segments(X, dur), -1)
    guest_start = segments(X, dur)[g]["start"]
    guest_sp = next(s["speaker"] for s in segs if s["start"] == guest_start)
    check(sum(1 for s in segs if s["speaker"] == guest_sp) == 1, "guest label is the guest only")
    unc = d.uncounted_clusters(segs, {}, speakers)
    check(guest_sp in unc, f"guest is uncounted: {guest_sp} not in {unc}")
    check(len(speakers) - len(unc) == 2, f"headcount 2: {len(speakers)} - {len(unc)}")
    check(seg_est["fallback"]["method"] == "substantive-count" and "labels" not in seg_est,
          f"{seg_est}")


def test_guest_with_inherited_fragments_stays_uncounted():
    # Real pipeline failure: un-embedded sub-0.5 s fragments inherit the nearest
    # embedded label in time, which pushed 0.6-0.8 s guest labels past 1.0 s of
    # talk, so they were counted (headcount 5 for 2 people).
    X, dur, groups = fixture()
    segs = segments(X, dur)
    g = groups.index("guest")
    end = segs[g]["end"]
    segs.append({"start": end + 0.05, "end": end + 0.45})   # no "emb": inherits by time
    segs.append({"start": end + 0.50, "end": end + 0.90})
    with recovery_env(None):
        out, _emb, speakers, est, _ = d.cluster_segments(copy.deepcopy(segs), -1)
    guest_sp = next(s["speaker"] for s in out if s["start"] == segs[g]["start"])
    talk = sum(s["end"] - s["start"] for s in out if s["speaker"] == guest_sp)
    check(talk > d.UNCOUNTED_MAX_TALK_SECONDS, f"fragments inherited past 1 s: {talk:.2f}")
    check(guest_sp in est["guest_labels"], f"guest label recorded: {est.get('guest_labels')}")
    # Control: by talk alone the guest would be counted.
    check(guest_sp not in d.uncounted_clusters(out, {}, speakers), "control: talk rule counts it")
    unc = d.uncounted_clusters(out, {}, speakers, always=est["guest_labels"])
    check(guest_sp in unc and len(speakers) - len(unc) == 2, f"guest stays uncounted: {unc}")
    # A guest label that gets a name is counted, like any named cluster.
    unc = d.uncounted_clusters(out, {guest_sp: "Guest"}, speakers, always=est["guest_labels"])
    check(guest_sp not in unc and len(speakers) - len(unc) == 3, "named guest is counted")
    # Legacy mode mints no guest labels and records none.
    with recovery_env("legacy"):
        est = d.cluster_segments(copy.deepcopy(segs), -1)[3]
    check("guest_labels" not in est, f"legacy records no guest labels: {est}")


def test_legacy_env_restores_hint_only():
    X, dur, _ = fixture()
    with recovery_env("legacy"):
        est = d.estimate_speakers(X, durations=dur)
        lab = d.cluster_estimated(X, est, dur)
    check(est["k"] == 20 and est["fallback"] is None, f"legacy keeps k at the bound: {est}")
    R = X[dur >= 1.0]
    merges = d._average_linkage_merges(1.0 - R @ R.T)
    old = d.suggest_max_speakers(R, merges, d.AGGLOM_THRESHOLD, 20)
    check(est["suggested_max"] == old == 4, f"legacy hint unchanged: {est['suggested_max']} / {old}")
    check(np.array_equal(lab, d.spherical_kmeans(X, 20)), "legacy assignment is plain k-means")
    with recovery_env(" LEGACY "):
        check(d.estimate_speakers(X, durations=dur)["k"] == 20, "env is trimmed, case-insensitive")
    with recovery_env("anything-else"):
        check(d.estimate_speakers(X, durations=dur)["k"] == 2, "other values use the new path")


def test_max_speakers_path_unchanged():
    X, dur, _ = fixture()
    results = []
    for mode in (None, "legacy"):
        with recovery_env(mode):
            results.append(d.estimate_speakers(X, durations=dur, max_speakers=12))
    for est in results:
        fb = est["fallback"]
        check(fb["method"] == "bounded-substantive-count" and est["k"] == 4 and fb["bound"] == 12
              and "absorbed_minor" not in fb and est["suggested_max"] is None,
              f"#75 bounded path unchanged: {est}")
    check(results[0]["fallback"] == results[1]["fallback"] and results[0]["k"] == results[1]["k"],
          "bounded path independent of WHOSAID_COUNT_RECOVERY")


def test_helper_edges():
    X, dur, _ = fixture()
    rel = dur >= 1.0
    R, Rd = X[rel], dur[rel]
    merges = d._average_linkage_merges(1.0 - R @ R.T)
    check(d.substantive_voice_count(R, Rd, merges, d.AGGLOM_THRESHOLD, 20) == (2, 2), "2 voices")
    check(d.substantive_voice_count(R, Rd, merges, d.AGGLOM_THRESHOLD, 20, min_speakers=5)[0] == 5,
          "raised to an explicit minimum")
    check(d.substantive_voice_count(R, Rd, merges, d.AGGLOM_THRESHOLD, 2)[0] is None,
          "count reaching the cap gives None")


if __name__ == "__main__":
    test_same_voice_minors_fold_to_two()
    test_distinct_third_voice_survives()
    test_brief_guest_keeps_own_label()
    test_guest_with_inherited_fragments_stays_uncounted()
    test_legacy_env_restores_hint_only()
    test_max_speakers_path_unchanged()
    test_helper_edges()
    print(f"PASS: {CHECKS} assertions")
