#!/usr/bin/env python3
"""Offline integration acceptance for tracked, classified personal worklists.

Synthetic corpora exercise the real builder and CLI. No models or services are
started; each CLI process has a 15-second deadline.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import tracking
import workspace
import wsconfig


class TrackingWorklistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tracking-worklist-test-")
        self.ws = Path(self.tmp.name)
        self.ws.joinpath("whosaid.toml").write_text(
            '[workspace]\nowner = "Speaker_A"\n[search]\nembed = false\n'
            '[groups]\nleadership = ["Speaker_B"]\n')
        self.cm, self.ai = [], []
        self.env = patch.dict(os.environ, {"WHOSAID_TODAY": "2020-01-10",
            "WHOSAID_OLLAMA": "http://127.0.0.1:9", "WHOSAID_OWNER": "",
            "WHOSAID_EMBED_FAKE": "", "WHOSAID_SPEAKER_DB": str(self.ws / "missing-speakers.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def commitment(self, ident="CM-001", text="I will send the sample guide.",
                   meeting="2020-01-01-0900", **kwargs):
        item = workspace.CommitmentItem(id=ident, text=text, speaker="Speaker_A",
            first_seen=meeting, last_seen=meeting, cue="i will",
            occurrences=[workspace.CmOccurrence(meeting=meeting, line=1, t_sec=0,
                         owner="Speaker_A", text=text)], **kwargs)
        self.cm.append(item)
        return item

    def action(self, ident, text, heading="Asks from team", meeting="2020-01-01-0900"):
        item = workspace.ActionItem(id=ident, text=text, owner="Speaker_A", type=heading,
            first_seen=meeting, last_seen=meeting,
            occurrences=[workspace.Occurrence(meeting=meeting, line=99)])
        self.ai.append(item)
        return item

    def persist(self):
        meetings = sorted({o.meeting for item in [*self.cm, *self.ai] for o in item.occurrences})
        for name, items in (("_commitments.json", self.cm), ("_action-items.json", self.ai)):
            self.ws.joinpath(name).write_text(json.dumps({"items": [asdict(i) for i in items],
                "folded_meetings": meetings, "similarity_threshold": 0.70, "next_id": len(items) + 1}))
        return meetings

    def build(self, owner="Speaker_A", as_of=None):
        meetings = self.persist()
        entries, payload = workspace.build_worklist(owner, self.cm, self.ai,
            wsconfig.load_config(self.ws), workspace.TextMatcher(threshold=0.70), meetings,
            today=date(2020, 1, 10), ws=self.ws, as_of=as_of)
        return {e["id"]: e for e in entries}, payload

    def cli(self, *args):
        self.persist()
        result = subprocess.run([sys.executable, str(Path(workspace.__file__)), "worklist",
            str(self.ws), "--json", *args], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def event(self, ident="CM-001", day="2020-01-02", kind="status", **kwargs):
        event = {"type": kind, "item_id": ident, "occurred_at": day,
                 "evidence": {"kind": "manual", "ref": "synthetic reviewed receipt"}, **kwargs}
        if kind == "status":
            event.setdefault("status", "delivered")
        return tracking.record_event(self.ws, event)

    def test_reviewed_delivery_overrides_open_corpus_before_ranking(self):
        item = self.commitment()
        initial, _ = self.build()
        self.assertTrue(initial[item.id]["open"])
        self.assertTrue(initial[item.id]["tier"])
        self.event()
        rows, _ = self.build()
        delivered = rows[item.id]
        self.assertEqual(item.status, "open", "extraction corpus must remain unchanged")
        self.assertEqual(delivered["status"], "delivered")
        self.assertFalse(delivered["open"])
        self.assertEqual((delivered["tier"], delivered["score"], delivered["why"]), ("", 0, []))
        self.assertTrue(delivered["evidence_available"])
        self.assertEqual([r["id"] for r in self.cli("--status", "delivered")["items"]], [item.id])

    def test_partial_milestones_stay_open_and_ranked_until_all_are_delivered(self):
        item = self.commitment(text="I will prepare the sample release.")
        tracking.add_milestones(self.ws, item.id, [
            {"id": "CM-001:draft", "text": "Prepare draft", "acceptance": "draft receipt"},
            {"id": "CM-001:ship", "text": "Ship release", "acceptance": "release receipt"},
        ], {"kind": "manual", "ref": "synthetic confirmed split"}, "2020-01-01")
        self.event("CM-001:draft")
        partial = self.build()[0][item.id]
        self.assertEqual(partial["status"], "partial")
        self.assertTrue(partial["open"])
        self.assertIn(partial["tier"], ("P1", "P2", "P3"))
        self.assertEqual(partial["remaining_milestones"], ["CM-001:ship"])
        self.event(day="2020-01-03")  # parent completion cannot hide the unfinished child
        blocked_completion = self.build()[0][item.id]
        self.assertEqual(blocked_completion["status"], "partial")
        self.assertTrue(blocked_completion["open"])
        self.assertTrue(blocked_completion["tier"])
        self.event("CM-001:ship", day="2020-01-04")
        completed = self.build()[0][item.id]
        self.assertEqual((completed["status"], completed["tier"]), ("delivered", ""))

    def test_accepted_handoff_moves_current_worklist_and_preserves_past_owner(self):
        item = self.commitment()
        request = self.event(kind="handoff_requested", owner="Speaker_C")
        self.assertIn(item.id, self.build("Speaker_A")[0])
        self.assertNotIn(item.id, self.build("Speaker_C")[0])
        self.event(day="2020-01-03", kind="handoff_accepted", owner="Speaker_C", request_event_id=request["id"])
        self.assertNotIn(item.id, self.build("Speaker_A")[0])
        reassigned = self.build("Speaker_C")[0][item.id]
        self.assertEqual(reassigned["next_action_owner"], "Speaker_C")
        self.assertEqual(reassigned["accepted_owner"], "Speaker_C")
        self.assertTrue(reassigned["open"])
        self.assertIn(item.id, self.build("Speaker_A", as_of="2020-01-02")[0])
        self.assertNotIn(item.id, self.build("Speaker_C", as_of="2020-01-02")[0])
        cli = self.cli("--owner", "Speaker_C", "--accepted-only")
        self.assertEqual([r["id"] for r in cli["items"]], [item.id])
        all_owners = {row["owner"]: row["items"] for row in self.cli("--all-owners")["owners"]}
        self.assertIn("Speaker_C", all_owners, "a new handoff owner must be discoverable without knowing their name")
        self.assertEqual([r["id"] for r in all_owners["Speaker_C"]], [item.id])

    def test_historical_acceptance_uses_visible_occurrences_not_current_title(self):
        item = self.commitment(text="I will send the sample guide.")
        item.occurrences = [
            workspace.CmOccurrence(meeting="2020-01-01-0900", line=1, t_sec=0,
                owner="Speaker_A", text="I might send the sample guide."),
            workspace.CmOccurrence(meeting="2020-01-03-0900", line=1, t_sec=0,
                owner="Speaker_A", text="I will send the sample guide."),
        ]
        item.last_seen = "2020-01-03-0900"
        historical = self.build(as_of="2020-01-02")[0][item.id]
        self.assertFalse(historical["accepted_owner"])
        self.assertEqual(historical["acceptance_evidence"], [])
        self.assertEqual(historical["independent_occurrences"], 1)
        self.assertEqual(self.build()[0][item.id]["accepted_owner"], "Speaker_A")

    def classification_fixture(self):
        self.commitment(text="I will prepare the chart tomorrow.")
        self.action("AI-001", "Please send the sample deployment guide tomorrow.")
        self.action("AI-002", "Always include the review checklist tomorrow.", "Standing directives")
        self.action("AI-003", "[inferred] Schedule a planning session tomorrow.", "Inferred next steps")

    def test_cli_kind_and_accepted_only_filter_classified_rows(self):
        self.classification_fixture()
        all_rows = self.cli()["items"]
        self.assertEqual(len(all_rows), 4)
        self.assertEqual([r["id"] for r in self.cli("--kind", "request")["items"]], ["AI-001"])
        self.assertEqual([r["id"] for r in self.cli("--accepted-only")["items"]], ["CM-001"])
        self.assertEqual(self.cli("--kind", "request", "--accepted-only")["items"], [])

    def test_unaccepted_requests_directives_and_inferred_steps_never_become_overdue(self):
        self.classification_fixture()
        rows, _ = self.build()
        self.assertTrue(any(w.startswith("overdue=") for w in rows["CM-001"]["why"]))
        for ident in ("AI-001", "AI-002", "AI-003"):
            with self.subTest(item=ident):
                self.assertFalse(rows[ident]["overdue_eligible"])
                self.assertFalse(any(w.startswith(("overdue=", "due=")) for w in rows[ident]["why"]))
        for ident in ("AI-002", "AI-003"):
            self.assertEqual((rows[ident]["tier"], rows[ident]["score"]), ("", 0))

    def test_similar_unaccepted_request_does_not_fold_into_accepted_commitment(self):
        cm = self.commitment(text="I will send the sample deployment guide tomorrow.")
        ai = self.action("AI-001", "Please send the sample deployment guide tomorrow.")
        matcher = workspace.TextMatcher(threshold=0.70)
        left, right = workspace.normalize_text(cm.text), workspace.normalize_text(ai.text)
        self.assertTrue(matcher.matches(left, right, workspace.similarity(left, right)), "fixture must otherwise pass text-similarity folding")
        rows, _ = self.build()
        self.assertEqual(set(rows), {cm.id, ai.id})
        self.assertEqual(rows[cm.id]["accepted_owner"], "Speaker_A")
        self.assertFalse(rows[ai.id]["accepted_owner"])
        self.assertEqual(rows[ai.id]["kind"], "request")
        self.assertEqual(rows[cm.id]["also"], [])

    def test_similar_accepted_tasks_do_not_share_reviewed_completion(self):
        cm = self.commitment(text="I will send the deployment guide for component alpha tomorrow.")
        ai = self.action("AI-001", "Please send the deployment guide for component beta tomorrow.")
        request = self.event(ai.id, kind="handoff_requested", owner="Speaker_A")
        self.event(ai.id, day="2020-01-03", kind="handoff_accepted", owner="Speaker_A", request_event_id=request["id"])
        self.event(ai.id, day="2020-01-04")
        rows, _ = self.build()
        self.assertEqual(set(rows), {cm.id, ai.id})
        self.assertEqual(rows[cm.id]["status"], "open")
        self.assertTrue(rows[cm.id]["tier"])
        self.assertEqual(rows[cm.id]["also"], [])
        self.assertEqual(rows[cm.id]["tracking_history"], [])
        self.assertEqual(rows[ai.id]["status"], "delivered")
        self.assertEqual(rows[ai.id]["tier"], "")

    def test_explicit_references_share_status_even_when_display_text_differs(self):
        cm = self.commitment(text="I will publish the sample guide.", ai_refs="AI-001")
        ai = self.action("AI-001", "Distribute the approved onboarding documentation to reviewers.")
        self.event(ai.id)
        rows, _ = self.build()
        self.assertEqual(set(rows), {cm.id})
        self.assertEqual(rows[cm.id]["also"], [ai.id])
        self.assertEqual(rows[cm.id]["status"], "delivered")
        self.assertEqual(rows[cm.id]["tier"], "")

    def test_unique_source_pointer_shares_reviewed_completion(self):
        cm = self.commitment(text="Publish the sample guide.")
        ai = self.action("AI-001", "Distribute approved documentation to reviewers.")
        cm.occurrences[0].source = "action-items"
        cm.occurrences[0].line = ai.occurrences[0].line
        self.event(ai.id)
        rows, _ = self.build()
        self.assertEqual(set(rows), {cm.id})
        self.assertEqual(rows[cm.id]["also"], [ai.id])
        self.assertEqual(rows[cm.id]["status"], "delivered")

    def chat(self, meeting, text, chat_name, epoch=1577840400000):
        folder = self.ws / meeting
        folder.mkdir()
        folder.joinpath("teams.speakers.txt").write_text(f"[01:00:00] Speaker_A: {text}\n")
        folder.joinpath("teams.diarization.json").write_text(json.dumps({
            "source": {"kind": "teams-chat", "chat_name": chat_name, "day": meeting[:10]},
            "segments": [{"start": 3600, "epoch_ms": epoch, "mid": str(epoch),
                          "speaker": "Speaker_A", "text": text}],
        }))

    def test_confirmed_chat_copies_collapse_before_scoring_but_later_repeat_counts(self):
        text = "I will write the sample guide covering all supported options and share the full document with the review group."
        item = self.commitment(text=text)
        folders = [f"2020-01-01-0100-{i}" for i in range(3)]
        item.occurrences = []
        for n, folder in enumerate(folders):
            self.chat(folder, text, f"Sample {n}")
            item.occurrences.append(workspace.CmOccurrence(meeting=folder, line=1, t_sec=3600,
                                    owner="Speaker_A", text=text))
        item.first_seen, item.last_seen = folders[0], folders[-1]
        unconfirmed = self.build()[0][item.id]
        self.assertEqual((unconfirmed["independent_occurrences"], unconfirmed["tier"]), (3, "P1"))
        with self.ws.joinpath("whosaid.toml").open("a") as config:
            config.write('\n[messages.conversation_aliases]\n"Sample 0"="confirmed"\n"Sample 1"="confirmed"\n"Sample 2"="confirmed"\n')
        confirmed, payload = self.build()
        self.assertEqual(confirmed[item.id]["independent_occurrences"], 1)
        self.assertEqual(confirmed[item.id]["tier"], "P2")
        self.assertFalse(any("occurrences" in w or "meetings" in w for w in confirmed[item.id]["why"]))
        self.assertEqual(payload["items"][0]["occurrences"], 3, "raw meeting provenance stays visible")
        later = "2020-01-02-0100"
        self.chat(later, text, "Sample 0", epoch=1577926800000)
        item.occurrences.append(workspace.CmOccurrence(meeting=later, line=1, t_sec=3600, owner="Speaker_A", text=text))
        item.last_seen = later
        repeated = self.build()[0][item.id]
        self.assertEqual(repeated["independent_occurrences"], 2)
        self.assertIn("2 independent occurrences", repeated["why"])
        self.assertGreater(repeated["score"], confirmed[item.id]["score"])

    def test_repeated_audio_sentences_score_once_per_meeting(self):
        item = self.commitment()
        item.occurrences = []
        for day in (1, 2):
            meeting = f"2020-01-{day:02}-0900"
            folder = self.ws / meeting
            folder.mkdir()
            folder.joinpath("audio.speakers.txt").write_text(
                f"[00:00:00] Speaker_A: {item.text}\n[00:00:01] Speaker_A: {item.text}\n")
            item.occurrences += [workspace.CmOccurrence(meeting=meeting, line=n+1, t_sec=n,
                                owner="Speaker_A", text=item.text) for n in (0, 1)]
        item.last_seen = "2020-01-02-0900"
        row = self.build()[0][item.id]
        self.assertEqual(row["independent_occurrences"], 2)
        self.assertIn("2 meetings", row["why"])
        self.assertEqual(row["tier"], "P2")

    def test_ledger_and_config_content_changes_invalidate_worklist_cache_digests(self):
        self.commitment()
        self.event(status="in_progress")
        _, payload = self.build()
        digests = workspace.corpora_digests(self.ws)
        cache = self.ws / workspace.worklist_cache_filename("Speaker_A")
        self.assertTrue(workspace.write_worklist_cache(cache, payload, digests))
        original = cache.read_bytes()
        self.assertFalse(workspace.write_worklist_cache(cache, payload, digests))
        self.assertEqual(cache.read_bytes(), original)
        for filename in ("_tracking.json", "whosaid.toml"):
            path = self.ws / filename
            old = path.stat()
            prior = workspace.corpora_digests(self.ws)
            if filename == "_tracking.json":
                self.event(day="2020-01-03")
            else:
                with path.open("a") as config:
                    config.write('\n[commitments.weights]\nrepeat = 99\n')
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
            updated = workspace.corpora_digests(self.ws)
            self.assertNotEqual(prior[filename], updated[filename])
            self.assertNotEqual(json.loads(cache.read_text())["corpora"], updated)
            _, refreshed = self.build()
            self.assertTrue(workspace.write_worklist_cache(cache, refreshed, updated))
            self.assertEqual(json.loads(cache.read_text())["corpora"], workspace.corpora_digests(self.ws))


if __name__ == "__main__":
    unittest.main()
