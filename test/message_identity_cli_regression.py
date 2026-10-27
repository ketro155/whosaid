#!/usr/bin/env python3
"""Portable, synthetic CLI regression: same fixture/assertions on either revision."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("repo", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
repo = args.repo.resolve()
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=True)
ws = out / "workspace"
ws.mkdir()
body = "I will send the sample guide."
original = "2030-01-01-0000"
copy = "2030-01-01-0000-2"
later = "2030-01-02-0000"
fixture = {"whosaid.toml": '[search]\nembed = false\n'}
for folder, message, epoch in ((original, "sample-original", 1893456000000),
                               (copy, "sample-original", 1893456000000),
                               (later, "sample-later", 1893542400000)):
    fixture[f"{folder}/teams.speakers.txt"] = f"[00:00:00] Speaker A: {body}\n"
    fixture[f"{folder}/teams.diarization.json"] = json.dumps({
        "source": {"kind": "teams-chat", "chat_id": "sample-conversation",
                   "chat_name": "Sample discussion", "day": folder[:10]},
        "segments": [{"start": 0, "end": 0, "speaker": "Speaker A", "text": body,
                      "epoch_ms": epoch, "mid": message, "message_id": message}],
    }, sort_keys=True) + "\n"
for name, data in fixture.items():
    path = ws / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
fixture_hash = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
# The same guard runs in both legs and makes Python network attempts fail visibly.
guard = out / "network-guard"
guard.mkdir()
(guard / "sitecustomize.py").write_text(
    'import socket\ndef denied(*args, **kwargs):\n'
    '    raise RuntimeError("network disabled for synthetic CLI regression")\n'
    'socket.socket.connect = denied\nsocket.socket.connect_ex = denied\n'
    'socket.create_connection = denied\n')
env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(guard),
       "WHOSAID_PYTHON": sys.executable, "WHOSAID_EMBED_FAKE": "",
       "WHOSAID_OLLAMA": "http://127.0.0.1:9", "WHOSAID_WORKSPACE": str(ws),
       "WHOSAID_OWNER": "", "WHOSAID_SPEAKER_DB": str(out / "no-speakers.json")}
commands = []

def run(name, command):
    print(f"{name}: starting (20-second bound)", flush=True)
    start = time.monotonic()
    proc = subprocess.Popen(command, cwd=repo, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        raise RuntimeError(f"{name}: exceeded 20 seconds")
    (out / f"{name}.stdout").write_text(stdout)
    (out / f"{name}.stderr").write_text(stderr)
    commands.append({"name": name, "argv": command, "exit_code": proc.returncode,
                     "elapsed_seconds": round(time.monotonic() - start, 3)})
    if proc.returncode:
        raise RuntimeError(f"{name}: CLI exited {proc.returncode}: {stderr}")
    print(f"{name}: completed", flush=True)
    return stdout

run("index", ["bash", str(repo / "whosaid"), "index", str(ws), "--no-embed"])
query = ["bash", str(repo / "whosaid"), "search", str(ws), "guide", "--mode", "exact", "-k", "10", "--json"]
hits = json.loads(run("search", query))
copy_hits = json.loads(run("copy-filter", query + ["--meeting", copy]))
with sqlite3.connect(ws / "_search.db") as db:
    raw_count = db.execute("SELECT count(*) FROM seg").fetchone()[0]
checks = {
    "two_independent_source_message_hits": len(hits) == 2,
    "one_original_hit_with_both_copy_citations": sum(
        {p["meeting"] for p in hit.get("provenance", [])} == {original, copy}
        for hit in hits) == 1,
    "later_distinct_repeat_retained": sum(hit["meeting"] == later for hit in hits) == 1,
    "all_three_raw_turns_retained": raw_count == 3,
    "copied_source_folder_still_queryable": len(copy_hits) == 1 and copy_hits[0]["meeting"] == copy,
}
result = {"test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          "fixture_sha256": fixture_hash, "commands": commands, "hit_count": len(hits),
          "raw_turn_count": raw_count, "provenance_counts": sorted(len(h.get("provenance", [])) for h in hits),
          "checks": checks, "passed": all(checks.values())}
(out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({k: v for k, v in result.items() if k != "commands"}, indent=2), flush=True)
sys.exit(0 if result["passed"] else 1)
