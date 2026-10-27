"""Durable, read-only Plaud intake -> local speech -> OpenBrain Markdown.

Run from one designated host. A workspace lock prevents overlapping passes;
SQLite records the last successful stage, with one retry before quarantine.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openbrain_summary import SummaryError, atomic_write, summarize
from plaud_client import PlaudClient, Plaud4Client, PlaudError, token_from_config

LIB = Path(__file__).resolve().parent


def recording_date(record: dict) -> datetime:
    for key in ("start_time", "start_at", "created_at", "create_time", "create_timestamp"):
        value = record.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, (int, float)) or str(value).isdigit():
                seconds = float(value)
                return datetime.fromtimestamp(seconds / 1000 if seconds > 1e11 else seconds, timezone.utc)
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
        except (ValueError, OverflowError):
            pass
    raise PlaudError("Recording has no usable date; intake cannot silently skip it")


@contextmanager
def locked(workspace: Path):
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (workspace / ".plaud.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PlaudError("Another Plaud intake pass owns this workspace") from None
        yield


def database(workspace: Path) -> sqlite3.Connection:
    db = sqlite3.connect(workspace / "plaud.sqlite")
    db.row_factory = sqlite3.Row
    db.execute("""create table if not exists recordings (
        id text primary key, folder text not null, stage text not null default 'new',
        sha256 text, attempts integer not null default 0, next_attempt real not null default 0,
        error text, quarantined integer not null default 0, completed_at text)""")
    db.commit()
    return db


def run_process(command: list[str], log: Path, timeout: int = 3600) -> None:
    # Model subprocess logs stay private beside this recording, not in notifications.
    with log.open("w") as output:
        r = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
    if r.returncode:
        raise PlaudError(f"{log.stem} failed; see its recording-local log")


def transcribe(folder: Path, cfg: dict) -> None:
    model = cfg.get("asr_model", "mlx-community/whisper-large-v3-turbo")
    if not (folder / "transcript.json").is_file():
        run_process([sys.executable, str(LIB / "transcribe_mlx.py"), str(folder / "original.audio"),
                     "--model", model, "--outdir", str(folder), "--name", "transcript", "--format", "json"],
                    folder / "transcribe.log")
    command = [sys.executable, str(LIB / "diarize_sherpa.py"), str(folder / "original.audio"),
               "--whisper-json", str(folder / "transcript.json"), "--outdir", str(folder), "--name", "transcript"]
    for name, flag in (("max_speakers", "--max-speakers"), ("speakers", "--num-speakers")):
        if cfg.get(name):
            command += [flag, str(int(cfg[name]))]
    run_process(command, folder / "diarize.log")
    transcript = folder / "transcript.speakers.txt"
    if not transcript.is_file() or not re.search(r"^\[\d", transcript.read_text(), re.M):
        raise PlaudError("No speaker-attributed speech; summary was not requested")


def process_record(db, row, record, workspace, client, cfg, *, speech=transcribe, summary=summarize):
    folder = workspace / "meetings" / row["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    stage = row["stage"]

    def checkpoint(value, **fields):
        nonlocal stage
        stage = value
        fields.update(stage=value, attempts=0, next_attempt=0, error=None)
        db.execute("update recordings set " + ",".join(k + "=?" for k in fields) + " where id=?",
                   (*fields.values(), row["id"]))
        db.commit()

    if stage == "new":
        detail = client.detail(row["id"])
        url = detail.get("presigned_url") or detail.get("audio_url")
        if not url:
            raise PlaudError("Plaud audio is not ready; retry after device/cloud sync")
        digest = client.download(url, folder / "original.audio")
        # Decode before committing a download: an HTML error is not audio.
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                                str(folder / "original.audio")], capture_output=True, text=True, timeout=60)
        if probe.returncode or float(json.loads(probe.stdout).get("format", {}).get("duration", 0)) <= 0:
            raise PlaudError("Downloaded file is not decodable audio")
        metadata = {"source": "plaud", "file_id": row["id"], "title": detail.get("filename") or
                    detail.get("name") or record.get("filename") or record.get("name") or row["id"],
                    "date": recording_date(record).astimezone(ZoneInfo(cfg.get("plaud", {}).get("timezone", "America/New_York"))).isoformat(),
                    "audio_sha256": digest}
        atomic_write(folder / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        checkpoint("downloaded", sha256=digest)
    else:
        digest = hashlib.sha256((folder / "original.audio").read_bytes()).hexdigest()
        if digest != row["sha256"]:
            raise PlaudError("Saved raw audio changed; preserve it for review")
    if stage == "downloaded":
        speech(folder, cfg.get("speech", {}))
        checkpoint("transcribed")
    if stage == "transcribed":
        text = (folder / "transcript.speakers.txt").read_text()
        metadata = json.loads((folder / "metadata.json").read_text())
        # Previous context is explicitly supplied, never guessed from adjacent meetings.
        previous_file = cfg.get("summary", {}).get("previous_context_file")
        previous = Path(previous_file).expanduser().read_text() if previous_file else ""
        markdown, evidence = summary(text, metadata, cfg.get("summary", {}), previous=previous)
        atomic_write(folder / "summary.evidence.json", json.dumps(evidence, ensure_ascii=False, indent=2))
        atomic_write(folder / "summary.md", markdown)
        checkpoint("summarized")
    if stage == "summarized":
        metadata = json.loads((folder / "metadata.json").read_text())
        markdown = (folder / "summary.md").read_text()
        export = cfg.get("output", {}).get("summaries_dir")
        if export:
            # Export only verified notes. Raw audio and biometric sidecars stay
            # in the private workspace, outside any vault/Git sync.
            frontmatter = ("---\nsource: whosaid-local\nplaud_file_id: " + json.dumps(row["id"]) +
                           "\ntitle: " + json.dumps(metadata["title"], ensure_ascii=False) +
                           "\ndate: " + json.dumps(metadata["date"]) + "\n---\n\n")
            atomic_write(Path(export).expanduser() / (row["folder"] + ".md"), frontmatter + markdown)
        checkpoint("complete", completed_at=datetime.now(timezone.utc).isoformat())


def sync(workspace: Path, cfg: dict, *, client=None, speech=transcribe, summary=summarize) -> dict:
    os.umask(0o077)
    cutoff = cfg.get("plaud", {}).get("since")
    if not cutoff:
        raise PlaudError("Set [plaud] since to an explicit YYYY-MM-DD before first intake")
    datetime.strptime(cutoff, "%Y-%m-%d")
    with locked(workspace), database(workspace) as db:
        if client is None:
            pcfg = cfg["plaud"]
            client = (Plaud4Client(Path(pcfg["session_file"]), pcfg.get("domain", "https://api.plaud.ai"),
                                  since=cutoff, timezone=pcfg.get("timezone", "America/New_York"))
                      if pcfg.get("session_file") else
                      PlaudClient(token_from_config(pcfg), pcfg.get("domain", "https://api.plaud.ai")))
        records = client.list_files()
        # No moving watermark: retries and delayed uploads are re-discovered by stable id.
        zone = ZoneInfo(cfg.get("plaud", {}).get("timezone", "America/New_York"))
        selected = sorted((r for r in records if recording_date(r).astimezone(zone).date().isoformat() >= cutoff),
                          key=lambda r: (recording_date(r), str(r.get("id", r.get("file_id", "")))))
        allow = cfg.get("plaud", {}).get("file_ids")
        if allow is not None:
            selected = [r for r in selected if (r.get("id") or r.get("file_id")) in allow]
        result = {"listed": len(records), "selected": len(selected), "complete": 0, "failed": 0, "skipped": 0}
        processed = 0
        for record in selected:
            file_id = record.get("id") or record.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                raise PlaudError("Plaud recording has no stable id")
            folder = recording_date(record).astimezone(zone).strftime("%Y-%m-%d") + "-" + hashlib.sha256(file_id.encode()).hexdigest()[:24]
            db.execute("insert or ignore into recordings(id,folder) values (?,?)", (file_id, folder))
            db.commit()
            row = db.execute("select * from recordings where id=?", (file_id,)).fetchone()
            if row["stage"] == "complete" or row["quarantined"] or row["next_attempt"] > time.time():
                result["skipped"] += 1
                continue
            if processed >= int(cfg.get("plaud", {}).get("max_per_pass", 10)):
                break
            processed += 1
            try:
                process_record(db, row, record, workspace, client, cfg, speech=speech, summary=summary)
                result["complete"] += 1
            except Exception as error:
                # Read the current checkpoint: a failed summary must not consume
                # or reset the successful download/transcription checkpoint.
                current = db.execute("select attempts from recordings where id=?", (file_id,)).fetchone()
                attempts = current["attempts"] + 1
                if isinstance(error, SummaryError) and error.audit:
                    atomic_write(workspace / "meetings" / row["folder"] / f"summary.rejected-{attempts}.json",
                                 json.dumps(error.audit, ensure_ascii=False, indent=2))
                category = type(error).__name__  # never log provider bodies or signed URLs
                db.execute("update recordings set attempts=?,quarantined=?,next_attempt=?,error=? where id=?",
                           (attempts, int(attempts >= 2), time.time() + 300, category, file_id))
                db.commit()
                result["failed"] += 1
        result["quarantined"] = db.execute("select count(*) from recordings where quarantined=1").fetchone()[0]
        result["at"] = datetime.now(timezone.utc).isoformat()
        atomic_write(workspace / "freshness.json", json.dumps(result, indent=2))
        return result


def install(workspace: Path, config: Path, interval: int = 900) -> Path:
    if sys.platform != "darwin":
        raise PlaudError("The scheduler installer supports macOS launchd")
    label = "local.whosaid.plaud"
    workspace.mkdir(parents=True, exist_ok=True)
    plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    if plist.exists():
        raise PlaudError("Existing Plaud scheduler found; inspect it before replacing")
    data = {"Label": label, "ProgramArguments": [sys.executable, str(LIB / "plaud_pipeline.py"), "sync",
            "--workspace", str(workspace.resolve()), "--config", str(config.resolve())],
            "StartInterval": interval, "RunAtLoad": True, "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                     "HOME": str(Path.home())},
            "StandardOutPath": str(workspace / "intake.log"), "StandardErrorPath": str(workspace / "intake-error.log")}
    plist.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(plist, plistlib.dumps(data).decode())
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True)
    if r.returncode:
        plist.unlink()
        raise PlaudError("launchd refused the Plaud scheduler")
    return plist


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("sync", "status", "install", "retry"))
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--config", type=Path)
    p.add_argument("--file-id")
    a = p.parse_args()
    try:
        if a.command == "status":
            data = json.loads((a.workspace / "freshness.json").read_text())
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["at"])).total_seconds()
            print(json.dumps({**data, "age_seconds": round(age), "stale": age > 1800}))
            return 1 if age > 1800 or data.get("failed") or data.get("quarantined") else 0
        if a.command == "retry":
            if not a.file_id:
                p.error("retry requires an exact --file-id")
            with locked(a.workspace), database(a.workspace) as db:
                db.execute("update recordings set attempts=0,quarantined=0,next_attempt=0 where id=?", (a.file_id,))
            return 0
        if not a.config:
            p.error("sync/install requires --config")
        cfg = tomllib.loads(a.config.read_text())
        if a.command == "install":
            print(install(a.workspace, a.config))
            return 0
        result = sync(a.workspace, cfg)
        print(json.dumps(result))
        return 1 if result["failed"] or result["quarantined"] else 0
    except (PlaudError, SummaryError, OSError, ValueError, subprocess.SubprocessError) as e:
        if a.command == "sync" and a.workspace.is_dir():
            atomic_write(a.workspace / "freshness.json", json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                         "failed": 1, "error": type(e).__name__}))
        print(f"whosaid plaud: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
