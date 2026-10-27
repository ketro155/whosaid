"""Read-only Plaud raw-audio transport. No vendor transcription dependency."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


class PlaudError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PlaudError("HTTP redirect refused; refresh the Plaud file detail")


def validate_url(url: str, *, api: bool = False) -> str:
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise PlaudError("Expected an HTTPS URL without credentials or a custom port")
    host = p.hostname.lower()
    if api and not (host == "api.plaud.ai" or (host.endswith(".plaud.ai") and host.startswith("api"))):
        raise PlaudError("Unrecognized Plaud API host")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as e:
        raise PlaudError("Cannot resolve Plaud download host") from e
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise PlaudError("Private or non-routable URL refused")
    return url


def token_from_config(cfg: dict) -> str:
    token = os.environ.get("PLAUD_TOKEN", "").strip()
    if not token:
        command = cfg.get("token_command", [])
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise PlaudError("Set PLAUD_TOKEN or [plaud] token_command (an argv array)")
        try:
            r = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
            token = r.stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            raise PlaudError("Plaud credential command failed; refresh the stored credential") from e
    if not token or "\n" in token or "\r" in token:
        raise PlaudError("Invalid Plaud credential")
    return token


class PlaudClient:
    def __init__(self, token: str, domain: str = "https://api.plaud.ai"):
        self.token = token
        self.domain = validate_url(domain.rstrip("/"), api=True)
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path: str) -> dict | list:
        for _ in range(3):
            url = validate_url(self.domain + path, api=True)
            req = urllib.request.Request(url, headers={"Authorization": self.token, "Accept": "application/json",
                                                       "User-Agent": "Mozilla/5.0"})
            try:
                with self.opener.open(req, timeout=60) as r:
                    payload = r.read(32 * 1024 * 1024 + 1)
                if len(payload) > 32 * 1024 * 1024:
                    raise PlaudError("Plaud API response too large")
                data = json.loads(payload)
            except urllib.error.HTTPError as e:
                # Do not expose signed URLs, credentials, or response bodies in logs.
                raise PlaudError(f"Plaud API returned HTTP {e.code}") from None
            except (OSError, ValueError) as e:
                raise PlaudError("Plaud API request failed") from e
            if isinstance(data, dict) and data.get("status") == -302:
                domain = data.get("data", {}).get("domains", {}).get("api", "")
                self.domain = validate_url(domain if domain.startswith("https://") else "https://" + domain, api=True)
                continue
            if isinstance(data, dict) and data.get("status") not in (None, 0, 200):
                raise PlaudError("Plaud API returned an unsuccessful status")
            return data
        raise PlaudError("Too many Plaud region changes")

    def list_files(self) -> list[dict]:
        data = self.get("/file/simple/web")
        if isinstance(data, dict):
            data = next((data[k] for k in ("data_file_list", "data", "files", "results")
                         if isinstance(data.get(k), list)), None)
        if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
            raise PlaudError("Unexpected Plaud file-list schema; do not advance intake")
        return [x for x in data if not x.get("is_trash")]

    def detail(self, file_id: str) -> dict:
        data = self.get("/file/detail/" + urllib.parse.quote(file_id, safe=""))
        if isinstance(data, dict):
            data = data.get("data", data)
        if not isinstance(data, dict):
            raise PlaudError("Unexpected Plaud file-detail schema")
        return data

    def download(self, url: str, target: Path, max_bytes: int = 1024 * 1024 * 1024) -> str:
        validate_url(url)
        temp = target.with_suffix(".partial")
        digest = hashlib.sha256()
        size = 0
        try:
            # Deliberately no Authorization header on CDN/audio requests.
            with self.opener.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=120) as r, temp.open("wb") as out:
                os.chmod(temp, 0o600)
                while chunk := r.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise PlaudError("Plaud audio exceeds the configured size limit")
                    out.write(chunk)
                    digest.update(chunk)
                out.flush()
                os.fsync(out.fileno())
            if not size:
                raise PlaudError("Plaud returned empty audio")
            temp.replace(target)
            return digest.hexdigest()
        except Exception:
            temp.unlink(missing_ok=True)
            raise PlaudError("Plaud audio download failed; retry will obtain a fresh URL") from None


class Plaud4Client(PlaudClient):
    """Plaud 4.0 workspace routes, observed in the current first-party Web app.

    Session JSON belongs outside the repository, mode 0600. Only the auth
    refresh endpoint uses POST; no recording or vendor-generated note is edited.
    """
    def __init__(self, session_file: Path, domain: str = "https://api.plaud.ai", *, since: str | None = None,
                 timezone: str = "America/New_York"):
        self.session_file = session_file.expanduser()
        if self.session_file.stat().st_mode & 0o077:
            raise PlaudError("Plaud session file must be private (chmod 600)")
        self.session = json.loads(self.session_file.read_text())
        self.since_ms = (int(datetime.fromisoformat(since).replace(tzinfo=ZoneInfo(timezone)).timestamp() * 1000)
                         if since else None)
        if not all(self.session.get(k) for k in ("token", "workspace_id")):
            raise PlaudError("Plaud 4.0 session lacks its workspace token")
        super().__init__(self.session["token"], domain)

    def _request(self, path: str, *, refresh: bool = False):
        headers = {"Authorization": self.session.get("tokenType", "Bearer") + " " +
                   self.session["refreshToken" if refresh else "token"],
                   "x-scope-id": self.session["workspace_id"], "x-scope-type": "workspace",
                   "app-platform": "web", "edit-from": "web", "User-Agent": "Mozilla/5.0",
                   "Accept": "application/json"}
        request = urllib.request.Request(validate_url(self.domain + path, api=True), headers=headers,
                                         method="POST" if refresh else "GET", data=b"" if refresh else None)
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise PlaudError("Plaud response too large")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            raise PlaudError(f"Plaud 4.0 returned HTTP {e.code}") from None

    def refresh(self):
        if not self.session.get("refreshToken"):
            raise PlaudError("Plaud session cannot refresh; sign in again")
        data = self._request("/user-app/auth/workspace/refresh/" +
                             urllib.parse.quote(self.session["workspace_id"], safe=""), refresh=True)
        payload = data.get("data", {})
        token = payload.get("workspace_token") or payload.get("access_token")
        if data.get("status") != 0 or not token:
            raise PlaudError("Plaud workspace refresh rejected; sign in again")
        self.session.update(token=token, expiresAt=time.time() * 1000 + payload["expires_in"] * 1000)
        if payload.get("refresh_token"):
            self.session["refreshToken"] = payload["refresh_token"]
        if payload.get("refresh_expires_in") is not None:
            self.session["refreshExpiresAt"] = time.time() * 1000 + payload["refresh_expires_in"] * 1000
        from openbrain_summary import atomic_write
        atomic_write(self.session_file, json.dumps(self.session))

    def get(self, path: str):
        if self.session.get("expiresAt", 0) < (time.time() + 300) * 1000:
            self.refresh()
        data = self._request(path)
        if data.get("status") == -419:
            self.refresh()
            data = self._request(path)
        if data.get("status") != 0:
            raise PlaudError("Plaud 4.0 returned an unsuccessful status")
        return data.get("data")

    def list_files(self):
        # The agenda uses this recording index. Walking the Library tree visits
        # each meeting's audio/notes folder and is both slow and semantically
        # wrong for raw intake. Apply the date bound server-side and page fully.
        files, seen_cursors, cursor = {}, set(), None
        for _ in range(100):
            query = {"page_size": 200, "sort_by": "created", "sort_order": "asc"}
            if self.since_ms is not None:
                query["date_from"] = self.since_ms
            if cursor:
                query["cursor"] = cursor
            data = self.get("/file-app/v4/recordings/all?" + urllib.parse.urlencode(query))
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise PlaudError("Unexpected Plaud 4.0 recording-list schema")
            for item in data["items"]:
                if not isinstance(item, dict) or not item.get("file_id"):
                    raise PlaudError("Plaud recording index item lacks a file id")
                files[item["file_id"]] = {**item, "id": item["file_id"],
                                          "start_time": item.get("created_at_show_ms")}
            cursor = data.get("next_cursor")
            if not cursor:
                return list(files.values())
            if cursor in seen_cursors:
                raise PlaudError("Plaud repeated a pagination cursor; intake incomplete")
            seen_cursors.add(cursor)
        raise PlaudError("Plaud recording pagination limit exceeded; intake incomplete")

    def detail(self, file_id):
        data = self.get("/file-app/v4/files/detail/" + urllib.parse.quote(file_id, safe=""))
        audio = next((x for x in data.get("objects", []) if x.get("object_type") == "AUDIO"), {})
        return {"presigned_url": audio.get("content_url"), "name": data.get("node", {}).get("name"),
                "start_time": data.get("meta", {}).get("start_time")}
