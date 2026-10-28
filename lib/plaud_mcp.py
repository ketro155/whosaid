"""Structured, read-only intake through the pinned official Plaud MCP package.

The vendor owns OAuth storage and refresh. This transport never reads its token
files, invokes login during a scheduled run, or parses CLI display tables.
"""
from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
import urllib.request
from datetime import date, timedelta

from plaud_client import NoRedirect, PlaudClient, PlaudError


def tool_json(result):
    if not isinstance(result, dict) or result.get("isError"):
        raise PlaudError("Official Plaud tool failed; check login or service availability")
    if isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") != "text":
            continue
        text = block.get("text", "").strip()
        # The official server wraps recording JSON in a randomized data fence.
        match = re.search(r'^<(untrusted-user-data-[a-zA-Z0-9-]+)\b[^>]*>\s*', text, re.M)
        if match:
            end = text.rfind("</" + match[1] + ">")
            if end < match.end():
                raise PlaudError("Unclosed Plaud data fence")
            text = text[match.end():end].strip()
        try:
            return json.loads(text)
        except ValueError:
            continue
    raise PlaudError("Official Plaud returned no structured JSON; intake withheld")


class StdioMcp:
    def __init__(self, command, *, timeout=90, timezone="America/New_York"):
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise PlaudError("[plaud] mcp_command must be a nonempty argv array")
        env = {k: v for k, v in os.environ.items() if k in
               ("PATH", "HOME", "USER", "LOGNAME", "LANG", "SSL_CERT_FILE", "TMPDIR")}
        env.update(TZ=timezone, DO_NOT_TRACK="1", PLAUD_TELEMETRY_DISABLED="1")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=env, bufsize=0)
        self.timeout, self.serial, self.buffer = timeout, 0, b""
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "whosaid-intake", "version": "1"}})
            self.notify("notifications/initialized", {})
        except Exception:
            self.close()
            raise

    def notify(self, method, params):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params):
        self.serial += 1
        expected = self.serial
        message = {"jsonrpc": "2.0", "id": expected, "method": method, "params": params}
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    value = json.loads(line)
                except ValueError:
                    raise PlaudError("Invalid MCP protocol output") from None
                if value.get("id") == expected:
                    if "error" in value:
                        raise PlaudError("MCP request failed")
                    return value.get("result")
                if "id" in value and "method" in value:
                    raise PlaudError("Unexpected interactive MCP request in unattended intake")
            left = deadline - time.monotonic()
            if left <= 0 or not self.selector.select(left):
                raise PlaudError("Official Plaud MCP timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise PlaudError("Official Plaud MCP exited before its response")
            self.buffer += chunk
            if len(self.buffer) > 32 * 1024 * 1024:
                raise PlaudError("MCP response exceeds the intake bound")

    def call(self, name, args):
        return tool_json(self.request("tools/call", {"name": name, "arguments": args}))

    def close(self):
        self.selector.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout):
            pipe.close()


class PlaudMcpClient:
    download = PlaudClient.download

    def __init__(self, cfg, *, rpc=None):
        self.cfg = cfg
        self.rpc = rpc or StdioMcp(cfg.get("mcp_command"), timeout=int(cfg.get("mcp_timeout", 90)),
                                   timezone=cfg.get("timezone", "America/New_York"))
        self.opener = urllib.request.build_opener(NoRedirect())

    def list_files(self):
        # Widen server dates; pipeline applies the exact local cutoff afterward.
        start = (date.fromisoformat(self.cfg["since"]) - timedelta(days=1)).isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
        records, seen = [], set()
        for page in range(1, int(self.cfg.get("max_pages", 1000)) + 1):
            data = self.rpc.call("list_files", {"date_from": start, "date_to": end,
                                               "page": page, "page_size": 100})
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise PlaudError("Unexpected official Plaud inventory schema")
            if not rows:
                return records
            added = 0
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                    raise PlaudError("Official inventory lacks a recording identity")
                key = row["id"].removeprefix("of_")
                if key not in seen:
                    seen.add(key)
                    records.append({**row, "id": key})
                    added += 1
            if not added:
                raise PlaudError("Official inventory repeated a page; completion withheld")
        raise PlaudError("Official inventory page bound reached; completion withheld")

    def detail(self, file_id):
        data = self.rpc.call("get_file", {"file_id": file_id})
        if not isinstance(data, dict) or str(data.get("id", "")).removeprefix("of_") != file_id:
            raise PlaudError("Official file response does not match the requested identity")
        return data

    def transcript(self, file_id):
        """Read the raw transcript completely; never substitute polished notes."""
        segments, cursor, seen = [], None, set()
        total = None
        for _ in range(int(self.cfg.get("max_pages", 1000))):
            args = {"file_id": file_id, "block": "transaction", "limit": 100}
            if cursor:
                args["cursor"] = cursor
            data = self.rpc.call("get_transcript", args)
            if (not isinstance(data, dict) or str(data.get("file_id", "")).removeprefix("of_") != file_id.removeprefix("of_") or
                    data.get("block") != "transaction" or not isinstance(data.get("segments"), list) or
                    type(data.get("total")) is not int or data["total"] < 0):
                raise PlaudError("Unexpected official raw transcript schema")
            if total is not None and data["total"] != total:
                raise PlaudError("Plaud transcript changed during pagination; retry the complete read")
            total = data["total"]
            if data.get("offset") != len(segments) or data.get("returned") != len(data["segments"]):
                raise PlaudError("Plaud transcript page offset/count mismatch")
            segments.extend(data["segments"])
            cursor = data.get("next_cursor")
            if not cursor:
                if len(segments) != total:
                    raise PlaudError("Plaud transcript read incomplete")
                return {"file_id": file_id, "block": "transaction", "segments": segments}
            if not isinstance(cursor, str) or cursor in seen or not data["segments"]:
                raise PlaudError("Plaud transcript cursor did not advance")
            seen.add(cursor)
        raise PlaudError("Plaud transcript page bound reached")

    def close(self):
        close = getattr(self.rpc, "close", None)
        if close:
            close()
