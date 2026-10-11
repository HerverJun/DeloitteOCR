"""Counterexamples for the browser snapshot's explicitly bounded representation gap."""
import json
import unittest
import zlib
from actual_review_ui_readback import decode_revision, original_http_matches


class SnapshotRepresentation(unittest.TestCase):
    def event(self, value):
        return {'screen': {'sources': [{'prepared_metadata': {'engine_load_events': [{'ordinal': value}]}}]}}

    def test_only_same_double_load_ordinal_is_accepted(self):
        original = 1791695431614240200
        browser = 1791695431614240300
        rounded = []
        self.assertTrue(original_http_matches(self.event(browser), self.event(original), rounded=rounded))
        self.assertEqual(rounded[0]['committed_integer'], str(original))
        self.assertFalse(original_http_matches(self.event(browser + 4096), self.event(original)))
        self.assertFalse(original_http_matches({'result_id': browser}, {'result_id': original}))
        self.assertFalse(original_http_matches(self.event(41), self.event(40)))

    def test_geometry_and_text_drift_are_rejected(self):
        self.assertTrue(original_http_matches({'box': [173, 312]}, {'box': [173.0, 312.0]}))
        self.assertFalse(original_http_matches({'box': [174, 312]}, {'box': [173.0, 312.0]}))
        self.assertFalse(original_http_matches({'text': '0001'}, {'text': '1'}))
        self.assertFalse(original_http_matches({'text': '+20'}, {'text': '-20'}))

    def test_product_revision_magic_is_required(self):
        literal = {'screen': {'business_metadata': [{'box': [173.0, 312.0]}]}}
        encoded = json.dumps(literal).encode()
        self.assertEqual(decode_revision(b'OCRZ1\0' + zlib.compress(encoded)), literal)
        self.assertEqual(decode_revision(encoded.decode()), literal)
        with self.assertRaisesRegex(ValueError, 'Unknown immutable revision encoding'):
            decode_revision(zlib.compress(encoded))


if __name__ == '__main__':
    unittest.main()
