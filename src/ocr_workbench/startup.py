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
from contextlib import ExitStack
from ocr_workbench.engine_packages import member_path
from ocr_workbench.atomic_files import write_json
from ocr_workbench.verification_cache import VerificationCache


FIRST_START_NOTICE = (
    "首次启动需要校验离线模型和运行文件，所需时间较长，请耐心等待。"
    "校验完成后将自动继续启动工作台，之后日常启动会更快。"
)
UPGRADE_NOTICE = (
    "检测到应用版本更新，需要重新完整校验，所需时间较长，请耐心等待。"
    "校验完成后将自动继续启动。"
)
REQUIRED_ENTRIES = (
    "runtimes/service/python.exe", "app/ocr_workbench/service.py",
    "web/index.html", "config/engines.json", "config/fusion-policy.json",
)


def startup_notice(reason):
    if reason in {"first_start", "installation_changed"}:
        return FIRST_START_NOTICE
    if reason == "version_changed":
        return UPGRADE_NOTICE
    if reason == "cached":
        return ""
    detail = {
        "forced": "已选择完整校验并启动",
        "invalid_receipt": "校验记录不可用",
        "unfinished_check": "上次完整校验未完成",
        "policy_changed": "校验规则已更新",
        "recognition_enabled": "首次启用识别模式",
        "cache_unavailable": "无法访问校验记录目录",
    }.get(reason, "需要重新检查离线文件")
    return detail + "，需要完整校验，所需时间较长，请耐心等待。校验完成后将自动继续启动。"


def run_startup_checks(bundle, data, registry=None, *, policy="auto", review_only=False,
                       cache_directory=None):
    """Select a full check or a constant-size preflight; payload checks stay explicit."""
    if policy not in {"auto", "full"}:
        raise ValueError("未知启动校验策略")
    bundle, data = Path(bundle).resolve(), Path(data).resolve()
    target = data / "launcher/startup-state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = {
        "status": "checking", "errors": [], "warnings": [], "checks": [],
        "bundle": str(bundle), "mode": "review_only" if review_only else "full",
        "verification": "pending", "reason": "detecting", "notice": "",
        "last_verified_at": None,
    }

    def publish(message):
        report["message"] = message
        report["seconds"] = time.monotonic() - started
        write_json(target, report)

    try:
        publish("正在确认应用版本与校验记录…")
        cache = VerificationCache(bundle, directory=cache_directory)
        level = "core" if review_only else "full"
        with ExitStack() as stack:
            try:
                stack.enter_context(cache.locked(lambda: publish("另一个工作台正在校验此安装包，请耐心等待…")))
            except OSError:
                cache = None
                report["warnings"].append("无法访问校验记录目录；本次执行完整校验，下次启动可能需要重新校验")
            identity = cache.identity() if cache else None
            record, reason = cache.lookup(identity, level) if cache else (None, "cache_unavailable")
            if policy == "full":
                reason = "forced"
            full = policy == "full" or record is None
            report.update(
                verification=("core" if review_only else "full") if full else "cached",
                reason=reason, notice=startup_notice(reason),
                last_verified_at=record["verified_at"] if record else None,
            )
            # Publish the persistent explanation before any GPU/import/hash work.
            publish("准备完整校验…" if full else "正在快速启动工作台…")
            if full:
                if cache:
                    cache.invalidate(identity)
                previous_warnings = report["warnings"]
                context = {key: report[key] for key in (
                    "verification", "reason", "notice", "last_verified_at",
                )}
                report.update(run_checks(bundle, data, review_only=review_only, context=context))
                report["warnings"] = previous_warnings + report["warnings"]
                if report["status"] == "passed" and cache:
                    # Identity changes are a failed check; only persistence errors are warnings.
                    if cache.identity() != identity:
                        raise ValueError("校验期间文件清单发生变化，请等待更新完成后重试")
                    try:
                        saved = cache.save(identity, level)
                        report["last_verified_at"] = saved["verified_at"]
                    except OSError:
                        report["warnings"].append("校验已通过，但记录保存失败；下次启动会重新完整校验")
            else:
                missing = [name for name in REQUIRED_ENTRIES if not (bundle / name).is_file()]
                if missing:
                    cache.invalidate(identity)
                    raise ValueError("应用入口缺失，请重新解压完整包：" + "、".join(missing))
                report["disk_free_bytes"] = shutil.disk_usage(data).free
                if report["disk_free_bytes"] < 2 * 1024**3:
                    raise ValueError("项目磁盘剩余空间不足 2 GB，请清理磁盘或更换项目目录")
                report["checks"].extend(["manifest-identity", "required-entries", "disk-space"])
                report["status"] = "passed"
            if report["status"] == "passed" and registry and not review_only:
                report["status"] = "checking"
                report["engine_packages"] = registry.check_active(
                    force=full, publish=publish, cache_directory=cache_directory,
                )
                for item in report["engine_packages"]:
                    report["warnings"].extend(item.get("warnings", []))
                report["status"] = "passed"
    except Exception as error:
        report["errors"].append("启动检查未完成：" + str(error))
        report["status"] = "failed"
    if report["status"] == "passed":
        message = "正在快速启动工作台…（复用已通过的校验记录）" if report["verification"] == "cached" else "本次完整校验通过，正在启动工作台…"
        if report["warnings"]:
            message += "\n" + "；".join(report["warnings"])
    else:
        message = "；".join(report["errors"][:3])
    publish(message)
    return report


def verify_integrity(root, progress=lambda done, total: None, *, core_only=False):
    root = Path(root).resolve()
    manifest = root / "manifest.json"
    if not manifest.is_file():
        return ["缺少文件校验清单 manifest.json，请重新解压完整包"]
    records = json.loads(manifest.read_text("utf-8"))["files"]

    def included(relative):
        parts = member_path(relative).parts
        return not core_only or not (
            parts[0].casefold() == "models"
            or (
                len(parts) > 1
                and parts[0].casefold() == "runtimes"
                and parts[1].casefold()
                in {"ppocr", "paddlevl", "glm", "hunyuan", "llama", "dewarp"}
            )
        )

    expected = {}
    seen = set()
    errors = []
    for item in records:
        relative = member_path(item["path"]).as_posix()
        if relative.casefold() in seen:
            raise ValueError("完整性清单有重复路径")
        seen.add(relative.casefold())
        if included(relative):
            expected[relative.casefold()] = item
    records = list(expected.values())
    actual = set()
    directories = [root]
    while directories:
        directory = directories.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            relative = path.relative_to(root)
            if not included(relative.as_posix()):
                continue
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


def run_checks(bundle, data, registry=None, integrity=True, review_only=False, *, context=None):
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
        "mode": "review_only" if review_only else "full",
        **(context or {}),
    }

    def publish(message):
        report["message"] = message
        write_json(target, report)

    try:
        publish("检查校对运行环境…" if review_only else "检查磁盘、驱动与 GPU…")
        report["disk_free_bytes"] = shutil.disk_usage(data).free
        if report["disk_free_bytes"] < 2 * 1024**3:
            report["errors"].append(
                "项目磁盘剩余空间不足 2 GB，请清理磁盘或更换项目目录"
            )
        if not review_only:
            check_gpu(report)
        else:
            report["warnings"].append(
                "仅校对与导出模式：识别与引擎操作已禁用；应用和基础运行时仍完整校验"
            )
        if integrity or review_only:
            publish(
                "正在逐文件校验应用和基础依赖…"
                if review_only
                else "正在逐文件校验应用、模型和依赖…"
            )
            report["errors"].extend(
                verify_integrity(
                    bundle,
                    lambda done, total: publish(f"正在校验离线文件：{done:,} / {total:,}"),
                    core_only=review_only,
                )
            )
            report["checks"].append(
                "core-file-sha256" if review_only else "full-file-sha256"
            )
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
            if review_only:
                imports = {"service": imports["service"]}
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
            if not review_only:
                check_native_engines(bundle, registry, report, publish)
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


def check_gpu(report):
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


def check_native_engines(bundle, registry, report, publish):
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


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    result = run_checks(args.bundle, args.data, review_only=args.review_only)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(result["status"] != "passed")
