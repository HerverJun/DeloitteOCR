"""Isolated loopback server for the real data-workbench browser check."""

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--run-root", type=Path, required=True)
args = parser.parse_args()
run = args.run_root.resolve()
output = Path(os.environ.get("WORKBENCH_E2E_OUTPUT", str(run / "portal"))).resolve()
output.mkdir(parents=True, exist_ok=True)
data = output / ("data-ui-" + uuid.uuid4().hex)
data.mkdir()
os.environ["WORKBENCH_DATA_DIR"] = str(data)
os.environ["WORKBENCH_DATA_PYTHON"] = str(run / "workspaces/data-venv/Scripts/python.exe")
os.environ["WORKBENCH_PORTAL_DIST"] = str(ROOT / "apps/portal/dist")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from workbench_platform.app import app


class Server(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        port = self.servers[0].sockets[0].getsockname()[1]
        print(json.dumps({"origin": f"http://127.0.0.1:{port}",
                          "data_dir": str(data), "pid": os.getpid()}), flush=True)


Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                      access_log=False)).run()
