#!/usr/bin/env python3
"""
teams_transcript.py: ingest a Microsoft Teams meeting transcript (the Recap
"Transcript" panel, scraped by contrib/teams-scraper/teams-transcript-scraper.js)
into a whosaid workspace. One meeting becomes one dated folder holding

  teams.speakers.txt        the indexed artifact (parse_turns-compatible); turn
                            stamps are offsets from the meeting start, like an
                            audio meeting's, so `whosaid context` lines up
  teams.diarization.json    provenance sidecar; source.kind == "teams-transcript"

Input is the scraper's window.__tt.result: {"kind": "teams-transcript",
"transcript_id", "recap_url", "locale", "method", "meeting": {title, start_iso,
scheduled} | null, "entries": [{idx, entry_id, type, speaker, speaker_id,
start_s, end_s, text}]}. A file holding that object JSON-encoded as a string (what
a CDP savePath of JSON.stringify(...) writes) is accepted too.

Usage:
  python3 lib/teams_transcript.py ingest-transcript <transcript.json> --into WS
      [--start ISO] [--title T] [--tz ZONE] [--self NAME] [--dry-run]

The meeting start comes from --start, else the export's meeting.start_iso; one is
required (the transcript itself carries only offsets). Re-ingest is idempotent:
the folder already holding the same transcript_id is rewritten in place, never
renamed. Names and roles use the chat adapter's [teams.names] / [teams.roles].
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, tzinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import teams_chat as tc  # noqa: E402
import wsconfig  # noqa: E402
from teams_chat import TeamsError  # noqa: E402

BASE = tc.BASE
SPEAKERS_NAME = tc.SPEAKERS_NAME
SIDECAR_NAME = tc.SIDECAR_NAME
KIND = "teams-transcript"
SCRAPER = "teams-transcript-scraper@1"
TEXT_TYPES = {"Text"}


# ---- load -------------------------------------------------------------------------------

def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def load_transcript(path: str | os.PathLike) -> dict:
    """Read and validate the scraper result. Returns
    {transcript_id, recap_url, locale, method, meeting, entries: [...]}; raises TeamsError."""
    data = tc._parse_json_or_ndjson(path)
    if isinstance(data, str):  # JSON.stringify output saved as a JSON string
        try:
            data = json.loads(data)
        except ValueError as e:
            raise TeamsError(f"export {path}: string payload is not JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise TeamsError("transcript export must be an object with an 'entries' list "
                         "(window.__tt.result from teams-transcript-scraper.js)")
    if data.get("kind") not in (None, KIND):
        raise TeamsError(f"export kind is {data.get('kind')!r}, not {KIND!r}")
    entries = []
    for i, e in enumerate(data["entries"]):
        if not isinstance(e, dict):
            raise TeamsError(f"entry {i} is not an object")
        if not isinstance(e.get("text", ""), str):
            raise TeamsError(f"entry {i}: 'text' must be a string")
        entries.append({
            "idx": e.get("idx", i),
            "entry_id": e.get("entry_id"),
            "type": str(e.get("type") or "Text"),
            "speaker": e.get("speaker") if isinstance(e.get("speaker"), str) else None,
            "start_s": _num(e.get("start_s")),
            "end_s": _num(e.get("end_s")),
            "text": e.get("text") or "",
            "lang": e.get("lang") or e.get("spokenLanguageTag") or None,
        })
    meeting = data.get("meeting") if isinstance(data.get("meeting"), dict) else {}
    return {
        "transcript_id": data.get("transcript_id") or None,
        "recap_url": data.get("recap_url") or None,
        "locale": data.get("locale") or next((e["lang"] for e in entries if e["lang"]), None),
        "method": data.get("method") or None,
        "expected": data.get("expected"),
        "meeting": meeting,
        "entries": entries,
    }


# ---- render -----------------------------------------------------------------------------

def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def build_turns(entries: list[dict], name_map: dict[str, str]) -> tuple[list[dict], list[dict], dict[str, str]]:
    """(turns, events, names). Turns are text entries with a start offset and a
    non-empty body, in transcript order; events are Start/Stop (and other non-text) rows."""
    turns, events, names = [], [], {}
    for e in entries:
        if e["type"] not in TEXT_TYPES:
            events.append({"type": e["type"], "speaker": e["speaker"], "start": e["start_s"]})
            continue
        text = tc.clean_text(e["text"])
        if not text or e["start_s"] is None:
            continue
        display = e["speaker"] or "Unknown"
        spk = tc.resolve_speaker(display, name_map)
        names[tc._one_line(display)] = spk
        end = e["end_s"] if e["end_s"] is not None else e["start_s"]
        turns.append({"start": e["start_s"], "end": max(end, e["start_s"]), "speaker": spk,
                      "entry_id": e["entry_id"], "text": text})
    turns.sort(key=lambda t: t["start"])  # stable: keeps transcript order on ties
    return turns, events, names


def render_speakers(title: str, start_local: datetime, scraped_date: str, turns: list[dict],
                    roles: dict[str, str]) -> str:
    speakers = sorted({t["speaker"] for t in turns})
    lines = [
        f"# Speaker-labeled transcript: {BASE}",
        f"# Source: ms-teams-transcript ({title}), meeting {start_local.strftime('%Y-%m-%d %H:%M %Z').strip()}, "
        f"scraped {scraped_date}",
        f"# Speakers ({len(speakers)}): {', '.join(speakers)}",
    ]
    for name in sorted(roles):
        if name in speakers:
            lines.append(f"# Role: {name} = {roles[name]}")
    lines.append("")
    for t in turns:
        lines.append(f"[{hms(t['start'])}] {t['speaker']}: {t['text']}")
        lines.append("")
    return "\n".join(lines)


def render_sidecar(doc: dict, title: str, start_utc: datetime, scheduled: str | None,
                   turns: list[dict], events: list[dict], names: dict[str, str],
                   roles: dict[str, str], scraped_at: str) -> dict:
    ends = [t["end"] for t in turns] + [e["start"] for e in events if e["start"] is not None]
    speakers = {t["speaker"] for t in turns}
    return {
        "base": BASE,
        "source": {
            "kind": KIND, "platform": "microsoft-teams", "surface": "web-recap",
            "meeting_title": title, "transcript_id": doc["transcript_id"],
            "recap_url": doc["recap_url"], "locale": doc["locale"],
            "meeting_start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scheduled": scheduled,
            "duration_seconds": max(ends) if ends else 0,
            "entry_count": len(doc["entries"]), "turn_count": len(turns),
            "method": doc["method"],
            "scraped_at": scraped_at, "scraper": SCRAPER,
            "creation_time": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "names": dict(sorted(names.items())),
        "roles": {k: v for k, v in sorted(roles.items()) if k in speakers},
        "events": events,
        "segments": turns,
    }


# ---- folders ----------------------------------------------------------------------------

def find_existing(ws: Path, transcript_id: str | None, start_iso: str, title: str) -> Path | None:
    """The folder whose sidecar carries this transcript_id; without an id, the same
    meeting_start + title."""
    for d in sorted(p for p in Path(ws).iterdir() if p.is_dir()):
        src = (tc.read_sidecar(d) or {}).get("source")
        if not isinstance(src, dict) or src.get("kind") != KIND:
            continue
        if transcript_id and src.get("transcript_id"):
            if src["transcript_id"] == transcript_id:
                return d
        elif src.get("meeting_start") == start_iso and src.get("meeting_title") == title:
            return d
    return None


def pick_folder_name(ws: Path, start_local: datetime) -> str:
    base = start_local.strftime("%Y-%m-%d-%H%M")
    name, n = base, 1
    while (Path(ws) / name).exists():
        n += 1
        name = f"{base}-{n}"
    assert tc.DATE_DIR_RE.match(name), name
    return name


# ---- ingest -----------------------------------------------------------------------------

def resolve_start(flag: str | None, meeting: dict) -> datetime:
    raw = flag or meeting.get("start_iso")
    ms = tc._iso_ms(raw) if raw else None
    if ms is None:
        if raw:
            raise TeamsError(f"meeting start {raw!r} is not an ISO 8601 timestamp")
        raise TeamsError("no meeting start: pass --start ISO (e.g. 2026-10-12T06:30-07:00), "
                         "or set meeting.start_iso in the export (TOP_FRAME_SNIPPET in the scraper)")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ingest(export: str | os.PathLike, ws: str | os.PathLike, *, start: str | None = None,
           title: str | None = None, tz_flag: str | None = None, self_name: str | None = None,
           dry_run: bool = False, now: datetime | None = None) -> dict:
    """Returns {folder, title, turns, entries, created, written}."""
    ws = Path(ws)
    if not ws.is_dir():
        raise TeamsError(f"workspace {ws} is not a directory")
    doc = load_transcript(export)
    cfg = wsconfig.load_config(ws)
    teams_cfg = cfg.get("teams") if isinstance(cfg.get("teams"), dict) else {}
    name_map = {str(k): str(v) for k, v in (teams_cfg.get("names") or {}).items()}
    toml_roles = {str(k): str(v) for k, v in (teams_cfg.get("roles") or {}).items()}
    tz: tzinfo = tc.resolve_tz(tz_flag, cfg)
    start_utc = resolve_start(start, doc["meeting"])
    start_local = start_utc.astimezone(tz)
    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    title = tc._one_line(title or doc["meeting"].get("title") or "Teams meeting")
    now = now or datetime.now(timezone.utc)
    scraped_at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    turns, events, names = build_turns(doc["entries"], name_map)
    if not turns:
        raise TeamsError("transcript has no text entries")
    folder = find_existing(ws, doc["transcript_id"], start_iso, title)
    created = folder is None
    old_sc = None if created else tc.read_sidecar(folder)
    if created:
        folder = ws / pick_folder_name(ws, start_local)
    speakers = sorted({t["speaker"] for t in turns})
    roles = tc.resolve_roles(speakers, toml_roles, tc.registry_roles(), self_name)
    for k, v in ((old_sc or {}).get("roles") or {}).items():  # keep roles an earlier run recorded
        roles.setdefault(k, v)
    roles = {k: v for k, v in sorted(roles.items()) if k in speakers}
    if not dry_run:
        folder.mkdir(parents=True, exist_ok=True)
        sidecar = render_sidecar(doc, title, start_utc, doc["meeting"].get("scheduled"),
                                 turns, events, names, roles, scraped_at)
        (folder / SPEAKERS_NAME).write_text(
            render_speakers(title, start_local, scraped_at[:10], turns, roles), encoding="utf-8")
        (folder / SIDECAR_NAME).write_text(
            json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"folder": folder.name, "title": title, "turns": len(turns),
            "entries": len(doc["entries"]), "created": created, "written": not dry_run}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="teams_transcript.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest-transcript", help="ingest a Teams meeting transcript into a workspace")
    pi.add_argument("export", help="JSON from teams-transcript-scraper.js (window.__tt.result)")
    pi.add_argument("--into", required=True, metavar="WS", help="workspace directory")
    pi.add_argument("--start", default=None, metavar="ISO",
                    help="meeting start, ISO 8601 (default: the export's meeting.start_iso)")
    pi.add_argument("--title", default=None, help="meeting title (default: the export's meeting.title)")
    pi.add_argument("--tz", default=None, help="IANA zone for the folder name (default: whosaid.toml [workspace].tz, else system)")
    pi.add_argument("--self", dest="self_name", default=None, metavar="NAME",
                    help="canonical name of the workspace owner (role 'self')")
    pi.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = ap.parse_args(argv)
    try:
        r = ingest(args.export, args.into, start=args.start, title=args.title, tz_flag=args.tz,
                   self_name=args.self_name, dry_run=args.dry_run)
    except TeamsError as e:
        print(f"whosaid: teams ingest-transcript: {e}", file=sys.stderr)
        return 1
    verb = "would write" if not r["written"] else ("created" if r["created"] else "updated")
    print(f"{r['folder']}  {r['title']}  {r['turns']} turns of {r['entries']} entries ({verb})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
