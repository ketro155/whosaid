"""Revision-aware meeting outbox. Preparation is local; sending is opt-in.

One meeting-level thought per revision, no transcript dump. Stable requests use
OpenBrain's existing durable capture deduplication. Local acknowledgement requires
independent exact-content and supersession readback, not merely an HTTP success.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from openbrain_summary import validate, verification_metadata
from plaud_client import NoRedirect, validate_url

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class DeliveryError(RuntimeError):
    pass


@contextmanager
def connect(workspace):
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = workspace / "delivery.sqlite"
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("""create table if not exists outbox (
        event_id text primary key, meeting_id text not null, revision integer not null,
        digest text not null, parent text, content text not null, status text not null default 'pending',
        request text, thought_id text, attempts integer not null default 0, error text,
        created_at text not null, delivered_at text, unique(meeting_id,revision))""")
    if "verification_binding" not in {r[1] for r in db.execute("pragma table_info(outbox)")}:
        db.execute("alter table outbox add column verification_binding text")
    db.commit()
    try:
        with db:
            yield db
    finally:
        db.close()


def enqueue(workspace, folder):
    folder = Path(folder)
    meta = json.loads((folder / "metadata.json").read_text())
    audit = json.loads((folder / "summary.evidence.json").read_text())
    transcript = (folder / "transcript.speakers.txt").read_text()
    markdown = (folder / "summary.md").read_text()
    if audit.get("verification_metadata") != verification_metadata(meta):
        raise DeliveryError("Meeting metadata changed or lacks verification; re-verify before delivery")
    if audit.get("verification") != {"valid": True, "issues": []}:
        raise DeliveryError("Delivery requires a verified summary")
    if hashlib.sha256(transcript.encode()).hexdigest() != audit.get("transcript_sha256"):
        raise DeliveryError("Summary evidence does not match the current transcript")
    previous = "\n".join(p["text"] for p in audit.get("source_passages", []) if p.get("source") == "previous_context")
    if validate(audit["draft"], transcript, previous) != markdown:
        raise DeliveryError("Summary changed after verification; re-verify before delivery")
    meeting = meta.get("file_id")
    if not isinstance(meeting, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", meeting):
        raise DeliveryError("Missing or invalid stable meeting identity")
    date = datetime.fromisoformat(meta["date"])
    if date.tzinfo is None:
        raise DeliveryError("Meeting date requires a timezone")
    delivered_metadata = {key: meta.get(key) for key in ("file_id", "date", "title", "audio_sha256")}
    digest = hashlib.sha256(json.dumps({"metadata": delivered_metadata, "summary": markdown,
        "transcript_sha256": audit["transcript_sha256"], "template_sha256": audit.get("template_sha256")},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with connect(workspace) as db:
        db.execute("begin immediate")
        prior = db.execute("select * from outbox where meeting_id=? order by revision desc limit 1", (meeting,)).fetchone()
        same_revision = prior and prior["digest"] == digest
        revision = prior["revision"] if same_revision else (prior["revision"] + 1 if prior else 1)
        event = prior["event_id"] if same_revision else f"plaud:{meeting}:{revision}:{digest}"
        # Keep the content byte-identical across retries. No ingest timestamp,
        # signed URL, audio, transcript body, or local biometric paths.
        content = ("[whosaid-local meeting summary]\n" + json.dumps({"meeting_id": meeting,
            "revision": revision, "event_id": event, "date": meta["date"], "title": meta.get("title", meeting),
            "audio_sha256": meta.get("audio_sha256"), "transcript_sha256": audit["transcript_sha256"],
            "template_sha256": audit.get("template_sha256"), "source": f"mcp://plaud/{meeting}",
            "evidence": "retained in private whosaid workspace"}, sort_keys=True, ensure_ascii=False) + "\n\n" + markdown)
        if same_revision:
            if prior["content"] != content:
                raise DeliveryError("Frozen outbox content changed; preserve it for review")
            db.execute("update outbox set verification_binding=? where event_id=?",
                       (hashlib.sha256(content.encode()).hexdigest(), event))
            return event
        db.execute("insert into outbox(event_id,meeting_id,revision,digest,parent,content,created_at,verification_binding) values(?,?,?,?,?,?,?,?)",
                   (event, meeting, revision, digest, prior["event_id"] if prior else None,
                    content, datetime.now(timezone.utc).isoformat(), hashlib.sha256(content.encode()).hexdigest()))
    return event


def context_json(result):
    if not isinstance(result, dict) or result.get("isError"):
        raise DeliveryError("OpenBrain context readback failed")
    for block in result.get("content", []):
        # Ignore Markdown narrative/quoted meeting text. Only the server's
        # standalone structured context block is a receipt.
        for match in re.finditer(r"\A```json\s*\n(.*?)\n```\s*\Z", block.get("text", ""), re.S):
            try:
                data = json.loads(match[1])
            except ValueError:
                continue
            if isinstance(data, dict) and "seed" in data and "canon" in data:
                return data
    raise DeliveryError("OpenBrain returned no verifiable context receipt")


def ownership_json(result, meeting):
    if not isinstance(result, dict) or result.get("isError"):
        raise DeliveryError("Meeting ownership lookup failed; no capture attempted")
    for block in result.get("content", []):
        match = re.fullmatch(r"```json\s*\n(.*?)\n```\s*", block.get("text", ""), re.S)
        if not match:
            continue
        data = json.loads(match[1])
        if (data.get("recording_id") != meeting.removeprefix("of_") or
                data.get("owner") != "whosaid-local" or data.get("epoch", 0) <= 0 or
                any(type(data.get(k)) is not int or data[k] < 0 for k in ("epoch", "revision")) or
                not isinstance(data.get("prior_thought_ids"), list) or
                any(not isinstance(x, str) or not re.fullmatch(UUID, x) for x in data["prior_thought_ids"]) or
                not (data.get("thought_id") is None or isinstance(data["thought_id"], str) and re.fullmatch(UUID, data["thought_id"])) or
                not (data.get("content_sha256") is None or isinstance(data["content_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", data["content_sha256"]))):
            raise DeliveryError("Local ownership unconfirmed; no capture attempted")
        return data
    raise DeliveryError("No structured meeting ownership receipt")


class HttpMcp:
    def __init__(self, cfg):
        self.url = validate_url(cfg["url"])
        self.command = cfg.get("token_command")
        if not isinstance(self.command, list) or not self.command or not all(isinstance(x, str) for x in self.command):
            raise DeliveryError("Delivery needs an explicit OAuth token-command argv")
        self.timeout = int(cfg.get("timeout", 90))
        self.session, self.serial = None, 0
        self.opener = urllib.request.build_opener(NoRedirect())
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "whosaid-delivery", "version": "1"}})
        self.request("notifications/initialized", {}, notification=True)

    def request(self, method, params, *, notification=False):
        token = subprocess.run(self.command, capture_output=True, text=True, timeout=30, check=True).stdout.strip()
        if not token or "\n" in token or "\r" in token:
            raise DeliveryError("Invalid delivery credential")
        self.serial += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.serial
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2024-11-05"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            # A socket timeout resets on every heartbeat. Bound the entire HTTP
            # exchange in a child process, including DNS and SSE heartbeat streams.
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_http-io"],
                input=json.dumps({"url": self.url, "headers": headers, "body": body, "timeout": self.timeout}),
                capture_output=True, text=True, timeout=self.timeout, check=True)
            received = json.loads(run.stdout)
            self.session = received.get("session") or self.session
            raw = received["body"].encode()
            if notification:
                return None
            if len(raw) > 4 * 1024 * 1024:
                raise DeliveryError("Delivery response exceeds bound")
            text = raw.decode()
            messages = [json.loads(text)] if text.lstrip().startswith("{") else [
                json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]
            for message in messages:
                if message.get("id") == self.serial and "result" in message and "error" not in message:
                    return message["result"]
        except (OSError, ValueError, subprocess.SubprocessError):
            raise DeliveryError("OpenBrain request failed; pending receipt retained") from None
        raise DeliveryError("OpenBrain request did not return a matching result")

    def call(self, name, args):
        return self.request("tools/call", {"name": name, "arguments": args})


def drain(workspace, cfg, *, rpc=None):
    if cfg.get("mode") != "pilot" or not cfg.get("file_ids"):
        raise DeliveryError("Sending requires pilot mode and an explicit recording allowlist")
    allowed = set(cfg["file_ids"])
    rpc = rpc or HttpMcp(cfg)
    result = {"delivered": 0, "failed": 0}
    # This lock serializes delivery across every process, including ambiguous
    # remote commits. It does not touch the independent audio-intake lock.
    from plaud_pipeline import locked
    lock_dir = Path(workspace) / "delivery-lock"
    with locked(lock_dir), connect(workspace) as db:
        rows = db.execute("select * from outbox where status!='delivered' order by created_at,revision").fetchall()
        for row in rows:
            if row["meeting_id"] not in allowed:
                continue
            try:
                if row["verification_binding"] != hashlib.sha256(row["content"].encode()).hexdigest():
                    raise DeliveryError("Queued summary lacks a current verification binding; re-verify and prepare it")
                ownership = ownership_json(rpc.call("meeting_ownership", {"recording_id": row["meeting_id"]}), row["meeting_id"])
                parent = db.execute("select * from outbox where event_id=?", (row["parent"],)).fetchone() if row["parent"] else None
                if parent and parent["status"] != "delivered":
                    raise DeliveryError("Prior meeting revision has no confirmed receipt")
                # Freeze arguments before sending. An ambiguous retry must send
                # exactly the same request so server-side durable dedup applies.
                if row["request"]:
                    args = json.loads(row["request"])
                    if args.get("content") != row["content"] or args.get("origin") != "whosaid-local":
                        raise DeliveryError("Frozen delivery request differs from verified content")
                else:
                    if parent and ownership["thought_id"] != parent["thought_id"]:
                        raise DeliveryError("Remote revision differs from local parent; reconcile before correction")
                    replay = not parent and ownership["revision"] > 0
                    if replay and ownership["content_sha256"] != row["verification_binding"]:
                        raise DeliveryError("Existing remote summary requires reconciliation before first local delivery")
                    revision = ownership["revision"] if replay else ownership["revision"] + 1
                    args = {"content": row["content"], "origin": "whosaid-local", "type": "observation",
                            "meeting": {"provider": "plaud", "recording_id": row["meeting_id"].removeprefix("of_"),
                                        "owner_epoch": ownership["epoch"], "revision": revision, "expected_revision": revision - 1}}
                    if not parent and ownership["prior_thought_ids"]:
                        args.update(supersedes=ownership["prior_thought_ids"], supersedes_as="supersedes")
                    if parent:
                        before = context_json(rpc.call("get_context", {"seed_id": parent["thought_id"], "hops": 1, "limit": 1}))
                        if before["canon"].get("is_canon") is not True:
                            raise DeliveryError("Prior summary already superseded outside this delivery chain")
                        args.update(supersedes=parent["thought_id"], supersedes_as="supersedes")
                    db.execute("update outbox set request=? where event_id=?", (json.dumps(args, sort_keys=True), row["event_id"]))
                    db.commit()
                contract = args.get("meeting", {})
                if (contract.get("owner_epoch") != ownership["epoch"] or
                        type(contract.get("revision")) is not int or
                        not ownership["revision"] <= contract["revision"] <= ownership["revision"] + 1 or
                        contract.get("expected_revision") != contract["revision"] - 1):
                    raise DeliveryError("Frozen request has stale ownership or revision")
                response = rpc.call("capture_thought", args)
                if not isinstance(response, dict) or response.get("isError"):
                    raise DeliveryError("Capture rejected; receipt withheld")
                text = "\n".join(b.get("text", "") for b in response.get("content", []))
                match = re.search(r"\bCaptured (" + UUID + r")\b", text, re.I)
                if not match:
                    raise DeliveryError("Capture has no confirmed thought identity")
                thought = match[1]
                check = context_json(rpc.call("get_context", {"seed_id": thought, "hops": 1, "limit": 1}))
                if (check["seed"].get("id") != thought or check["seed"].get("content") != args["content"] or
                        check["canon"].get("is_canon") is not True):
                    raise DeliveryError("Captured summary identity/content/currentness readback mismatch")
                saved = ownership_json(rpc.call("meeting_ownership", {"recording_id": row["meeting_id"]}), row["meeting_id"])
                if (saved["epoch"] != contract["owner_epoch"] or saved["revision"] != contract["revision"] or
                        saved["thought_id"] != thought or saved["content_sha256"] != row["verification_binding"]):
                    raise DeliveryError("Shared meeting revision receipt mismatch")
                predecessors = args.get("supersedes", [])
                if isinstance(predecessors, str): predecessors = [predecessors]
                for predecessor in predecessors:
                    old = context_json(rpc.call("get_context", {"seed_id": predecessor, "hops": 1, "limit": 1}))
                    if (old["seed"].get("id") != predecessor or
                            old["canon"].get("is_canon") is not False or old["canon"].get("current_id") != thought):
                        raise DeliveryError("Correction lineage was not confirmed")
                db.execute("update outbox set status='delivered',thought_id=?,error=null,delivered_at=? where event_id=?",
                           (thought, datetime.now(timezone.utc).isoformat(), row["event_id"]))
                result["delivered"] += 1
            except Exception as error:
                db.execute("update outbox set status='pending',attempts=attempts+1,error=? where event_id=?",
                           (type(error).__name__, row["event_id"]))
                result["failed"] += 1
            db.commit()
    return result


def status(workspace):
    if not (Path(workspace) / "delivery.sqlite").is_file():
        raise DeliveryError("Delivery workspace is not initialized")
    with connect(workspace) as db:
        return {"pending": db.execute("select count(*) from outbox where status!='delivered'").fetchone()[0],
                "failed": db.execute("select count(*) from outbox where error is not null").fetchone()[0],
                "delivered": db.execute("select count(*) from outbox where status='delivered'").fetchone()[0]}


def main():
    import tomllib
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("prepare", "send", "status"))
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--folder", type=Path)
    p.add_argument("--config", type=Path)
    a = p.parse_args()
    os.umask(0o077)
    try:
        if a.command == "prepare":
            if not a.folder:
                p.error("prepare requires --folder")
            print(json.dumps({"event_id": enqueue(a.workspace, a.folder), "remote_write": False}))
        elif a.command == "send":
            if not a.config:
                p.error("send requires --config")
            result = drain(a.workspace, tomllib.loads(a.config.read_text())["delivery"])
            print(json.dumps(result))
            return int(bool(result["failed"]))
        else:
            result = status(a.workspace)
            print(json.dumps(result))
            return int(bool(result["pending"] or result["failed"]))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        print("whosaid delivery: operation failed; private pending state retained", file=__import__("sys").stderr)
        return 1


def http_io():
    # Only the parent validates/configures the destination. No tools or browser
    # are involved. Authorization travels over stdin, never command arguments.
    payload = json.load(sys.stdin)
    request = urllib.request.Request(payload["url"], data=json.dumps(payload["body"]).encode(),
                                     headers=payload["headers"], method="POST")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=payload["timeout"]) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise DeliveryError("HTTP response exceeds bound")
        print(json.dumps({"session": response.headers.get("Mcp-Session-Id"), "body": raw.decode()}))


if __name__ == "__main__":
    if sys.argv[1:] == ["_http-io"]:
        try:
            http_io()
        except Exception:
            raise SystemExit(1) from None
    else:
        raise SystemExit(main())
