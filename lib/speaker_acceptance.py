"""Duration-weighted identity gate for human-annotated held-out recordings.

Input intervals are seconds: {start, end, speaker}. A null truth speaker means
not enrolled, not permission to guess a name. Overlapping truth is refused so
overlap cannot silently inflate this single-speaker identity measurement.
"""
import argparse
import json
import math
import re
from pathlib import Path


def intervals(rows):
    result = []
    for row in rows:
        start, end = row["start"], row["end"]
        if (type(start) not in (int, float) or type(end) not in (int, float) or
                not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or
                row.get("speaker") is not None and not isinstance(row["speaker"], str)):
            raise ValueError("Invalid labeled interval")
        result.append({"start": start, "end": end, "speaker": row.get("speaker")})
    result.sort(key=lambda x: x["start"])
    if any(b["start"] < a["end"] for a, b in zip(result, result[1:])):
        raise ValueError("Overlapping annotations require a separately declared overlap evaluation")
    return result


def evaluate(manifest):
    def identity(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
            raise ValueError("Invalid recording identity")
        value = value.removeprefix("of_")
        if not value:
            raise ValueError("Empty canonical recording identity")
        return value
    enrolled = set(manifest["enrolled_names"])
    enrollment = {identity(x) for x in manifest["enrollment_recording_ids"]}
    seen = set()
    people = {name: {"meetings": set(), "known_seconds": 0., "assigned_seconds": 0.,
                     "correct_seconds": 0., "named_seconds": 0.} for name in enrolled}
    totals = {"known_seconds": 0., "assigned_known_seconds": 0., "correct_seconds": 0.,
              "named_seconds": 0., "false_identity_seconds": 0., "unknown_false_identity_seconds": 0., "unknown_seconds": 0.}
    for meeting in manifest["meetings"]:
        recording_id = identity(meeting["recording_id"])
        if recording_id in enrollment or recording_id in seen:
            raise ValueError("Enrollment leakage or duplicate held-out meeting")
        seen.add(recording_id)
        if meeting.get("annotation_status") != "human-confirmed":
            raise ValueError("Human-confirmed held-out annotations are required")
        truth, prediction = intervals(meeting["truth"]), intervals(meeting["prediction"])
        if not truth:
            raise ValueError("Empty annotation is not an accuracy result")
        start, end = meeting["evaluation_start"], meeting["evaluation_end"]
        if (start != truth[0]["start"] or end != truth[-1]["end"] or
                any(a["end"] != b["start"] for a, b in zip(truth, truth[1:])) or
                any(p["start"] < start or p["end"] > end for p in prediction)):
            raise ValueError("Evaluation window needs complete truth coverage and bounded predictions")
        for t in truth:
            duration = t["end"] - t["start"]
            known = t["speaker"] in enrolled
            if t["speaker"] is not None and not known:
                raise ValueError("Truth identity must be enrolled or explicitly null")
            totals["known_seconds" if known else "unknown_seconds"] += duration
            if known:
                people[t["speaker"]]["meetings"].add(recording_id)
                people[t["speaker"]]["known_seconds"] += duration
            for p in prediction:
                overlap = max(0, min(p["end"], t["end"]) - max(p["start"], t["start"]))
                # Cluster labels/UNASSIGNED are abstentions. An unfamiliar real
                # name is still an identity claim and must not escape scoring.
                named = bool(p["speaker"] and p["speaker"] != "UNASSIGNED" and
                             not p["speaker"].startswith("SPEAKER_"))
                if named:
                    totals["named_seconds"] += overlap
                    if known:
                        totals["assigned_known_seconds"] += overlap
                        people[t["speaker"]]["assigned_seconds"] += overlap
                    if p["speaker"] in people:
                        people[p["speaker"]]["named_seconds"] += overlap
                    if known and p["speaker"] == t["speaker"]:
                        totals["correct_seconds"] += overlap
                        people[t["speaker"]]["correct_seconds"] += overlap
                    else:
                        totals["false_identity_seconds"] += overlap
                        if not known:
                            totals["unknown_false_identity_seconds"] += overlap
    precision = totals["correct_seconds"] / totals["named_seconds"] if totals["named_seconds"] else 0
    coverage = totals["assigned_known_seconds"] / totals["known_seconds"] if totals["known_seconds"] else 0
    for person in people.values():
        person["meetings"] = sorted(person["meetings"])
        person["precision"] = person["correct_seconds"] / person["named_seconds"] if person["named_seconds"] else 0
        person["coverage"] = person["assigned_seconds"] / person["known_seconds"] if person["known_seconds"] else 0
    gates = {"three_heldout_meetings": len(seen) >= 3,
             "each_enrolled_name_in_two_meetings": bool(people) and all(len(p["meetings"]) >= 2 for p in people.values()),
             "each_enrolled_name_accuracy": bool(people) and all(p["precision"] >= .95 and p["coverage"] >= .8 for p in people.values()),
             "known_and_unknown_controls": totals["known_seconds"] > 0 and totals["unknown_seconds"] > 0,
             "identity_precision": precision >= .95,
             "known_coverage": coverage >= .80,
             "zero_unknown_false_identity": totals["unknown_false_identity_seconds"] == 0}
    return {**totals, "precision": precision, "coverage": coverage, "people": people, "gates": gates,
            "passed": all(gates.values())}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("manifest", type=Path)
    a = p.parse_args()
    try:
        result = evaluate(json.loads(a.manifest.read_text()))
        print(json.dumps(result, indent=2))
        return int(not result["passed"])
    except (ValueError, KeyError, TypeError):
        print(json.dumps({"passed": False, "error": "Invalid or unconfirmed held-out annotations"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
