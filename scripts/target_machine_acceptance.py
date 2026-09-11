"""Explicit external-machine gates plus real packaged application/performance audits."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import winreg


def machine_identity():
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
    ) as key:
        value = winreg.QueryValueEx(key, "MachineGuid")[0]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def environment():
    command = "$cpu=Get-CimInstance Win32_Processor;$pc=Get-CimInstance Win32_ComputerSystem;@{cpu=($cpu.Name -join ', ');ram_bytes=$pc.TotalPhysicalMemory}|ConvertTo-Json -Compress"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("无法读取 Windows 硬件信息")
    return {
        "machine_sha256": machine_identity(),
        "windows": platform.platform(),
        **json.loads(result.stdout),
        "development_commands": {
            name: shutil.which(name)
            for name in [
                "python",
                "python3",
                "py",
                "node",
                "npm",
                "nvcc",
                "docker",
                "git",
            ]
        },
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--role", choices=["clean-windows", "a4000"], required=True)
    p.add_argument(
        "--confirm-no-development-environment",
        action="store_true",
        help="Operator attests this is a separate machine without development dependencies",
    )
    p.add_argument(
        "--os-isolation-evidence",
        type=Path,
        required=True,
        help="Completed offline_guard --application output directory on this same machine",
    )
    a = p.parse_args()
    if a.output.exists() and any(a.output.iterdir()):
        raise ValueError("请使用新的验收输出目录")
    a.output.mkdir(parents=True, exist_ok=True)
    report = {
        "passed": False,
        "role": a.role,
        "errors": [],
        "checks": [],
        "bundle": str(a.bundle.resolve()),
    }
    try:
        report["environment"] = environment()
        baseline = json.loads(
            (a.bundle / "config/development-machine.json").read_text("utf-8")
        )
        if baseline["machine_sha256"] == report["environment"]["machine_sha256"]:
            report["errors"].append("当前仍是开发机，不能作为另一台电脑的证据")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from audit_load_cycles import gpu

        report["gpu"] = gpu()
        if a.role == "a4000" and "RTX A4000" not in report["gpu"]["name"]:
            report["errors"].append("未检测到要求的 RTX A4000")
        if a.role == "a4000" and (
            "W5-2455X" not in report["environment"]["cpu"].upper()
            or report["environment"]["ram_bytes"] < 60 * 1024**3
        ):
            report["errors"].append("目标 CPU/RAM 与 Xeon W5-2455X、64 GB 配置不符")
        if a.role == "clean-windows":
            if not a.confirm_no_development_environment:
                report["errors"].append("缺少操作者关于无开发环境的明确确认")
            commands = report["environment"]["development_commands"]
            found = {
                k: v
                for k, v in commands.items()
                if v
                and "WindowsApps" not in v
                and not Path(v).is_relative_to(a.bundle.resolve())
            }
            if found:
                report["errors"].append(
                    "发现开发命令路径，不能标记为干净机器：" + ", ".join(found)
                )
        # The isolation receipt is bound to both machine and exact application manifest.
        isolation = json.loads(
            (a.os_isolation_evidence / "os-isolation-result.json").read_text("utf-8")
        )
        current_manifest = hashlib.sha256(
            (a.bundle / "manifest.json").read_bytes()
        ).hexdigest()
        report["bundle_manifest_sha256"] = current_manifest
        if (
            not isolation.get("passed")
            or isolation.get("machine_sha256")
            != report["environment"]["machine_sha256"]
            or isolation.get("bundle_manifest_sha256") != current_manifest
        ):
            report["errors"].append(
                "系统禁网证据不通过，或不属于当前机器和应用文件版本"
            )
        report["os_isolation"] = isolation
        if not report["errors"]:
            runtime = a.bundle / "runtimes/service/python.exe"
            scripts = ["audit_application.py"]
            if a.role == "a4000":
                scripts += ["audit_load_cycles.py", "audit_batch_stress.py"]
            for name in scripts:
                target = a.output / name.removesuffix(".py")
                started = time.monotonic()
                result = subprocess.run(
                    [
                        str(runtime),
                        "-B",
                        "-X",
                        "utf8",
                        "-I",
                        str(a.bundle / "tools" / name),
                        "--bundle",
                        str(a.bundle),
                        "--output",
                        str(target),
                    ],
                    timeout=3 * 3600,
                )
                report["checks"].append(
                    {
                        "script": name,
                        "exit_code": result.returncode,
                        "seconds": time.monotonic() - started,
                    }
                )
                if result.returncode:
                    report["errors"].append(name + " 未通过")
                    break
            if a.role == "a4000" and not report["errors"]:
                cycles = json.loads((a.output / "audit_load_cycles/load-cycles.json").read_text("utf-8"))
                batch = json.loads((a.output / "audit_batch_stress/batch-stress.json").read_text("utf-8"))
                if (cycles.get("passed") is not True or len(cycles.get("cycles", [])) != 20
                    or batch.get("passed") is not True or batch.get("completed_tasks") != 500):
                    report["errors"].append("目标机缺少完整的 20 次循环或 500 张队列证据")
                else:
                    peaks = [c.get("sampled_peak_gpu_mib") for c in cycles["cycles"]]
                    peaks.append(batch.get("sampled_peak_gpu_mib"))
                    if any(type(peak) is not int for peak in peaks):
                        report["errors"].append("目标机显存采样缺失")
                    else:
                        peak = max(peaks)
                        headroom = report["gpu"]["total_mib"] - peak
                        report["gpu_headroom"] = {
                            "scope": "20 fixture cycles and 500 synthetic PP-OCR tasks only; intranet image workload must be measured separately",
                            "sampled_peak_device_mib": peak,
                            "minimum_free_mib": headroom,
                            "target_free_mib": 2048,
                            "passed": headroom >= 2048,
                        }
                        if headroom < 2048:
                            report["errors"].append("本次目标机测量未达到约 2 GB 显存余量目标")
            report["passed"] = not report["errors"]
    except Exception as error:
        report["errors"].append(str(error))
    (a.output / "target-machine.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
