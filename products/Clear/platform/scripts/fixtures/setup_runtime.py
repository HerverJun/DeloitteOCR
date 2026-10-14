"""Install a separate fixture Python from an explicit offline wheelhouse.

No project/global interpreter is upgraded. Example:
  python scripts/fixtures/setup_runtime.py --run-root <run> --wheelhouse <wheels>
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import venv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    args = parser.parse_args()
    lock = Path(__file__).with_name("requirements-windows-py312.lock.txt")
    runtime = args.run_root.resolve() / "workspaces/fixture-venv"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    venv.create(runtime, with_pip=True, system_site_packages=False)
    python = runtime / "Scripts/python.exe"
    env = os.environ.copy()
    for key in tuple(env):
        if any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "CREDENTIAL", "PASSWORD")):
            env.pop(key)
    env["PIP_CACHE_DIR"] = str(args.run_root.resolve() / "cache/fixtures-pip")
    subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(args.wheelhouse.resolve()),
                    "--require-hashes", "-r", str(lock)], check=True, env=env)
    subprocess.run([str(python), "-m", "pip", "check"], check=True, env=env)
    versions = subprocess.check_output([str(python), "-c",
        "import PIL,openpyxl,pyarrow,pypdf,reportlab,numpy; "
        "print(PIL.__version__,openpyxl.__version__,pyarrow.__version__,pypdf.__version__,reportlab.Version,numpy.__version__)"],
        text=True, env=env).strip()
    identity = {"python": str(python), "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
                "versions": versions, "site_packages_isolated": True}
    print(json.dumps(identity, indent=2))


if __name__ == "__main__":
    main()
