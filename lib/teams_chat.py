#!/usr/bin/env python3
"""
teams_chat.py: ingest a Microsoft Teams chat export into a whosaid workspace
(GitHub issue #49). A non-audio chat adapter: each (chat, local calendar day)
becomes one dated meeting folder holding

  teams.speakers.txt        the indexed artifact (parse_turns-compatible)
  teams.diarization.json    provenance sidecar; source.kind == "teams-chat"

Input is a hand-off file: a list of message records, an object
{"messages": [...], optional "chat_id", "url"}, the scraper's raw_by_chat map
{"<chat>": {"messages": [...]}}, or NDJSON (one record per line). A record is
{"chat", "author", "epoch_ms" | "epoch", "text"} with "timestamp_iso" | "ts"
(ISO 8601) as the fallback clock, plus optional "chat_id", "url". epoch_ms wins
over epoch. Records may be unsorted and duplicated (epoch dedups).

The same ingest serves Outlook mail (contrib/outlook-scraper): with --source outlook
the files are outlook.speakers.txt / outlook.diarization.json, source.kind is
"outlook-mail", "chat" is the thread subject and "chat_id" the conversation id.

Usage:
  python3 lib/teams_chat.py ingest <export.json> --into WS [--tz ZONE] [--self NAME]
                                   [--source teams|outlook] [--dry-run]

Re-ingest is idempotent: an existing folder holding the same chat-day is merged
(dedup on epoch_ms) and rewritten in place, never renamed. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wsconfig  # noqa: E402



@dataclass(frozen=True)
class Source:
    """One text-only source: file base name, sidecar source.kind and provenance labels.
    `cfg` is the whosaid.toml table holding [<cfg>.names] / [<cfg>.roles]."""
    base: str
    kind: str
    platform: str
    label: str      # the "# Source:" header word
    scraper: str
    cfg: str

    @property
    def speakers_name(self) -> str:
        return f"{self.base}.speakers.txt"

    @property
    def sidecar_name(self) -> str:
        return f"{self.base}.diarization.json"


TEAMS = Source("teams", "teams-chat", "microsoft-teams", "ms-teams-chat", "teams-dom-scraper@1", "teams")
OUTLOOK = Source("outlook", "outlook-mail", "microsoft-outlook", "ms-outlook-mail", "outlook-dom-scraper@1", "outlook")
SOURCES = {"teams": TEAMS, "outlook": OUTLOOK}
TEXT_KINDS = frozenset(s.kind for s in SOURCES.values())

BASE = TEAMS.base
SPEAKERS_NAME = TEAMS.speakers_name
SIDECAR_NAME = TEAMS.sidecar_name
KIND = TEAMS.kind
SCRAPER = TEAMS.scraper
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}(?:-\d+)?$")


class TeamsError(Exception):
    """A problem the user can fix; main() prints one line and exits 1."""


# ---- C1: load the export ----------------------------------------------------------------

def _one_line(value: str) -> str:
    return " ".join(str(value).split())


def _iso_ms(value) -> int | None:
    """ISO 8601 string (Z suffix or offset; naive means UTC) -> epoch ms, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v[-1] in "Zz":
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)


def _record_ms(rec: dict) -> int | None:
    """epoch_ms, else epoch (numbers, bool rejected), else ISO timestamp_iso, else ts."""
    for key in ("epoch_ms", "epoch"):
        v = rec.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    for key in ("timestamp_iso", "ts"):
        ms = _iso_ms(rec.get(key))
        if ms is not None:
            return ms
    return None


def _parse_json_or_ndjson(path) -> object:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise TeamsError(f"cannot read export {path}: {e.strerror or e}") from None
    except UnicodeDecodeError as e:
        raise TeamsError(f"export {path} is not valid JSON: {e}") from None
    try:
        return json.loads(text)
    except ValueError as e:
        whole_err = e
    # Not one JSON document: try NDJSON, one JSON object per non-blank line.
    if not text.strip():
        raise TeamsError(f"export {path} is not valid JSON: {whole_err}")
    recs = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            recs.append(json.loads(line))
        except ValueError as e:
            raise TeamsError(f"export {path} is not valid JSON or NDJSON: line {n}: {e}") from None
    return recs


def load_export(path: str | os.PathLike) -> list[dict]:
    """Read the hand-off file into normalized records:
    {chat, chat_id, url, author, epoch_ms, text}. Raises TeamsError.
    Accepts a list, {"messages": [...]}, a scraper raw_by_chat map
    ({chat: {"messages": [...]}}), or NDJSON (one record per line)."""
    data = _parse_json_or_ndjson(path)
    env_chat_id = env_url = None
    if isinstance(data, dict):
        if "messages" not in data and data and all(
                isinstance(v, dict) and isinstance(v.get("messages"), list) for v in data.values()):
            flat: list = []
            for key, val in data.items():
                for m in val["messages"]:
                    if isinstance(m, dict) and m.get("chat") is None:
                        m = dict(m, chat=key)
                    flat.append(m)
            data = flat
        elif "messages" not in data and "author" in data and "text" in data:
            data = [data]  # a single-record NDJSON file parses as one object
        else:
            env_chat_id, env_url = data.get("chat_id"), data.get("url")
            data = data.get("messages")
    if not isinstance(data, list):
        raise TeamsError("export must be a list of messages, an object with a 'messages' list, "
                         "or a map of chat name to {'messages': [...]}")
    out: list[dict] = []
    for i, rec in enumerate(data):
        if not isinstance(rec, dict):
            raise TeamsError(f"message {i} is not an object")
        ms = _record_ms(rec)
        if ms is None:
            raise TeamsError(f"message {i}: needs a numeric epoch_ms or epoch, "
                             "or an ISO 8601 timestamp_iso or ts")
        for key in ("chat", "author", "text"):
            if not isinstance(rec.get(key), str):
                raise TeamsError(f"message {i}: '{key}' must be a string")
        chat = _one_line(rec["chat"])
        if not chat:
            raise TeamsError(f"message {i}: 'chat' is empty")
        out.append({
            "chat": chat,
            "chat_id": rec.get("chat_id") or env_chat_id or None,
            "url": rec.get("url") or env_url or None,
            "author": rec["author"],
            "epoch_ms": ms,
            "text": rec["text"],
        })
    # A chat whose records carry a chat_id on only some of them is one chat:
    # propagate the first id (and url) seen for each chat name to the rest.
    ids: dict[str, str] = {}
    urls: dict[str, str] = {}
    for r in out:
        if r["chat_id"]:
            ids.setdefault(r["chat"], r["chat_id"])
        if r["url"]:
            urls.setdefault(r["chat"], r["url"])
    for r in out:
        r["chat_id"] = r["chat_id"] or ids.get(r["chat"])
        r["url"] = r["url"] or urls.get(r["chat"])
    return out


# ---- text + names (C4, C6) --------------------------------------------------------------

def clean_text(text: str) -> str:
    return " ".join(str(text).split())


def sanitize_speaker(name: str) -> str:
    """':' breaks TURN_BRACKET_RE's speaker group, so it becomes '-'."""
    return _one_line(str(name).replace(":", "-")) or "Unknown"


def resolve_speaker(author: str, name_map: dict[str, str]) -> str:
    """Display name -> canonical name via [teams.names]; unmapped keeps the display name."""
    mapped = name_map.get(author)
    if mapped is None:
        mapped = name_map.get(_one_line(author))
    return sanitize_speaker(mapped if mapped else author)


def registry_roles() -> dict[str, str]:
    """Per-speaker role from the voice registry; {} when absent or unreadable."""
    try:
        import speaker_registry
        reg = speaker_registry.read_json(speaker_registry.registry_path(), label="speaker registry")
    except Exception:  # noqa: BLE001 - registry is optional
        return {}
    roles: dict[str, str] = {}
    for entry in reg.get("speakers", []):
        if isinstance(entry, dict) and entry.get("role") and entry.get("name"):
            roles.setdefault(str(entry["name"]), str(entry["role"]))
    return roles


def resolve_roles(speakers: list[str], toml_roles: dict, reg_roles: dict[str, str],
                  self_name: str | None) -> dict[str, str]:
    """[teams.roles] first, then the registry, then --self (wins). Only for speakers present."""
    roles: dict[str, str] = {}
    for s in speakers:
        role = toml_roles.get(s) or reg_roles.get(s)
        if role:
            roles[s] = str(role)
    if self_name:
        roles[sanitize_speaker(self_name)] = "self"
    return roles


# ---- C2: chat-days ----------------------------------------------------------------------

def resolve_tz(flag: str | None, cfg: dict) -> tzinfo:
    name = (flag or (cfg.get("workspace") or {}).get("tz") or "").strip()
    if not name:
        return datetime.now().astimezone().tzinfo  # system zone
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as e:  # noqa: BLE001
        raise TeamsError(f"unknown timezone {name!r} ({e})") from None


def local_dt(epoch_ms: int, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).astimezone(tz)


def utc_iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chat_identity(chat_id: str | None, chat_name: str) -> str:
    return f"id:{chat_id}" if chat_id else f"name:{chat_name}"


def group_chat_days(records: list[dict], tz: tzinfo, name_map: dict[str, str]) -> list[dict]:
    """Group records into chat-days. Each group:
    {chat_name, chat_id, url, day, messages: {epoch_ms: (speaker, display, text)}, names: {display: canonical}}.
    Dedups on epoch_ms (last record wins); drops messages empty after whitespace collapse."""
    groups: dict[tuple[str, str], dict] = {}
    for rec in records:
        text = clean_text(rec["text"])
        if not text:
            continue
        day = local_dt(rec["epoch_ms"], tz).strftime("%Y-%m-%d")
        ident = chat_identity(rec["chat_id"], rec["chat"])
        g = groups.setdefault((ident, day), {
            "chat_name": rec["chat"], "chat_id": rec["chat_id"], "url": rec["url"],
            "day": day, "messages": {}, "names": {},
        })
        if rec["chat_id"] and not g["chat_id"]:
            g["chat_id"] = rec["chat_id"]
        if rec["url"] and not g["url"]:
            g["url"] = rec["url"]
        speaker = resolve_speaker(rec["author"], name_map)
        g["messages"][rec["epoch_ms"]] = (speaker, text)
        g["names"][_one_line(rec["author"])] = speaker
    return sorted(groups.values(), key=lambda g: (g["day"], min(g["messages"])))


# ---- C3: find / merge an existing folder ------------------------------------------------

def read_sidecar(folder: Path, src: Source = TEAMS) -> dict | None:
    p = folder / src.sidecar_name
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def find_existing(ws: Path, group: dict, src: Source = TEAMS) -> Path | None:
    want = chat_identity(group["chat_id"], group["chat_name"])
    for d in sorted(p for p in Path(ws).iterdir() if p.is_dir()):
        sc = read_sidecar(d, src)
        meta = (sc or {}).get("source")
        if not isinstance(meta, dict) or meta.get("kind") != src.kind or meta.get("day") != group["day"]:
            continue
        if group["chat_id"] and meta.get("chat_id"):
            same = meta["chat_id"] == group["chat_id"]
        else:
            same = meta.get("chat_name") == group["chat_name"]
        if same:
            return d
    return None


def pick_folder_name(ws: Path, group: dict, tz: tzinfo, reserved: set[str]) -> str:
    first = local_dt(min(group["messages"]), tz)
    base = first.strftime("%Y-%m-%d-%H%M")
    name, n = base, 1
    while (Path(ws) / name).exists() or name in reserved:
        n += 1
        name = f"{base}-{n}"
    assert DATE_DIR_RE.match(name), name
    return name


def existing_messages(sidecar: dict | None) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for seg in (sidecar or {}).get("segments", []) or []:
        if isinstance(seg, dict) and isinstance(seg.get("epoch_ms"), int):
            out[seg["epoch_ms"]] = (str(seg.get("speaker", "Unknown")), str(seg.get("text", "")))
    return out


# ---- C4 / C5: render --------------------------------------------------------------------

def render_speakers(chat_name: str, scraped_date: str, messages: dict[int, tuple[str, str]],
                    roles: dict[str, str], tz: tzinfo, src: Source = TEAMS) -> str:
    speakers = sorted({s for s, _ in messages.values()})
    lines = [
        f"# Speaker-labeled transcript: {src.base}",
        f"# Source: {src.label} ({chat_name}), scraped {scraped_date}",
        f"# Speakers ({len(speakers)}): {', '.join(speakers)}",
    ]
    for name in sorted(roles):
        if name in speakers:
            lines.append(f"# Role: {name} = {roles[name]}")
    lines.append("")
    for ms in sorted(messages):
        spk, text = messages[ms]
        lines.append(f"[{local_dt(ms, tz).strftime('%H:%M:%S')}] {spk}: {text}")
        lines.append("")
    return "\n".join(lines)


def _sec_of_day(ms: int, tz: tzinfo) -> int:
    d = local_dt(ms, tz)
    return d.hour * 3600 + d.minute * 60 + d.second


def render_sidecar(group: dict, messages: dict[int, tuple[str, str]], names: dict[str, str],
                   roles: dict[str, str], tz: tzinfo, scraped_at: str, src: Source = TEAMS) -> dict:
    order = sorted(messages)
    speakers = {s for s, _ in messages.values()}
    segs = []
    for ms in order:
        spk, text = messages[ms]
        t = _sec_of_day(ms, tz)
        segs.append({"start": t, "end": t, "speaker": spk, "epoch_ms": ms, "mid": str(ms), "text": text})
    return {
        "base": src.base,
        "source": {
            "kind": src.kind, "platform": src.platform, "surface": "web",
            "chat_name": group["chat_name"], "chat_id": group["chat_id"], "url": group["url"],
            "day": group["day"], "message_count": len(messages),
            "scraped_at": scraped_at, "scraper": src.scraper,
            "creation_time": utc_iso(order[0]),
        },
        "names": dict(sorted(names.items())),
        "roles": {k: v for k, v in sorted(roles.items()) if k in speakers},
        "segments": segs,
    }


# ---- ingest -----------------------------------------------------------------------------

def ingest(export: str | os.PathLike, ws: str | os.PathLike, *, tz_flag: str | None = None,
           self_name: str | None = None, dry_run: bool = False, now: datetime | None = None,
           src: Source = TEAMS) -> list[dict]:
    """Returns one result per folder: {folder, chat, total, new, written}."""
    ws = Path(ws)
    if not ws.is_dir():
        raise TeamsError(f"workspace {ws} is not a directory")
    records = load_export(export)
    cfg = wsconfig.load_config(ws)
    # [teams.*] is the shared people map; a source's own table ([outlook.names]) wins over it.
    name_map, toml_roles = {}, {}
    for table in dict.fromkeys(("teams", src.cfg)):
        t = cfg.get(table) if isinstance(cfg.get(table), dict) else {}
        name_map.update({str(k): str(v) for k, v in (t.get("names") or {}).items()})
        toml_roles.update({str(k): str(v) for k, v in (t.get("roles") or {}).items()})
    tz = resolve_tz(tz_flag, cfg)
    reg_roles = registry_roles()
    now = now or datetime.now(timezone.utc)
    scraped_at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scraped_date = scraped_at[:10]

    results, reserved = [], set()
    for g in group_chat_days(records, tz, name_map):
        folder = find_existing(ws, g, src)
        old_sc = read_sidecar(folder, src) if folder else None
        merged = existing_messages(old_sc)
        known = set(merged)
        merged.update(g["messages"])
        new = len(set(merged) - known)
        names = dict((old_sc or {}).get("names") or {})
        names.update(g["names"])
        if folder is None:
            name = pick_folder_name(ws, g, tz, reserved)
            reserved.add(name)
            folder = ws / name
        speakers = sorted({s for s, _ in merged.values()})
        roles = resolve_roles(speakers, toml_roles, reg_roles, self_name)
        if old_sc:  # keep roles an earlier run recorded for speakers still present
            for k, v in ((old_sc.get("roles") or {}).items()):
                roles.setdefault(k, v)
        roles = {k: v for k, v in sorted(roles.items()) if k in speakers}
        if not dry_run:
            folder.mkdir(parents=True, exist_ok=True)
            sidecar = render_sidecar(g, merged, names, roles, tz, scraped_at, src)
            (folder / src.speakers_name).write_text(
                render_speakers(g["chat_name"], scraped_date, merged, roles, tz, src), encoding="utf-8")
            (folder / src.sidecar_name).write_text(
                json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        results.append({"folder": folder.name, "chat": g["chat_name"], "total": len(merged),
                        "new": new, "written": not dry_run})
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="teams_chat.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", help="ingest a Teams chat or Outlook mail export into a workspace")
    pi.add_argument("export", help="JSON hand-off file from the Teams or Outlook scraper")
    pi.add_argument("--source", choices=sorted(SOURCES), default="teams",
                    help="which scraper produced the export (default: teams)")
    pi.add_argument("--into", required=True, metavar="WS", help="workspace directory")
    pi.add_argument("--tz", default=None, help="IANA zone for chat-days (default: whosaid.toml [workspace].tz, else system)")
    pi.add_argument("--self", dest="self_name", default=None, metavar="NAME",
                    help="canonical name of the workspace owner (role 'self')")
    pi.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = ap.parse_args(argv)
    try:
        results = ingest(args.export, args.into, tz_flag=args.tz, self_name=args.self_name,
                         dry_run=args.dry_run, src=SOURCES[args.source])
    except TeamsError as e:
        print(f"whosaid: {args.source} ingest: {e}", file=sys.stderr)
        return 1
    for r in results:
        print(f"{r['folder']}  {r['chat']}  {r['total']} messages ({r['new']} new)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
