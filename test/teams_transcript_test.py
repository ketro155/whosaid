#!/usr/bin/env python3
"""
Offline test for lib/teams_transcript.py (Teams meeting transcripts, the Recap
"Transcript" panel scraped by contrib/teams-scraper/teams-transcript-scraper.js).

Builds a synthetic scraper result with placeholder names, ingests it into a temp
workspace, and asserts: one folder named from the meeting start in the workspace
zone; teams.speakers.txt round-trips through wsconfig.parse_turns with
meeting-offset stamps (a display name holding ':' included); Start/Stop events
kept in the sidecar, not as turns; the sidecar contract (source.kind,
transcript_id, creation_time, duration_seconds); [teams.names] / --self roles;
idempotent re-ingest on transcript_id; --start overriding the export; a
JSON-string payload (CDP savePath) accepted; clear errors for a missing start and
a wrong kind; and the real CLI (`./whosaid teams ingest-transcript`, then index
marking the folder text-only).

Run:  python3 test/teams_transcript_test.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
sys.path.insert(0, str(LIB))

import teams_transcript as tt  # noqa: E402
import wsconfig  # noqa: E402
from teams_chat import TeamsError  # noqa: E402

TZ = "America/Los_Angeles"
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}(?:-\d+)?$")
NOW = datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc)
CHECKS = 0

DOE = "Doe, Jane"
ROE = "Roe: Richard"        # display name containing ':'
SELF = "Self Example"


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def export(transcript_id="tid-1", start_iso="2026-10-12T13:30:00.000Z", extra=()) -> dict:
    entries = [
        {"idx": 0, "entry_id": "x/1", "type": "StartEvent", "speaker": DOE, "start_s": 3, "end_s": None, "text": ""},
        {"idx": 1, "entry_id": "x/2", "type": "Text", "speaker": DOE, "start_s": 3, "end_s": 5, "text": "Back in."},
        {"idx": 2, "entry_id": "x/3", "type": "Text", "speaker": ROE, "start_s": 32, "end_s": 35,
         "text": "the robots perform\n  as expected"},
        {"idx": 3, "entry_id": "x/4", "type": "Text", "speaker": SELF, "start_s": 3725, "end_s": 3730, "text": "Late point."},
        {"idx": 4, "entry_id": "x/5", "type": "Text", "speaker": DOE, "start_s": 40, "end_s": 41, "text": "   "},
        *extra,
        {"idx": 9, "entry_id": "x/9", "type": "StopEvent", "speaker": DOE, "start_s": 3800, "end_s": None, "text": ""},
    ]
    return {"kind": "teams-transcript", "method": "fiber", "transcript_id": transcript_id, "locale": None,
            "recap_url": "https://example.sharepoint.com/recap", "expected": len(entries),
            "meeting": {"title": "Weekly Sync", "start_iso": start_iso,
                        "scheduled": "Monday, October 12, 2026 6:30 AM - 7:30 AM"},
            "entries": [dict(e, lang="en-us") for e in entries]}


def write(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="teams-transcript-test-"))
    # no voice registry: roles come only from whosaid.toml and --self
    env = dict(os.environ, WHOSAID_SPEAKER_DB=str(tmp / "no-registry.json"))
    os.environ["WHOSAID_SPEAKER_DB"] = env["WHOSAID_SPEAKER_DB"]
    try:
        ws = tmp / "ws"
        ws.mkdir()
        (ws / "whosaid.toml").write_text(f'[teams.names]\n"{DOE}" = "Jane Doe"\n', encoding="utf-8")
        src = write(tmp / "t.json", export())

        r = tt.ingest(src, ws, tz_flag=TZ, self_name=SELF, now=NOW)
        check(r["created"] and r["folder"] == "2026-10-12-0630", f"folder from meeting start: {r}")
        check(DATE_DIR_RE.match(r["folder"]), "folder matches DATE_DIR_RE")
        check(r["turns"] == 3 and r["entries"] == 6, "blank text and events are not turns")
        folder = ws / r["folder"]

        text = (folder / tt.SPEAKERS_NAME).read_text(encoding="utf-8")
        check("# Source: ms-teams-transcript (Weekly Sync), meeting 2026-10-12 06:30 PDT" in text, "source header")
        check(f"# Role: {SELF} = self" in text, "--self role line")
        turns = wsconfig.parse_turns(text)
        check([t.t_str for t in turns] == ["00:00:03", "00:00:32", "01:02:05"], f"offset stamps: {turns}")
        check([t.speaker for t in turns] == ["Jane Doe", "Roe- Richard", SELF], "names mapped and ':' sanitized")
        check(turns[1].text == "the robots perform as expected", "whitespace collapsed")

        sc = json.loads((folder / tt.SIDECAR_NAME).read_text(encoding="utf-8"))
        s = sc["source"]
        check(s["kind"] == "teams-transcript" and s["transcript_id"] == "tid-1", "sidecar kind + id")
        check(s["creation_time"] == "2026-10-12T13:30:00Z" == s["meeting_start"], "creation_time is the start")
        check(s["duration_seconds"] == 3800 and s["locale"] == "en-us", "duration from last event, locale fallback")
        check([e["type"] for e in sc["events"]] == ["StartEvent", "StopEvent"], "events in sidecar")
        check(sc["names"] == {DOE: "Jane Doe", ROE: "Roe- Richard", SELF: SELF}, "names map")
        check(sc["roles"] == {SELF: "self"}, "roles")
        check([g["entry_id"] for g in sc["segments"]] == ["x/2", "x/3", "x/4"], "segments keep entry ids")

        # Re-ingest with one more entry: same folder, updated, role kept without --self.
        more = {"idx": 5, "entry_id": "x/6", "type": "Text", "speaker": ROE, "start_s": 50, "end_s": 52, "text": "Added."}
        write(src, export(extra=[more]))
        r2 = tt.ingest(src, ws, tz_flag=TZ, now=NOW)
        check(not r2["created"] and r2["folder"] == r["folder"] and r2["turns"] == 4, f"idempotent update: {r2}")
        check(sorted(p.name for p in ws.iterdir() if p.is_dir()) == [r["folder"]], "no second folder")
        sc2 = json.loads((folder / tt.SIDECAR_NAME).read_text(encoding="utf-8"))
        check(sc2["roles"] == {SELF: "self"}, "earlier role kept")

        # A second meeting at the same minute gets -2; --start overrides the export.
        write(tmp / "b.json", export(transcript_id="tid-2"))
        r3 = tt.ingest(tmp / "b.json", ws, tz_flag=TZ, now=NOW)
        check(r3["folder"] == "2026-10-12-0630-2", f"suffix on a taken name: {r3}")
        r4 = tt.ingest(tmp / "b.json", ws, start="2026-10-13T09:15:00-07:00", tz_flag=TZ, dry_run=True, now=NOW)
        check(r4["folder"] == "2026-10-12-0630-2" and not r4["written"], "same id -> same folder even with a new start")
        write(tmp / "c.json", export(transcript_id="tid-3"))
        r5 = tt.ingest(tmp / "c.json", ws, start="2026-10-13T09:15:00-07:00", tz_flag=TZ, dry_run=True, now=NOW)
        check(r5["folder"] == "2026-10-13-0915" and not (ws / r5["folder"]).exists(), "--start wins; dry run writes nothing")

        # CDP savePath writes JSON.stringify output as a JSON string.
        (tmp / "s.json").write_text(json.dumps(json.dumps(export(transcript_id="tid-4"))), encoding="utf-8")
        check(tt.load_transcript(tmp / "s.json")["transcript_id"] == "tid-4", "string payload decoded")

        for bad, want in ((export(start_iso=None), "no meeting start"),
                          (dict(export(), kind="teams-chat"), "not 'teams-transcript'"),
                          ({"messages": []}, "'entries' list")):
            write(tmp / "bad.json", bad)
            try:
                tt.ingest(tmp / "bad.json", ws, tz_flag=TZ, now=NOW)
                check(False, f"expected TeamsError for {want}")
            except TeamsError as e:
                check(want in str(e), f"error {e!r} mentions {want!r}")

        # Real CLI, then index: the folder is text-only, not flagged for a missing .txt.
        cli_ws = tmp / "cli"
        cli_ws.mkdir()
        p = subprocess.run([str(REPO / "whosaid"), "teams", "ingest-transcript", str(src), "--into", str(cli_ws),
                            "--tz", TZ], capture_output=True, text=True, env=env, cwd=REPO)
        check(p.returncode == 0 and "2026-10-12-0630" in p.stdout and "(created)" in p.stdout, p.stdout + p.stderr)
        p = subprocess.run([str(REPO / "whosaid"), "rollup", str(cli_ws)], capture_output=True, text=True,
                           env=env, cwd=REPO)
        check(p.returncode == 0, p.stderr[-800:])
        idx = (cli_ws / "_INDEX.md").read_text(encoding="utf-8")
        check("2026-10-12-0630: teams-transcript (text-only" in idx, "index audit marks it text-only")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"teams_transcript_test: OK ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
