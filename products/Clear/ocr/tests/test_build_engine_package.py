import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ocr_workbench.engine_packages import EnginePackages


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_engine_package.py"


class LocalGlmPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.glm = self.root / "my-glm"
        self.layout = self.root / "my-layout"
        self.output = self.root / "glm-local.zip"
        spec = {"glm": {"name": "GLM-OCR", "models": ["GLM-OCR", "PP-DocLayoutV3_safetensors"],
                        "capabilities": {"text": True, "tables": True}}}
        self.write(self.bundle / "config/engines.json", json.dumps(spec))
        self.write(self.bundle / "app/ocr_workbench/engine_host.py", "")
        self.write(self.bundle / "app/ocr_workbench/offline.py", "")
        for name in ("python.exe", "python312._pth", "python312.dll", "python312.zip"):
            self.write(self.bundle / "runtimes/glm" / name,
                       "python312.zip\n.\nLib/site-packages\n../../app\nimport site\n"
                       if name == "python312._pth" else "runtime")
        self.write(self.bundle / "locks/glm.json", "[]")
        self.write(self.bundle / "locks/models.json", '{"GLM-OCR":{"revision":"old"}}')
        self.write(self.glm / "config.json", "{}")
        self.write(self.glm / "preprocessor_config.json", "{}")
        self.write(self.glm / "tokenizer.json", "{}")
        self.write(self.glm / "model.safetensors", "glm weights")
        self.write(self.glm / "source-manifest.json", '{"repo":"old","revision":"stale","files":["private-path"]}')
        self.write(self.glm / ".huggingface/private.txt", "excluded")
        self.write(self.layout / "config.json", "{}")
        self.write(self.layout / "model.safetensors", "layout weights")

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def run_builder(self, *more):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--bundle", str(self.bundle), "--engine", "glm",
             "--id", "glm-local-v1", "--version", "local-1", "--output", str(self.output),
             "--glm-model-dir", str(self.glm), *more], capture_output=True, text=True,
        )

    def test_existing_local_models_make_importable_package(self):
        result = self.run_builder("--layout-model-dir", str(self.layout))
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.output) as archive:
            manifest = json.loads(archive.read("engine-package.json"))
            names = set(archive.namelist())
            self.assertEqual(names, {record["path"] for record in manifest["files"]} | {"engine-package.json"})
            self.assertNotIn("models/GLM-OCR/.huggingface/private.txt", names)
            self.assertNotIn(b"private-path", archive.read("models/GLM-OCR/source-manifest.json"))
            self.assertEqual(archive.read("models/GLM-OCR/model.safetensors"), b"glm weights")
            for record in manifest["files"]:
                data = archive.read(record["path"])
                self.assertEqual((len(data), hashlib.sha256(data).hexdigest()),
                                 (record["bytes"], record["sha256"]))
            locks = json.loads(archive.read("locks/models.json"))
            for name in ("GLM-OCR", "PP-DocLayoutV3_safetensors"):
                source = json.loads(archive.read(f"models/{name}/source-manifest.json"))
                self.assertEqual(locks[name], source)
                self.assertTrue(source["revision"].startswith("local-sha256-"))
        self.assertIn(hashlib.sha256(self.output.read_bytes()).hexdigest(),
                      self.output.with_suffix(".sha256").read_text("ascii"))
        staged = EnginePackages(self.bundle, self.root / "workspace").stage(self.output)
        self.assertEqual((staged["engine"], staged["id"]), ("glm", "glm-local-v1"))

    def test_layout_model_is_required(self):
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--layout-model-dir", result.stderr)
        self.assertFalse(self.output.exists())

    def test_local_glm_requires_tokenizer_and_processor(self):
        for name, expected in (("tokenizer.json", "tokenizer"),
                               ("preprocessor_config.json", "processor")):
            with self.subTest(name=name):
                path = self.glm / name
                content = path.read_bytes()
                path.unlink()
                try:
                    result = self.run_builder("--layout-model-dir", str(self.layout))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stderr)
                    self.assertFalse(self.output.exists())
                finally:
                    path.write_bytes(content)

    def test_output_cannot_be_inside_source_models(self):
        self.output = self.glm / "package.zip"
        before = {path.relative_to(self.glm): path.read_bytes()
                  for path in self.glm.rglob("*") if path.is_file()}
        result = self.run_builder("--layout-model-dir", str(self.layout))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the model directories", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(before, {path.relative_to(self.glm): path.read_bytes()
                                  for path in self.glm.rglob("*") if path.is_file()})

    def test_incomplete_runtime_is_not_published(self):
        (self.bundle / "runtimes/glm/python312.dll").unlink()
        result = self.run_builder("--layout-model-dir", str(self.layout))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Complete offline engine runtime", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".zip.part").exists())

    def test_existing_bundled_layout_can_be_reused(self):
        for name in ("config.json", "model.safetensors"):
            self.write(self.bundle / "models/PP-DocLayoutV3_safetensors" / name,
                       (self.layout / name).read_text("utf-8"))
        self.write(self.bundle / "models/PP-DocLayoutV3_safetensors/source-manifest.json",
                   '{"repo":"PaddlePaddle/PP-DocLayoutV3_safetensors","revision":"known"}')
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.output) as archive:
            locks = json.loads(archive.read("locks/models.json"))
            self.assertEqual(locks["PP-DocLayoutV3_safetensors"]["revision"], "known")

    def test_existing_engine_build_without_overrides(self):
        spec = {"ppocr": {"name": "PP-OCR", "models": ["PP-OCRv6_medium_rec"],
                          "capabilities": {"text": True}}}
        self.write(self.bundle / "config/engines.json", json.dumps(spec))
        for name in ("python.exe", "python312._pth", "python312.dll", "python312.zip"):
            self.write(self.bundle / "runtimes/ppocr" / name, "runtime")
        self.write(self.bundle / "locks/ppocr.json", "[]")
        self.write(self.bundle / "models/PP-OCRv6_medium_rec/source-manifest.json",
                   '{"repo":"PaddlePaddle/PP-OCRv6_medium_rec","revision":"known"}')
        self.write(self.bundle / "models/PP-OCRv6_medium_rec/model.pdiparams", "weights")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--bundle", str(self.bundle), "--engine", "ppocr",
             "--id", "ppocr-test-v1", "--version", "test", "--output", str(self.output)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read("models/PP-OCRv6_medium_rec/model.pdiparams"), b"weights")


if __name__ == "__main__":
    unittest.main()
