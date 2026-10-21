#!/usr/bin/env python3
"""
Offline test for inline image ingest in lib/teams_chat.py.

A scraper record may carry images: [{alt, src, width, height, data (PNG data URL | null),
skipped?}]. Ingest writes each captured image to <folder>/teams-images/<epoch>-<n>.png,
appends an "[image: teams-images/<file>]" reference to the message text in
teams.speakers.txt, lists the paths on the sidecar segment, keeps image-only messages,
and stays idempotent. A re-scrape without image data (capture off) keeps the earlier refs.

Run:  python3 test/teams_chat_images_test.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import teams_chat  # noqa: E402
import wsconfig  # noqa: E402

TZ = "America/Los_Angeles"
LA = ZoneInfo(TZ)
CHECKS = 0
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
DATA = "data:image/png;base64," + base64.b64encode(PNG).decode()


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def ms(h, mi) -> int:
    return int(datetime(2026, 10, 20, h, mi, tzinfo=LA).timestamp() * 1000)


T1, T2, T3 = ms(11, 28), ms(11, 30), ms(11, 31)
IMG = {"alt": "image", "src": "https://example.invalid/full", "width": 4, "height": 3, "data": DATA}


def export(tmp: Path, records, name) -> Path:
    p = tmp / name
    p.write_text(json.dumps(records), encoding="utf-8")
    return p


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="teams-images-test-"))
    os.environ["WHOSAID_SPEAKER_DB"] = str(tmp / "no-registry.json")
    try:
        run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"PASS: teams_chat_images_test ({CHECKS} checks)")


def run(tmp: Path) -> None:
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "whosaid.toml").write_text("", encoding="utf-8")
    recs = [
        {"chat": "Chat A", "author": "Roe Richard", "epoch": T1, "text": "look at this", "images": [IMG]},
        {"chat": "Chat A", "author": "Roe Richard", "epoch": T2, "text": "",
         "images": [IMG, dict(IMG, data=None, skipped="not-loaded")]},
        {"chat": "Chat A", "author": "Roe Richard", "epoch": T3, "text": "plain"},
    ]
    res = teams_chat.ingest(export(tmp, recs, "a.json"), ws, tz_flag=TZ)
    check(len(res) == 1 and res[0]["total"] == 3, f"image-only message kept: {res}")
    folder = ws / res[0]["folder"]
    f1 = folder / "teams-images" / f"{T1}-1.png"
    f2 = folder / "teams-images" / f"{T2}-1.png"
    check(f1.read_bytes() == PNG, "image bytes written")
    check(f2.read_bytes() == PNG, "image-only message's image written")
    check(not (folder / "teams-images" / f"{T2}-2.png").exists(), "uncaptured image writes no file")

    sp = (folder / "teams.speakers.txt").read_text(encoding="utf-8")
    check(f"look at this [image: teams-images/{T1}-1.png]" in sp, sp)
    check(f"Roe Richard: [image: teams-images/{T2}-1.png] [image not captured]" in sp, sp)
    turns = wsconfig.parse_turns(sp)
    check(len(turns) == 3, f"3 turns parse: {turns}")

    sc = json.loads((folder / "teams.diarization.json").read_text(encoding="utf-8"))
    segs = {s["epoch_ms"]: s for s in sc["segments"]}
    check(segs[T1]["images"] == [f"teams-images/{T1}-1.png"], segs[T1])
    check(segs[T2]["images"] == [f"teams-images/{T2}-1.png"], segs[T2])
    check("images" not in segs[T3], "text-only segment has no images key")

    # Idempotent re-ingest: same files, same transcript.
    before = sp
    res2 = teams_chat.ingest(export(tmp, recs, "a.json"), ws, tz_flag=TZ)
    check(res2[0]["new"] == 0, res2)
    check((folder / "teams.speakers.txt").read_text(encoding="utf-8").replace(
        sp.splitlines()[1], "") == before.replace(sp.splitlines()[1], ""), "re-ingest unchanged")

    # A later scrape with capture off (no images key, or data null) keeps the earlier refs.
    off = [{"chat": "Chat A", "author": "Roe Richard", "epoch": T1, "text": "look at this"},
           {"chat": "Chat A", "author": "Roe Richard", "epoch": T2, "text": "",
            "images": [dict(IMG, data=None, skipped="not-loaded")]}]
    teams_chat.ingest(export(tmp, off, "b.json"), ws, tz_flag=TZ)
    sp3 = (folder / "teams.speakers.txt").read_text(encoding="utf-8")
    check(f"look at this [image: teams-images/{T1}-1.png]" in sp3, sp3)
    check(f"[image: teams-images/{T2}-1.png]" in sp3, "existing file still referenced when data is missing")

    # Dry run writes no image files.
    ws2 = tmp / "ws2"
    ws2.mkdir()
    (ws2 / "whosaid.toml").write_text("", encoding="utf-8")
    teams_chat.ingest(export(tmp, recs, "a.json"), ws2, tz_flag=TZ, dry_run=True)
    check(not any(ws2.rglob("*.png")), "dry run writes nothing")

    # A malformed images field is a user-fixable error.
    try:
        teams_chat.ingest(export(tmp, [dict(recs[0], images="nope")], "c.json"), ws, tz_flag=TZ)
        check(False, "images must be a list")
    except teams_chat.TeamsError as e:
        check("images" in str(e), str(e))


if __name__ == "__main__":
    main()
