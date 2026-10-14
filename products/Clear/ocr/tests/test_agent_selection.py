import json
import unittest

import test_agent_policy
from ocr_workbench.agent.selection import create_selection, read_selection, grant_selection, validate_snapshot
from ocr_workbench.agent.context import AgentContext
from ocr_workbench.agent.contracts import GetWorkspaceContext
from ocr_workbench.documents import Documents
from ocr_workbench.store import Conflict


class SelectionTests(unittest.TestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    def lazy_document(self, count=3):
        key = 'synthetic-pdf'
        with self.store.transaction() as db:
            db.execute("INSERT INTO documents(id,project_id,name,kind,sha256,page_count,status,metadata,created,updated,original_path) VALUES(?,?,?,'pdf',?,?,'ready','{}','now','now','synthetic.pdf')",
                       (key, self.project, '合成未渲染文档', 'source-hash', count))
            Documents._insert_pages(db, key, [{"page_number": n, "render_dpi": 72} for n in range(1, count + 1)])
        return key

    def test_lazy_range_grants_only_selected_pages_and_detects_changes(self):
        doc = self.lazy_document()
        created = create_selection(self.agent, self.project, {"document_ranges": [{"document_id": doc, "first_page": 1, "last_page": 2}],
            "engines": ['ppocr'], "allow_processing": True}, available_engines=['ppocr'])
        snapshot = read_selection(self.agent, self.project, created['selection_token'])
        self.assertEqual([p['page_number'] for p in snapshot['pages']], [1, 2])
        self.assertTrue(all(p['image_id'] is None and p['version_id'] is None for p in snapshot['pages']))
        grant_selection(self.policy, self.run, snapshot)
        self.policy.authorize(self.run, 1, 'process_pages', {"document_id": doc, "page_numbers": [1, 2]})
        with self.assertRaises(ValueError):
            self.policy.authorize(self.run, 1, 'process_pages', {"document_id": doc, "page_numbers": [3]})
        with self.store.transaction() as db:
            db.execute("UPDATE pages SET render_parameters='changed' WHERE id=?", (snapshot['pages'][0]['page_id'],))
        with self.assertRaises(Conflict):
            read_selection(self.agent, self.project, created['selection_token'])

    def test_cross_project_invalid_ranges_and_deduplication(self):
        doc = self.lazy_document()
        for selected in ({"document_id": self.other_photo['id'], "first_page": 1, "last_page": 1},
                         {"document_id": doc, "first_page": 3, "last_page": 2},
                         {"document_id": doc, "first_page": 1, "last_page": 4}):
            with self.assertRaises(ValueError):
                create_selection(self.agent, self.project, {"document_ranges": [selected]}, available_engines=[])
        selected = {"document_id": doc, "first_page": 1, "last_page": 2}
        created = create_selection(self.agent, self.project, {"document_ranges": [selected, selected]}, available_engines=[])
        self.assertEqual(len(created['selection']['pages']), 2)

    def test_thousand_page_selection_is_bounded_and_pageable(self):
        doc = self.lazy_document(1000)
        created = create_selection(self.agent, self.project, {"document_ranges": [{"document_id": doc, "first_page": 1, "last_page": 1000}]}, available_engines=[])
        run = {**self.run, 'context': {'selection': created['selection']}}
        context = AgentContext(self.agent, None)
        first = context.workspace(run, GetWorkspaceContext(), {})
        self.assertEqual(len(first['data']['selection']['pages']), 20)
        self.assertTrue(first['truncated'])
        next_page = context.workspace(run, GetWorkspaceContext(cursor=first['next_cursor']), {})
        self.assertEqual(next_page['data']['selection']['pages'][0]['page_number'], 21)
        self.assertLess(len(json.dumps(first, ensure_ascii=False).encode()), 16384)
