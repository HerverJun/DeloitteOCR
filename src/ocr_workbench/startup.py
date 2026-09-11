"""Offline startup gates: full hashes, native dependency loads, GPU and disk."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from ocr_workbench.engine_packages import member_path
from ocr_workbench.atomic_files import write_json


def verify_integrity(root, progress=lambda done, total: None):
    root = Path(root).resolve()
    manifest = root / "manifest.json"
    if not manifest.is_file():
        return ["缺少文件校验清单 manifest.json，请重新解压完整包"]
    records = json.loads(manifest.read_text("utf-8"))["files"]
    expected = {}
    errors = []
    for item in records:
        relative = member_path(item["path"]).as_posix()
        if relative.casefold() in expected:
            raise ValueError("完整性清单有重复路径")
        expected[relative.casefold()] = item
    actual = set()
    directories = [root]
    while directories:
        directory = directories.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or relative.parts[0] in {
                "cache",
                "runs",
                "results",
            }:
                continue
            info = entry.stat(follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & 0x400 or stat.S_ISLNK(
                info.st_mode
            ):
                errors.append("应用目录包含链接或重解析路径：" + relative.as_posix())
                continue
            if entry.is_dir(follow_symlinks=False):
                directories.append(path)
            elif relative.as_posix() != "manifest.json":
                actual.add(relative.as_posix().casefold())
    if actual != set(expected):
        errors.extend(
            "缺少文件：" + expected[name]["path"] for name in set(expected) - actual
        )
        errors.extend("出现清单外文件：" + name for name in actual - set(expected))

    def verify(item):
        path = root.joinpath(*member_path(item["path"]).parts)
        try:
            if path.stat().st_size != item["bytes"]:
                return "文件大小不符：" + item["path"]
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != item["sha256"]:
                return "文件内容损坏：" + item["path"]
        except OSError:
            return "无法读取文件：" + item["path"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(verify, item)
            for item in records
            if item["path"].casefold() in actual
        ]
        for done, future in enumerate(as_completed(futures), 1):
            error = future.result()
            if error:
                errors.append(error)
            if done % 250 == 0 or done == len(futures):
                progress(done, len(records))
    return errors


def run_checks(bundle, data, registry=None, integrity=True):
    bundle = Path(bundle).resolve()
    data = Path(data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    session = data / "launcher"
    session.mkdir(exist_ok=True)
    target = session / "startup-state.json"
    started = time.monotonic()
    report = {
        "status": "checking",
        "errors": [],
        "warnings": [],
        "checks": [],
        "bundle": str(bundle),
    }

    def publish(message):
        report["message"] = message
        write_json(target, report)

    try:
        publish("检查磁盘、驱动与 GPU…")
        report["disk_free_bytes"] = shutil.disk_usage(data).free
        if report["disk_free_bytes"] < 2 * 1024**3:
            report["errors"].append(
                "项目磁盘剩余空间不足 2 GB，请清理磁盘或更换项目目录"
            )
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=uuid,name,memory.total,memory.free,driver_version",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except FileNotFoundError:
            raise ValueError(
                "未找到 NVIDIA 驱动检查工具 nvidia-smi，请安装兼容 NVIDIA 驱动后重试"
            )
        if result.returncode:
            report["errors"].append("未发现可用 NVIDIA 驱动，请安装兼容驱动后重试")
        else:
            columns = [v.strip() for v in result.stdout.splitlines()[0].split(",")]
            report["gpu"] = {
                "uuid": columns[0],
                "name": columns[1],
                "total_mib": int(columns[2]),
                "free_mib": int(columns[3]),
                "driver": columns[4],
            }
            if int(columns[2]) < 14 * 1024:
                report["errors"].append(
                    "GPU 总显存不足以运行本包全部固定精度引擎；本包目标为 16 GB 显存"
                )
            if int(columns[3]) < 2 * 1024:
                report["errors"].append("当前可用显存不足 2 GB，请关闭占用 GPU 的程序")
            elif int(columns[3]) < 12 * 1024:
                report["warnings"].append(
                    "当前空闲显存低于 12 GB，结构化模型可能因显存不足失败；不会自动降低质量"
                )
            report["checks"].append("driver-and-gpu")
        if integrity:
            publish("正在逐文件校验应用、模型和依赖…")
            report["errors"].extend(
                verify_integrity(
                    bundle,
                    lambda done, total: publish(f"正在校验离线文件 {done}/{total}…"),
                )
            )
            report["checks"].append("full-file-sha256")
        if not report["errors"]:
            imports = {
                "service": [
                    "fastapi",
                    "uvicorn",
                    "sqlite3",
                    "PIL.Image",
                    "pillow_heif",
                    "openpyxl",
                    "cv2",
                    "numpy",
                    "psutil",
                ],
                "ppocr": ["paddle", "paddleocr", "paddlex"],
                "paddlevl": ["paddle", "paddleocr", "paddlex"],
                "glm": ["torch", "transformers", "glmocr", "torchvision"],
                "hunyuan": ["PIL.Image", "openpyxl"],
            }
            dependency = []
            for engine, modules in imports.items():
                publish("检查运行时依赖：" + engine + "…")
                runtime = bundle / "runtimes" / engine / "python.exe"
                output = session / "dependency-probes" / engine
                output.mkdir(parents=True, exist_ok=True)
                env = os.environ.copy()
                env["PATH"] = (
                    str(runtime.parent)
                    + os.pathsep
                    + str(Path(os.environ["SystemRoot"]) / "System32")
                )
                for key in [
                    "PYTHONHOME",
                    "PYTHONPATH",
                    "CUDA_HOME",
                    "CUDA_PATH",
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                ]:
                    env.pop(key, None)
                code = "import importlib,json,sys;from pathlib import Path;from ocr_workbench.offline import configure,install_guard;configure(Path(sys.argv[1]));install_guard(Path(sys.argv[1])/'blocked.log');[importlib.import_module(name) for name in json.loads(sys.argv[2])];print('imports-ok')"
                if engine in {"ppocr", "paddlevl"}:
                    code += ";import paddle;assert paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count()>0"
                if engine == "glm":
                    code += ";import torch;assert torch.cuda.is_available()"
                probe = subprocess.run(
                    [
                        str(runtime),
                        "-B",
                        "-X",
                        "utf8",
                        "-I",
                        "-c",
                        code,
                        str(output),
                        json.dumps(modules),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="backslashreplace",
                    timeout=120,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                dependency.append(
                    {
                        "runtime": engine,
                        "exit_code": probe.returncode,
                        "stdout": probe.stdout,
                        "stderr": probe.stderr,
                    }
                )
                if probe.returncode:
                    report["errors"].append(
                        engine
                        + " 依赖或 CUDA 加载失败，请重新解压完整包并检查驱动；详情已保存于启动检查记录"
                    )
            report["dependencies"] = dependency
            report["checks"].append("native-runtime-imports")
            native = subprocess.run(
                [str(bundle / "runtimes/llama/llama-server.exe"), "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            report["llama"] = {
                "exit_code": native.returncode,
                "output": native.stdout + native.stderr,
            }
            if native.returncode:
                report["errors"].append("llama.cpp 原生运行库无法加载")
            if registry:
                for engine, package_id in registry.active().items():
                    if package_id == "builtin":
                        continue
                    publish("校验已启用的引擎包：" + engine)
                    root = registry.resolve(engine, package_id)
                    from ocr_workbench.engine_packages import read_manifest

                    registry.verify_files(root, read_manifest(root))
                    registry.probe(root, engine)
        report["status"] = "failed" if report["errors"] else "passed"
    except Exception as error:
        report["errors"].append("启动检查未完成：" + str(error))
        report["status"] = "failed"
    report["seconds"] = time.monotonic() - started
    publish(
        "启动检查通过"
        if report["status"] == "passed"
        else "；".join(report["errors"][:3])
    )
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    result = run_checks(args.bundle, args.data)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(result["status"] != "passed")
