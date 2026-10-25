#!/usr/bin/env python3
"""Issue #98: `roll-up` refolds only the commitment folders whose
fingerprint changed (or are new / gone), keeps ids and curation, re-derives
the fields a stripped line contributed, and keeps the full refresh for a
[commitments] cues change, logging final ids there. Offline (difflib
matcher); run: uv run --quiet python test/rollup_incremental_test.py"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
WS_PY = REPO_DIR / "lib" / "workspace.py"

CHECKS = 0


class CheckFailed(Exception):
    pass


def check(cond: bool, msg: str) -> None:
    global CHECKS
    if not cond:
        raise CheckFailed(msg)
    CHECKS += 1


SELF = "Alice Example"
VENDOR = "the vendor"
BOSS = "Boss Example"

# Distinct enough that difflib never folds two of them together.
TOPICS = [
    "draft the quarterly budget proposal for finance",
    "migrate the billing database to the new cluster",
    "schedule the vendor security audit interview",
    "publish the onboarding handbook revision to the wiki",
    "rotate the production signing keys before friday",
    "interview two candidates for the analyst opening",
    "renegotiate the office lease renewal terms",
    "prepare the board slides on churn trends",
    "order replacement laptops for the support team",
    "archive the legacy ticket queue after the cutover",
    "write the incident retrospective for the outage",
    "benchmark the search cluster against last quarter",
]
SHARED = "I will send the vendor the signed statement of work"


def day(n: int) -> str:
    return f"2026-09-{n:02d}-0900"


def entry(text: str, cue: str = "i will", **extra) -> dict:
    return {"speaker": SELF, "speaker_role": "self", "text": text, "time": "00:01:00",
            "cue": cue, "negative": False, "priority": "normal", **extra}


def meeting(ws: Path, name: str, entries: list[dict]) -> None:
    d = ws / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "commitments.json").write_text(json.dumps(
        {"source": "heuristic", "speakers": [SELF, VENDOR], "min_words": 2,
         "roles": {SELF: "self", VENDOR: "vendor", BOSS: "boss"},
         "items": entries, "dropped": []}))
    (d / "teams.speakers.txt").write_text(
        f"# Role: {SELF} = self\n[00:01:00] {SELF}: hello\n[00:01:05] {VENDOR}: hi\n")


def touch_speakers(ws: Path, name: str) -> None:
    with (ws / name / "teams.speakers.txt").open("a") as fh:
        fh.write(f"[00:02:00] {VENDOR}: one more delta line\n")


def rollup(ws: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(WS_PY), "rollup", str(ws), *extra],
                          capture_output=True, text=True, timeout=300)


def corpus(ws: Path) -> dict:
    return json.loads((ws / "_commitments.json").read_text())


def by_text(data: dict, needle: str) -> dict:
    hits = [i for i in data["items"] if needle.lower() in i["text"].lower()]
    check(len(hits) == 1, f"exactly one item containing {needle!r}, got {len(hits)}")
    return hits[0]


def ids_by_text(data: dict) -> dict[str, str]:
    return {i["text"]: i["id"] for i in data["items"]}


def set_status(ws: Path, item_id: str, status: str) -> None:
    md = ws / "_COMMITMENTS.md"
    text = md.read_text()
    new = text.replace(f"**{item_id}** [open]", f"**{item_id}** [{status}]")
    check(new != text, f"_COMMITMENTS.md has an open line for {item_id}")
    md.write_text(new)


def logged_ids(out: str, kind: str) -> list[int]:
    return [int(n) for n in re.findall(rf"^whosaid:\s+{re.escape(kind)} CM-(\d+) ", out, re.M)]


def fresh(tmp: str, tag: str, n: int) -> Path:
    ws = Path(tmp) / tag
    ws.mkdir()
    (ws / "whosaid.toml").write_text("[search]\nembed = false\n")
    for k in range(n):
        entries = [entry(f"I will {TOPICS[k % len(TOPICS)]} batch {k}")]
        if k % 3 == 0:
            entries.append(entry(SHARED))  # cross-meeting duplicate
        meeting(ws, day(k + 1), entries)
    return ws


def test_one_changed_folder(tmp: str) -> None:
    # --- 1. one changed folder of N: N-1 skip, ids stable, progress lines.
    n = 30
    ws = fresh(tmp, "one", n)
    r = rollup(ws)
    initial_out = r.stderr + r.stdout
    check(r.returncode == 0, f"one: initial rc={r.returncode}: {r.stderr[-400:]}")
    before = corpus(ws)
    shared = by_text(before, SHARED)
    check(len(shared["occurrences"]) == 10, f"one: shared item deduped across 10 meetings "
          f"({len(shared['occurrences'])})")
    touch_speakers(ws, day(7))
    r = rollup(ws)
    out = r.stderr + r.stdout
    check(r.returncode == 0, f"one: delta rc={r.returncode}: {r.stderr[-400:]}")
    skips = out.count("commitments already folded (skipping)")
    check(skips == n - 1, f"one: {n - 1} folders skipped, got {skips}")
    check(f"refolding 1 changed commitment folder(s): {day(7)}" in out,
          "one: names the changed folder")
    check("replacing derived contributions" not in out, "one: no full refresh")
    check("folded 25/" not in out, "one: no progress line for a 1-folder delta")
    after = corpus(ws)
    check(ids_by_text(after) == ids_by_text(before), "one: every id unchanged")
    check(after["next_id"] == before["next_id"], "one: next_id unchanged")
    check(len(by_text(after, SHARED)["occurrences"]) == 10, "one: shared item keeps 10 occurrences")
    check(after["folded_meetings"] == before["folded_meetings"], "one: folded set unchanged")
    # A second run with nothing changed folds nothing.
    r = rollup(ws)
    check((r.stderr + r.stdout).count("already folded (skipping)") == n, "one: idle run skips all")
    # Progress lines of the initial full fold (checked last so a missing
    # progress line does not mask the skip-count verdict above).
    check("folded 25/30 commitment folder(s)" in initial_out, "one: progress line at 25 of 30")
    check("folded 30/30 commitment folder(s)" in initial_out, "one: final progress line")
    check(before.get("cues_fingerprint"), "one: cues_fingerprint persisted")


def test_curated_survives(tmp: str) -> None:
    # --- 2. curated status survives a refold of its own meeting; a new
    # line in that meeting mints from the persisted next_id.
    ws = fresh(tmp, "curated", 4)
    check(rollup(ws).returncode == 0, "curated: initial")
    before = corpus(ws)
    target = by_text(before, "batch 1")
    set_status(ws, target["id"], "done")
    check(rollup(ws).returncode == 0, "curated: reconcile run")
    meeting(ws, day(2), [entry(f"I will {TOPICS[1]} batch 1"),
                         entry("I will book the offsite venue for the spring planning week")])
    r = rollup(ws)
    out = r.stderr + r.stdout
    check(r.returncode == 0, f"curated: refold rc={r.returncode}")
    check(out.count("commitments already folded (skipping)") == 3, "curated: 3 of 4 skipped")
    after = corpus(ws)
    t = by_text(after, "batch 1")
    check(t["id"] == target["id"] and t["status"] == "done", f"curated: keeps id and done ({t['id']}, {t['status']})")
    venue = by_text(after, "offsite venue")
    check(venue["id"] == f"CM-{before['next_id']:03d}", f"curated: new item takes next_id ({venue['id']})")
    check(after["next_id"] == before["next_id"] + 1, "curated: next_id advanced by one")
    # Curation outlives a third run (source_values were refreshed, the
    # edit is still recorded as curated).
    touch_speakers(ws, day(2))
    check(rollup(ws).returncode == 0, "curated: third run")
    t = by_text(corpus(ws), "batch 1")
    check(t["status"] == "done" and t["curated"].get("status") == "done", "curated: still done, still curated")


def test_cue_rederived(tmp: str) -> None:
    # --- 3. a strong cue / boss priority from a removed line is re-derived down.
    ws = fresh(tmp, "cue", 3)
    x = "I will send the revised pricing sheet to the vendor"
    meeting(ws, day(1), [entry(x, cue="let me")])
    meeting(ws, day(2), [entry(x, cue="i'll send", priority="high",
                               requested_by=BOSS, requested_by_role="boss")])
    check(rollup(ws).returncode == 0, "cue: initial")
    it = by_text(corpus(ws), x)
    check((it["cue"], it["priority"], it["requested_by"]) == ("i'll send", "high", BOSS),
          f"cue: strong cue + boss priority folded in ({it['cue']}, {it['priority']})")
    meeting(ws, day(2), [entry(f"I will {TOPICS[5]} only")])
    r = rollup(ws)
    check(r.returncode == 0, f"cue: refold rc={r.returncode}")
    it2 = by_text(corpus(ws), x)
    check(it2["id"] == it["id"], "cue: id kept")
    check(it2["cue"] == "let me", f"cue: cue re-derived to the remaining weak one ({it2['cue']})")
    check(it2["priority"] == "normal", f"cue: boss priority gone with its line ({it2['priority']})")
    check(it2["requested_by"] == "" and it2["requested_by_role"] == "", "cue: requester gone with its line")
    check([o["meeting"] for o in it2["occurrences"]] == [day(1)], "cue: only day 1 left")
    check((it2["first_seen"], it2["last_seen"]) == (day(1), day(1)), "cue: span recomputed")
    check(it2["curated"] == {}, f"cue: re-derived fields are not mistaken for edits ({it2['curated']})")
    # Next run still treats them as derived (source_values refreshed).
    touch_speakers(ws, day(3))
    check(rollup(ws).returncode == 0, "cue: next run")
    check(by_text(corpus(ws), x)["curated"] == {}, "cue: still no phantom curation")


def test_removed_folder(tmp: str) -> None:
    # --- 4. a removed folder's occurrences vanish; uncurated orphan
    # dropped, curated orphan kept, shared item keeps the rest.
    ws = fresh(tmp, "removed", 7)
    check(rollup(ws).returncode == 0, "removed: initial")
    before = corpus(ws)
    orphan, kept_one = by_text(before, "batch 4"), by_text(before, "batch 5")
    set_status(ws, kept_one["id"], "done")
    check(rollup(ws).returncode == 0, "removed: reconcile")
    shutil.rmtree(ws / day(4))  # carries SHARED (k=3) and 'batch 3'
    shutil.rmtree(ws / day(5))  # 'batch 4' only
    shutil.rmtree(ws / day(6))  # 'batch 5' only, curated done
    r = rollup(ws)
    out = r.stderr + r.stdout
    check(r.returncode == 0, f"removed: rc={r.returncode}: {r.stderr[-300:]}")
    check("dropping 3 removed folder(s)" in out, "removed: logs the removed folders")
    check(out.count("commitments already folded (skipping)") == 4, "removed: 4 kept folders skipped")
    after = corpus(ws)
    gone = {day(4), day(5), day(6)}
    check(not any(o["meeting"] in gone for i in after["items"] for o in i["occurrences"]),
          "removed: no occurrence of a removed folder left")
    check(not any(i["id"] == orphan["id"] for i in after["items"]), "removed: uncurated orphan dropped")
    k = [i for i in after["items"] if i["id"] == kept_one["id"]]
    check(len(k) == 1 and k[0]["status"] == "done" and k[0]["occurrences"] == [],
          "removed: curated orphan kept with no occurrences")
    sh = by_text(after, SHARED)
    check(sh["id"] == by_text(before, SHARED)["id"] and len(sh["occurrences"]) == 2,
          f"removed: shared item keeps id and its 2 other meetings ({len(sh['occurrences'])})")
    check(set(after["source_fingerprints"]) == {day(i) for i in (1, 2, 3, 7)},
          "removed: fingerprints forget removed folders")
    check(after["next_id"] == before["next_id"], "removed: next_id never rewinds")


def test_cues_full_path_final_ids(tmp: str) -> None:
    # --- 5. a [commitments] cues change keeps the full refresh, and its
    # log names final ids only (never a provisional id >= next_id).
    ws = fresh(tmp, "cues", 4)
    check(rollup(ws).returncode == 0, "cues: initial")
    before = corpus(ws)
    # A new commitment in the EARLIEST meeting: the refold mints it a
    # provisional id below the later meetings' fresh ids (#74).
    meeting(ws, day(1), [entry(f"I will {TOPICS[0]} batch 0"), entry(SHARED),
                         entry("I will renew the vendor support contract before it lapses")])
    (ws / "whosaid.toml").write_text(
        "[search]\nembed = false\n\n[commitments]\nweak_cues = [\"let me\", \"maybe i\"]\n")
    r = rollup(ws)
    out = r.stderr + r.stdout
    check(r.returncode == 0, f"cues: rc={r.returncode}: {r.stderr[-300:]}")
    check("replacing derived contributions" in out, "cues: full refresh path taken")
    check("already folded (skipping)" not in out, "cues: nothing skipped on the full path")
    after = corpus(ws)
    old = ids_by_text(before)
    check(all(ids_by_text(after).get(t) == i for t, i in old.items()), "cues: old ids restored")
    live = {int(i["id"].split("-")[1]) for i in after["items"]}
    plus, eq = logged_ids(out, "+"), logged_ids(out, "=")
    check(plus, "cues: a '+' line was logged for the new item")
    check(all(v < after["next_id"] for v in plus + eq),
          f"cues: no logged id at or above next_id {after['next_id']} (+{plus} ={eq})")
    check(set(plus + eq) <= live, f"cues: every logged id exists in the corpus (+{plus} ={eq})")
    renew = by_text(after, "renew the vendor support")
    check(plus == [int(renew["id"].split("-")[1])], f"cues: only the genuinely new item logs '+' ({plus})")
    check(after["next_id"] == before["next_id"] + 1, "cues: next_id advances by one")


def test_legacy_corpus(tmp: str) -> None:
    # --- 6. a corpus written before cues_fingerprint takes the full path once.
    ws = fresh(tmp, "legacy", 3)
    check(rollup(ws).returncode == 0, "legacy: initial")
    data = corpus(ws)
    data.pop("cues_fingerprint")
    (ws / "_commitments.json").write_text(json.dumps(data))
    touch_speakers(ws, day(2))
    r = rollup(ws)
    check("replacing derived contributions" in r.stderr + r.stdout, "legacy: full refresh once")
    check(corpus(ws).get("cues_fingerprint"), "legacy: fingerprint now persisted")
    touch_speakers(ws, day(2))
    r = rollup(ws)
    check("refolding 1 changed commitment folder(s)" in r.stderr + r.stdout, "legacy: then incremental")


def test_rebuild(tmp: str) -> None:
    # --- 7. --rebuild still folds everything from scratch (empty folded
    # set, so neither the incremental nor the refresh path runs).
    ws = fresh(tmp, "rebuild", 3)
    check(rollup(ws).returncode == 0, "rebuild: initial")
    touch_speakers(ws, day(2))
    r = rollup(ws, "--rebuild")
    out = r.stderr + r.stdout
    check(r.returncode == 0, f"rebuild: rc={r.returncode}")
    check("already folded" not in out and "refolding" not in out
          and "replacing derived contributions" not in out, "rebuild: plain full fold")
    check(out.count("folding 2026-09-") == 3, "rebuild: all 3 folders folded")
    check(corpus(ws).get("cues_fingerprint"), "rebuild: fingerprint persisted")


def test_near_miss_kept(tmp: str) -> None:
    # --- 8. a review pair only the fold sees (difflib's ratio is order-
    # sensitive: 0.73 entry-vs-item, 0.56 in possible_duplicates' all-pairs
    # order) survives a run that refolds a different folder.
    a = "I will benchmark the copido zumadu kihigu vurade"
    b = "I will benchmark the cohupi mapocu fibita cepado"
    ws = fresh(tmp, "near", 3)
    meeting(ws, day(1), [entry(a)])
    meeting(ws, day(2), [entry(b)])
    check(rollup(ws).returncode == 0, "near: initial")
    data = corpus(ws)
    ia, ib = by_text(data, a)["id"], by_text(data, b)["id"]
    pair = f"{ia} ↔ {ib}"
    check(pair in (ws / "_COMMITMENTS.md").read_text(), f"near: fold-time pair {pair} listed")
    touch_speakers(ws, day(3))
    r = rollup(ws)
    check("refolding 1 changed commitment folder(s)" in r.stderr + r.stdout, "near: incremental run")
    check(pair in (ws / "_COMMITMENTS.md").read_text(), "near: pair still listed after the incremental run")
    # Refolding the meeting that produced the pair recomputes it, no duplicate.
    touch_speakers(ws, day(2))
    check(rollup(ws).returncode == 0, "near: refold of the pair's meeting")
    md = (ws / "_COMMITMENTS.md").read_text()
    check(md.count(pair) == 1, "near: pair listed once after its meeting refolds")
    check(len(corpus(ws)["near_misses"]) == 1, f"near: one persisted near miss ({corpus(ws)['near_misses']})")


def main() -> None:
    failed = []
    for fn in (test_one_changed_folder, test_curated_survives, test_cue_rederived,
               test_removed_folder, test_cues_full_path_final_ids, test_legacy_corpus,
               test_rebuild, test_near_miss_kept):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                fn(tmp)
                print(f"PASS {fn.__name__}")
            except Exception as e:  # noqa: BLE001  (a crash is a failing scenario too)
                failed.append(fn.__name__)
                kind = "" if isinstance(e, CheckFailed) else f"{type(e).__name__}: "
                print(f"FAIL {fn.__name__}: {kind}{e}")
    print(f"{'FAILED' if failed else 'OK'}: {CHECKS} checks, {len(failed)} failing scenario(s)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
