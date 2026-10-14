"""Executed under offline_guard.exe's dynamic OS WFP isolation."""

import argparse
import hashlib
import winreg
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--application", action="store_true")
    a = p.parse_args()
    if a.output.exists() and any(a.output.iterdir()):
        raise ValueError("Use an empty OS-isolation evidence directory")
    a.output.mkdir(parents=True, exist_ok=True)
    probe = """import socket,json
s=socket.socket();s.settimeout(4);code=s.connect_ex(('1.1.1.1',443));s.close()
with socket.socket() as server:
 server.bind(('127.0.0.1',0));server.listen()
 with socket.create_connection(server.getsockname(),timeout=2):
  client,_=server.accept();client.close()
print(json.dumps({'external_connect_error':code,'loopback':'passed'}))
raise SystemExit(0 if code==10013 else 1)
"""
    results = []
    for runtime in ["control", "ppocr", "paddlevl", "glm", "hunyuan"] + (
        ["service"] if a.application else []
    ):
        res = subprocess.run(
            [
                str(a.bundle / "runtimes" / runtime / "python.exe"),
                "-X",
                "utf8",
                "-I",
                "-c",
                probe,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        results.append(
            {
                "runtime": runtime,
                "exit_code": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )
    native = subprocess.run(
        [str(a.bundle / "tools/offline_guard.exe"), "--probe"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    results.append(
        {
            "runtime": "native_winsock",
            "exit_code": native.returncode,
            "stdout": native.stdout,
            "stderr": native.stderr,
        }
    )
    passed = all(r["exit_code"] == 0 for r in results)
    (a.output / "os-network-probes.json").write_text(
        json.dumps({"passed": passed, "results": results}, indent=2), encoding="utf-8"
    )
    if not passed:
        raise RuntimeError(
            "OS network isolation was not demonstrated; refusing to label inference as air-gapped"
        )
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
    ) as key:
        machine_hash = hashlib.sha256(
            winreg.QueryValueEx(key, "MachineGuid")[0].encode("utf-8")
        ).hexdigest()
    binding = {
        "machine_sha256": machine_hash,
        "bundle_manifest_sha256": hashlib.sha256(
            (a.bundle / "manifest.json").read_bytes()
        ).hexdigest(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    script = "audit_application.py" if a.application else "run_acceptance.py"
    res = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-I",
            str(a.bundle / "tools" / script),
            "--bundle",
            str(a.bundle),
            "--output",
            str(a.output / "inference"),
        ]
        + (["--quick"] if a.application else []),
        timeout=40 * 60,
    )
    (a.output / "os-isolation-result.json").write_text(
        json.dumps(
            {
                **binding,
                "passed": res.returncode == 0,
                "method": "Windows Filtering Platform ALE_AUTH_CONNECT V4/V6 per executable, dynamic session",
                "external_probe": "WSAEACCES 10013 required for all Python runtimes and native Winsock",
                "loopback": "allowed and tested",
                "acceptance_exit_code": res.returncode,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return res.returncode


if __name__ == "__main__":
    raise SystemExit(main())
