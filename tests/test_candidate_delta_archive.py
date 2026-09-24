"""Small synthetic tests; never read the real candidate-07 archive."""

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "agent_eval"))
import candidate_delta_archive as delta


def manifest(files):
    return json.dumps({"kind": "synthetic", "files": [
        {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in files.items()]}, ensure_ascii=False).encode("utf-8")


class DeltaArchiveTests(unittest.TestCase):
    def fixture(self, root):
        base = root / "base.zip"
        base_files = {"kept.txt": b"unchanged", "edit.txt": b"old", "gone.txt": b"delete"}
        with zipfile.ZipFile(base, "x") as archive:
            archive.writestr(delta.MANIFEST, manifest(base_files))
            for name, data in base_files.items():
                archive.writestr(delta.PREFIX + name, data)
        candidate = root / "candidate"
        candidate.mkdir()
        target = {"kept.txt": b"unchanged", "edit.txt": b"new", "子目录/new.txt": b"hello"}
        for name, data in target.items():
            path = candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (candidate / "manifest.json").write_bytes(manifest(target))
        return base, candidate, delta.sha256_file(base)

    def test_build_and_verify_two_piece_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, candidate, base_sha = self.fixture(root)
            layer = root / "layer.zip"
            receipt = delta.build(base, candidate, layer, root / "build.json", base_sha)
            self.assertEqual((receipt["changed_files"], receipt["deleted_files"]), (2, 1))
            with zipfile.ZipFile(layer) as archive:
                self.assertEqual(set(archive.namelist()), {delta.MANIFEST, delta.METADATA,
                                  delta.PREFIX + "edit.txt", delta.PREFIX + "子目录/new.txt"})
                self.assertEqual(json.loads(archive.read(delta.METADATA))["deleted"], ["gone.txt"])
            verified = delta.verify_layer(base, layer, base_sha)
            self.assertEqual((verified["combined_files"], verified["inherited_files"]), (3, 1))
            with self.assertRaisesRegex(ValueError, "Base ZIP SHA-256 mismatch"):
                delta.verify_layer(base, layer, "0" * 64)

    def test_generic_candidate_cli_and_legacy_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, candidate, base_sha = self.fixture(root)
            for option in ("--candidate", "--candidate08"):
                layer = root / (option[2:] + ".zip")
                receipt = root / (option[2:] + ".json")
                args = ["candidate_delta_archive.py", "build", "--base-archive", str(base),
                        "--expected-base-sha256", base_sha, option, str(candidate),
                        "--layer-archive", str(layer), "--receipt", str(receipt)]
                with mock.patch.object(sys, "argv", args), redirect_stdout(io.StringIO()):
                    delta.main()
                self.assertEqual(delta.verify_layer(base, layer, base_sha)["status"], "pass")

    def test_refuses_path_traversal_duplicates_and_symlink(self):
        with self.assertRaisesRegex(ValueError, "Unsafe path"):
            delta.parse_manifest(manifest({"../escape": b"x"}))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            delta.parse_manifest(manifest({"Case.txt": b"x", "case.TXT": b"y"}))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, candidate, base_sha = self.fixture(root)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(base, "a") as archive:
                    archive.writestr(delta.PREFIX + "kept.txt", b"duplicate")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                delta.inspect_base(base, delta.sha256_file(base))
            link = candidate / "link.txt"
            try:
                link.symlink_to(candidate / "kept.txt")
            except (OSError, NotImplementedError):
                pass  # Windows systems without Developer Mode cannot create symlinks.
            else:
                with self.assertRaisesRegex(ValueError, "Non-ordinary|Reparse"):
                    delta.inspect_candidate(candidate)

    def test_refuses_zip_symlink_and_bad_crc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, _, _ = self.fixture(root)
            link = zipfile.ZipInfo(delta.PREFIX + "link.txt")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(base, "a") as archive:
                archive.writestr(link, b"kept.txt")
            with self.assertRaisesRegex(ValueError, "Non-ordinary ZIP entry"):
                delta.inspect_base(base, delta.sha256_file(base))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, _, _ = self.fixture(root)
            raw = base.read_bytes().replace(b"unchanged", b"unchaXged", 1)
            base.write_bytes(raw)
            with self.assertRaises(zipfile.BadZipFile):
                delta.inspect_base(base, delta.sha256_file(base))

    def test_rejects_layer_tampering_and_self_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, candidate, base_sha = self.fixture(root)
            layer = root / "layer.zip"
            delta.build(base, candidate, layer, root / "build.json", base_sha)
            with zipfile.ZipFile(layer, "a") as archive:
                archive.writestr("../escape", b"x")
            with self.assertRaisesRegex(ValueError, "Unsafe path"):
                delta.verify_layer(base, layer, base_sha)
        with self.assertRaisesRegex(ValueError, "Invalid manifest record"):
            delta.parse_manifest(manifest({"manifest.json": b"self"}))

    def test_failure_cleans_partial_and_published_layer(self):
        for failure in ("receipt_write", "receipt_publish"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                base, candidate, base_sha = self.fixture(root)
                layer, receipt = root / "layer.zip", root / "receipt.json"
                if failure == "receipt_write":
                    context = mock.patch.object(delta.json, "dump", side_effect=OSError("receipt write failed"))
                else:
                    original = delta.os.link
                    calls = 0

                    def fail_second(source, destination):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise OSError("receipt publish failed")
                        return original(source, destination)

                    context = mock.patch.object(delta.os, "link", side_effect=fail_second)
                with context, self.assertRaises(OSError):
                    delta.build(base, candidate, layer, receipt, base_sha)
                for path in (layer, receipt, root / "layer.zip.partial", root / "receipt.json.partial"):
                    self.assertFalse(path.exists(), str(path))
                # A failed attempt must not prevent a clean retry.
                self.assertEqual(delta.build(base, candidate, layer, receipt, base_sha)["status"], "pass")

    def test_verify_rejects_mutation_after_member_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, candidate, base_sha = self.fixture(root)
            layer = root / "layer.zip"
            delta.build(base, candidate, layer, root / "build.json", base_sha)
            original = delta.verify_zip

            def mutate_after_members(archive, expected_names, records, control, label):
                result = original(archive, expected_names, records, control, label)
                if label == "layer":
                    with layer.open("ab") as stream:
                        stream.write(b"mutation after CRC/SHA checks")
                return result

            with mock.patch.object(delta, "verify_zip", side_effect=mutate_after_members):
                with self.assertRaisesRegex(ValueError, "changed during member verification"):
                    delta.verify_layer(base, layer, base_sha)

    def test_space_preflight_rejects_before_creating_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, candidate, base_sha = self.fixture(root)
            with mock.patch.object(delta.shutil, "disk_usage", return_value=mock.Mock(free=0)):
                with self.assertRaisesRegex(OSError, "Insufficient space"):
                    delta.build(base, candidate, root / "layer.zip", root / "receipt.json", base_sha)
            self.assertFalse((root / "layer.zip.partial").exists())


if __name__ == "__main__":
    unittest.main()
