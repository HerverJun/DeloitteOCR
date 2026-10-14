import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from ocr_workbench.atomic_files import read_json, write_json
from ocr_workbench import atomic_files


class AtomicFileTests(unittest.TestCase):
    def test_transient_read_and_replace_sharing_violations(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "state.json"
            write_json(target, {"value": 1}, durable=True)
            original = atomic_files._read_text
            attempts = []

            def read(path, *args, **kwargs):
                attempts.append(path)
                if len(attempts) < 4:
                    raise PermissionError("reader overlaps Windows rename")
                return original(path, *args, **kwargs)

            with patch.object(atomic_files, "_read_text", read):
                self.assertEqual(read_json(target), {"value": 1})
            self.assertEqual(len(attempts), 4)

    def test_concurrent_readers_never_observe_partial_json(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "state.json"
            write_json(target, {"value": 0})
            done = threading.Event()
            errors = []

            def reader():
                while not done.is_set():
                    try:
                        value = read_json(target)
                        if not isinstance(value["value"], int):
                            errors.append(value)
                    except Exception as error:
                        errors.append(str(error))

            threads = [threading.Thread(target=reader) for _ in range(3)]
            for thread in threads:
                thread.start()
            try:
                for i in range(40):
                    write_json(target, {"value": i, "body": "x" * 10000})
            finally:
                done.set()
                for thread in threads:
                    thread.join(5)
            self.assertEqual(errors, [])
            self.assertEqual(read_json(target)["value"], 39)
            self.assertEqual(list(Path(temp).glob("*.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
