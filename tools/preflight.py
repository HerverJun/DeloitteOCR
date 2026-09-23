"""Read-only environment inventory. Does not install or reconfigure software."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def command(args: list[str], timeout: int = 20) -> dict:
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout)
        return {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}


def collect(probe_ml: bool) -> dict:
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "logical_cpus": os.cpu_count(),
        "disks": {}, "tools": {}, "packages": {},
        "limitations": ["Package presence does not prove compatibility.", "No model, export or throughput test was performed."],
    }
    roots = [Path("C:/"), Path("E:/")] if os.name == "nt" else [Path("/")]
    for root in roots:
        if root.exists():
            usage = shutil.disk_usage(root)
            report["disks"][str(root)] = {"total_bytes": usage.total, "free_bytes": usage.free}
    for name in ("git", "node", "pnpm", "uv", "docker", "nvidia-smi"):
        path = shutil.which(name)
        entry = {"path": path}
        if path and Path(path).suffix.lower() not in (".cmd", ".bat", ".ps1"):
            args = [path, "--version"]
            if name == "nvidia-smi":
                args = [path, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]
            entry["check"] = command(args)
        report["tools"][name] = entry
    for name in ("torch", "torchvision", "timm", "onnx", "onnxscript", "onnxruntime", "onnxruntime-gpu", "Pillow", "numpy", "fastapi", "sqlalchemy", "jsonschema"):
        try:
            report["packages"][name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            report["packages"][name] = None
    if os.name == "nt":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            script = "$c=Get-CimInstance Win32_Processor; $o=Get-CimInstance Win32_OperatingSystem; @{cpu=$c.Name;cores=$c.NumberOfCores;ram_bytes=([int64]$o.TotalVisibleMemorySize*1024)} | ConvertTo-Json -Compress"
            raw = command([ps, "-NoProfile", "-Command", script])
            try:
                report["hardware"] = json.loads(raw.get("stdout", ""))
            except json.JSONDecodeError:
                report["hardware"] = raw
    if probe_ml:
        probes = {
            "torch": "import json,torch; ok=torch.cuda.is_available(); r={'version':torch.__version__,'cuda_available':ok,'compiled_cuda':torch.version.cuda}; r.update({'device':torch.cuda.get_device_name(0),'sum':float(torch.ones(4,device='cuda').sum().item())} if ok else {}); print(json.dumps(r))",
            "onnxruntime": "import json,onnxruntime as ort; print(json.dumps({'version':ort.__version__,'providers':ort.get_available_providers()}))",
        }
        report["ml_probes"] = {name: command([sys.executable, "-c", code], timeout=45) for name, code in probes.items()}
        report["limitations"].append("ORT provider enumeration is not proof that a real model executes on GPU.")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--probe-ml", action="store_true")
    args = parser.parse_args()
    report = collect(args.probe_ml)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
