"""Versioned complete engine bundles; staging never executes uploaded code."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import threading
import zipfile
from ocr_workbench.store import uid
from ocr_workbench.atomic_files import read_json, write_json

ID = re.compile(r"[a-z][a-z0-9_-]{1,63}")
RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *[f"COM{i}" for i in range(1, 10)],
    *[f"LPT{i}" for i in range(1, 10)],
}


def member_path(name):
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or path.as_posix() != name
        or any(
            p in {".", ".."}
            or ":" in p
            or p.endswith((" ", "."))
            or p.split(".")[0].upper() in RESERVED
            for p in path.parts
        )
    ):
        raise ValueError("引擎包包含不安全的 Windows 路径")
    return path


def read_manifest(root):
    return read_json(Path(root) / "engine-package.json")


class EnginePackages:
    def __init__(self, bundle, data):
        self.bundle = Path(bundle).resolve()
        self.root = Path(data).resolve() / "engine-packages"
        self.root.mkdir(parents=True, exist_ok=True)
        self.packages = self.root / "versions"
        self.packages.mkdir(exist_ok=True)
        self.staging = self.root / "staging"
        self.staging.mkdir(exist_ok=True)
        self.guard = threading.RLock()
        self.state_guard = threading.RLock()
        self.builtin = json.loads(
            (self.bundle / "config/engines.json").read_text("utf-8")
        )
        self.active_file = self.root / "active.json"

    def active(self):
        with self.state_guard:
            return read_json(self.active_file) if self.active_file.exists() else {}

    def resolve(self, engine, package_id="builtin"):
        if package_id == "builtin":
            if engine not in self.builtin and engine != "dewarp":
                raise ValueError("未知内置引擎")
            return self.bundle
        if not isinstance(package_id, str) or not ID.fullmatch(package_id):
            raise ValueError("无效引擎版本")
        root = self.packages / package_id
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            raise ValueError("引擎版本不存在")
        if read_manifest(root)["engine"] != engine:
            raise ValueError("引擎包不属于所选引擎")
        return root

    def engines(self, active=None):
        values = {
            key: {**value, "package_id": "builtin"}
            for key, value in self.builtin.items()
        }
        for engine, package_id in (self.active() if active is None else active).items():
            if package_id == "builtin":
                continue
            root = self.resolve(engine, package_id)
            spec = json.loads((root / "config/engines.json").read_text("utf-8"))[engine]
            values[engine] = {**spec, "package_id": package_id}
        return values

    def inventory(self):
        active = self.active()
        installed = []
        for root in self.packages.iterdir():
            if root.is_dir() and ID.fullmatch(root.name):
                manifest = read_manifest(root)
                installed.append(
                    {
                        key: manifest[key]
                        for key in ["id", "engine", "name", "version", "protocol"]
                    }
                )
        staged = []
        for root in self.staging.iterdir():
            if (
                not re.fullmatch("[a-f0-9]{32}", root.name)
                or root.is_symlink()
                or root.is_junction()
            ):
                continue
            receipt = root / "stage-receipt.json"
            if receipt.is_file():
                staged.append({**read_json(receipt), "ready": True})
            else:
                staged.append({"staging_id": root.name, "ready": False})
        return {
            "engines": self.engines(active),
            "installed": installed,
            "active": active,
            "staged": staged,
        }

    @contextmanager
    def mutation(self):
        # Covers concurrent imports/activation in the process; Windows locks cover services.
        import msvcrt

        with self.guard, (self.root / "mutation.lock").open("a+b") as lock:
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("另一个工作台正在更新引擎，请稍后再试")
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def stage(self, archive_path):
        with self.mutation():
            folder = self.staging / uid()
            folder.mkdir()
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    entries = archive.infolist()
                    if len(entries) > 200000:
                        raise ValueError("引擎包文件数量超限")
                    names = {}
                    total = 0
                    for entry in entries:
                        path = member_path(entry.filename)
                        if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
                            raise ValueError(
                                "引擎包只允许普通文件，不允许链接或目录条目"
                            )
                        folded = entry.filename.casefold()
                        if folded in names:
                            raise ValueError("引擎包有重复或大小写冲突路径")
                        names[folded] = entry
                        total += entry.file_size
                        if entry.file_size > 32 * 1024**3 or total > 128 * 1024**3:
                            raise ValueError("引擎包解压体积超限")
                    if (
                        "engine-package.json" not in names
                        or names["engine-package.json"].file_size > 32 * 1024**2
                    ):
                        raise ValueError("缺少有效的引擎包清单")
                    manifest = json.loads(archive.read("engine-package.json"))
                    if (
                        manifest.get("schema_version") != 1
                        or manifest.get("protocol") != "file-ipc-v1"
                    ):
                        raise ValueError("不支持此引擎包协议版本")
                    for key in ["id", "engine"]:
                        if not isinstance(manifest.get(key), str) or not ID.fullmatch(
                            manifest[key]
                        ):
                            raise ValueError("无效的引擎包标识")
                    for key in ["name", "version"]:
                        if (
                            not isinstance(manifest.get(key), str)
                            or not 1 <= len(manifest[key]) <= 120
                        ):
                            raise ValueError("引擎包名称或版本无效")
                    records = manifest.get("files")
                    if not isinstance(records, list):
                        raise ValueError("缺少文件哈希清单")
                    expected = {r["path"]: r for r in records}
                    if len(expected) != len(records) or {
                        n.filename for n in entries
                    } != {*expected, "engine-package.json"}:
                        raise ValueError("引擎包内容与清单不一致")
                    if total + 1024**3 > shutil.disk_usage(self.root).free:
                        raise ValueError("磁盘空间不足，无法暂存完整引擎包")
                    for relative, record in expected.items():
                        path = member_path(relative)
                        entry = archive.getinfo(relative)
                        if (
                            type(record.get("bytes")) is not int
                            or record["bytes"] != entry.file_size
                            or not re.fullmatch(
                                "[a-f0-9]{64}", str(record.get("sha256", ""))
                            )
                        ):
                            raise ValueError("文件大小或哈希清单无效")
                        target = folder.joinpath(*path.parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        digest = hashlib.sha256()
                        size = 0
                        with archive.open(entry) as source, target.open("wb") as output:
                            while chunk := source.read(4 * 1024**2):
                                output.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                        if (
                            digest.hexdigest() != record["sha256"]
                            or size != record["bytes"]
                        ):
                            raise ValueError("引擎包文件损坏：" + relative)
                    (folder / "engine-package.json").write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8"
                    )
                    self.validate_layout(folder, manifest)
                with Path(archive_path).open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                receipt = {
                    "staging_id": folder.name,
                    "sha256": digest,
                    "manifest_sha256": hashlib.sha256(
                        (folder / "engine-package.json").read_bytes()
                    ).hexdigest(),
                    "id": manifest["id"],
                    "engine": manifest["engine"],
                    "name": manifest["name"],
                    "version": manifest["version"],
                    "bytes": total,
                    "files": len(records),
                }
                write_json(folder / "stage-receipt.json", receipt, durable=True)
                return receipt
            except BaseException:
                shutil.rmtree(folder)
                raise

    def validate_layout(self, root, manifest):
        engine = manifest["engine"]
        required = [
            f"runtimes/{engine}/python.exe",
            f"runtimes/{engine}/python312._pth",
            "app/ocr_workbench/engine_host.py",
            "app/ocr_workbench/offline.py",
            "config/engines.json",
            f"locks/{engine}.json",
            f"runtimes/{engine}/python312.dll",
            f"runtimes/{engine}/python312.zip",
        ]
        if any(not (root / path).is_file() for path in required):
            raise ValueError("引擎包缺少独立运行时、适配器、能力声明或依赖锁")
        pth = (root / required[1]).read_text("utf-8")
        if any(
            line.strip()
            and not line.startswith("#")
            and line.strip()
            not in {
                "python312.zip",
                ".",
                "Lib/site-packages",
                "../../app",
                "import site",
            }
            for line in pth.splitlines()
        ):
            raise ValueError("运行时路径必须可搬迁且位于完整引擎包内")
        spec = json.loads((root / "config/engines.json").read_text("utf-8"))
        if (
            set(spec) != {engine}
            or not isinstance(spec[engine].get("capabilities"), dict)
            or not spec[engine].get("models")
        ):
            raise ValueError("引擎能力或模型清单无效")
        for name in spec[engine]["models"]:
            member_path("models/" + name + "/source-manifest.json")
            if "/" in name or "\\" in name:
                raise ValueError("无效模型目录")
            provenance = root / "models" / name / "source-manifest.json"
            if not provenance.is_file() or not json.loads(
                provenance.read_text("utf-8")
            ).get("revision"):
                raise ValueError("缺少模型 revision 记录")
            if not any(
                p.suffix.lower()
                in {
                    ".safetensors",
                    ".gguf",
                    ".pdiparams",
                    ".bin",
                    ".pt",
                    ".pth",
                    ".onnx",
                }
                for p in provenance.parent.rglob("*")
                if p.is_file()
            ):
                raise ValueError("模型目录缺少权重文件")

    def activate(self, staging_id, expected_sha256):
        if not re.fullmatch("[a-f0-9]{32}", str(staging_id)):
            raise ValueError("无效暂存标识")
        with self.mutation():
            staged = self.staging / staging_id
            receipt = read_json(staged / "stage-receipt.json")
            if receipt["sha256"] != expected_sha256:
                raise ValueError("暂存包校验标识不一致")
            if (
                hashlib.sha256(
                    (staged / "engine-package.json").read_bytes()
                ).hexdigest()
                != receipt["manifest_sha256"]
            ):
                raise ValueError("暂存清单已改变，请重新导入")
            manifest = read_manifest(staged)
            target = self.packages / manifest["id"]
            if target.exists():
                raise ValueError("此版本已安装，请从版本列表启用或回退")
            # Reverify staged content before executing code or publishing activation.
            self.verify_files(staged, manifest)
            self.probe(staged, manifest["engine"])
            self.smoke(staged, manifest["engine"])
            staged.replace(target)
            active = self.active()
            previous = active.get(manifest["engine"], "builtin")
            active[manifest["engine"]] = manifest["id"]
            self.save_active(active)
            return {
                "activated": True,
                "engine": manifest["engine"],
                "package_id": manifest["id"],
                "previous": previous,
            }

    def verify_files(self, root, manifest):
        if root.is_symlink() or root.is_junction():
            raise ValueError("引擎目录包含链接或重解析路径")
        actual = set()
        for path in root.rglob("*"):
            if path.is_symlink() or path.is_junction():
                raise ValueError("引擎目录包含链接或重解析路径")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != {r["path"] for r in manifest["files"]} | {
            "engine-package.json",
            "stage-receipt.json",
        }:
            raise ValueError("引擎文件清单发生变化")
        for record in manifest["files"]:
            path = root.joinpath(*member_path(record["path"]).parts)
            if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError("引擎文件缺失或路径已改变")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != record["sha256"] or path.stat().st_size != record["bytes"]:
                raise ValueError("暂存或已安装引擎文件发生变化")

    def probe(self, root, engine):
        runtime = root / "runtimes" / engine / "python.exe"
        environment = os.environ.copy()
        environment["PATH"] = (
            str(runtime.parent)
            + os.pathsep
            + str(Path(os.environ["SystemRoot"]) / "System32")
        )
        for key in [
            "PYTHONPATH",
            "PYTHONHOME",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        ]:
            environment.pop(key, None)
        output = self.root / "checks" / uid()
        output.mkdir(parents=True)
        code = "import importlib,importlib.metadata as m,json,sys;from pathlib import Path;from ocr_workbench.offline import configure,install_guard;configure(Path(sys.argv[1]));install_guard(Path(sys.argv[1])/'blocked.log');import ocr_workbench.engine_host;lock=json.loads(Path(sys.argv[2]).read_text('utf-8'));assert all(m.version(d['name'])==d['version'] for d in lock);print('dependency-lock-ok')"
        result = subprocess.run(
            [
                str(runtime),
                "-B",
                "-X",
                "utf8",
                "-I",
                "-c",
                code,
                str(output),
                str(root / "locks" / f"{engine}.json"),
            ],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (output / "probe.json").write_text(
            json.dumps(
                {
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            ),
            "utf-8",
        )
        if result.returncode:
            raise ValueError("引擎包依赖自检失败，现用版本未变；请查看引擎检查日志")

    def save_active(self, active):
        with self.state_guard:
            write_json(self.active_file, active, durable=True)

    def smoke(self, root, engine):
        from ocr_workbench.adapter import EngineAdapter

        output = self.root / "checks" / uid()
        output.mkdir(parents=True)
        adapter = EngineAdapter(root, engine, output / "sessions")
        try:
            adapter.load()
            result = adapter.recognize(self.bundle / "fixtures/printed.png", output)
            if result.get("status") != "success" or not result.get("text", "").strip():
                raise ValueError("引擎实际识别自检失败，现用版本未变")
        finally:
            adapter.unload()

    def switch(self, engine, package_id):
        with self.mutation():
            root = self.resolve(engine, package_id)
            if package_id != "builtin":
                manifest = read_manifest(root)
                self.verify_files(root, manifest)
                self.probe(root, engine)
                self.smoke(root, engine)
            active = self.active()
            previous = active.get(engine, "builtin")
            active[engine] = package_id
            self.save_active(active)
            return {
                "activated": True,
                "engine": engine,
                "package_id": package_id,
                "previous": previous,
            }

    def discard(self, key):
        if not re.fullmatch("[a-f0-9]{32}", str(key)):
            raise ValueError("无效暂存标识")
        with self.mutation():
            folder = self.staging / key
            if folder.exists():
                if folder.is_symlink() or folder.is_junction():
                    raise ValueError("无效暂存目录")
                shutil.rmtree(folder)
        return {"discarded": True}
