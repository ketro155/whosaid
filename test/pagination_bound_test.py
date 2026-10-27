#!/usr/bin/env python3
"""Regression tests for following default MCP pages (issue #112).

Run: python3 test/pagination_bound_test.py

The real MCP tool receives synthetic graph rows. Only the subprocess boundary
is replaced; ordering, argument handling, pagination and response metadata run
through the production item and worklist paths. The existing workspace test module
supplies its SDK stub when the optional MCP package is unavailable.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_workspace_tools_test import mcp_server


class PaginationBoundTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="whosaid-pagination-")
        self.addCleanup(self.workspace.cleanup)
        self.expected_ids = [f"AI-{i:03d}" for i in range(1, 251)]
        rows = [{"id": ident, "text": f"Review task {ident}", "owner": "Owner_Example",
                 "type": "task", "status": "open", "occurrences": 1}
                for ident in reversed(self.expected_ids)]
        runner = patch.object(mcp_server, "_run_ws", return_value=(rows, None))
        self.runner = runner.start()
        self.addCleanup(runner.stop)

    def page(self, **kwargs):
        result = mcp_server.whosaid_items(workspace=self.workspace.name, **kwargs)
        self.assertTrue(result["ok"], result)
        return result

    def test_next_offset_preserves_default_bound(self):
        first = self.page()
        self.assertEqual(first["count"], 50)
        self.assertEqual(first["next_offset"], 50)

        second = self.page(offset=first["next_offset"])
        self.assertEqual(second["count"], 50,
                         "following next_offset without a limit must keep the 50-item bound")
        self.assertEqual(second["limit"], 50)
        self.assertEqual(second["total"], 250)
        self.assertEqual(second["omitted"], 150)
        self.assertEqual(second["next_offset"], 100)
        self.assertEqual([row["id"] for row in second["items"]], self.expected_ids[50:100])

    def test_default_traversal_returns_every_item_once(self):
        offset = 0
        seen = []
        page_sizes = []
        while offset is not None:
            result = self.page(offset=offset)
            page_sizes.append(result["count"])
            seen.extend(row["id"] for row in result["items"])
            offset = result.get("next_offset")
            self.assertLessEqual(len(page_sizes), 5, "pagination must terminate")
        self.assertEqual(seen, self.expected_ids)
        self.assertEqual(len(set(seen)), 250)
        self.assertEqual(page_sizes, [50, 50, 50, 50, 50])
        self.assertNotIn("omitted", result)

    def test_explicit_limit_remains_authoritative(self):
        result = self.page(limit=75, offset=50)
        self.assertEqual(result["count"], 75)
        self.assertEqual(result["limit"], 75)
        self.assertEqual(result["next_offset"], 125)
        self.assertEqual(result["omitted"], 125)
        self.assertEqual([row["id"] for row in result["items"]], self.expected_ids[50:125])

    def test_worklist_offset_preserves_default_bound(self):
        (Path(self.workspace.name) / "_commitments.json").write_text('{"items": []}\n')
        rows = [{"id": f"CM-{i:03d}", "tier": "P1", "text": f"Review task {i}"}
                for i in range(1, 251)]
        self.runner.return_value = ({"owner": "Owner_Example", "items": rows}, None)
        first = mcp_server.whosaid_worklist(workspace=self.workspace.name)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["count"], 50)
        second = mcp_server.whosaid_worklist(workspace=self.workspace.name, offset=first["count"])
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["count"], 50,
                         "worklist offset without a limit must keep the 50-item bound")
        self.assertEqual(first["next_offset"], 50)
        self.assertEqual(second["next_offset"], 100)
        self.assertEqual(second["omitted"], 150)
        self.assertEqual([row["id"] for row in second["items"]], [row["id"] for row in rows[50:100]])
        explicit = mcp_server.whosaid_worklist(workspace=self.workspace.name, limit=75, offset=50)
        self.assertEqual(explicit["count"], 75)
        self.assertEqual(explicit["limit"], 75)
        self.assertEqual(explicit["next_offset"], 125)


if __name__ == "__main__":
    unittest.main()
