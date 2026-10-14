"""Measure real packaged startup only: first, cached, forced and review-only."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from audit_application import Application, until
import psutil


class ExistingProcess:
    def __init__(self, pid):
        self.process = psutil.Process(pid)
        self.pid = pid

    def poll(self):
        return None if self.process.is_running() else 0

    def wait(self, timeout):
        return self.process.wait(timeout)

    def terminate(self):
        self.process.terminate()


class MeasuredApplication(Application):
    def __init__(self, bundle, output):
        super().__init__(bundle, output)
        self.environment = os.environ.copy()
        self.environment["LOCALAPPDATA"] = str(output / "test-local-profile")

    def start(self, *, force=False, review_only=False, adopt=False):
        started = time.monotonic()
        state_file = self.data / "launcher/launcher-state.json"
        if adopt:
            state = json.loads(state_file.read_text("utf-8"))
            assert Path(state["bundle"]).resolve() == self.bundle
            assert Path(state["data"]).resolve() == self.data
            self.process = ExistingProcess(state["pid"])
            assert Path(self.process.process.exe()).resolve() == self.bundle / "launcher/OfflineOCRLauncher.exe"
            assert "--verify-startup" in self.process.process.cmdline()
        else:
            launch = subprocess.STARTUPINFO()
            launch.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            launch.wShowWindow = 0
            self.process = subprocess.Popen([
                str(self.bundle / "launcher/OfflineOCRLauncher.exe"), "--data", str(self.data), "--no-browser",
                *(["--verify-startup"] if force else []), *(["--review-only"] if review_only else []),
            ], env=self.environment, startupinfo=launch, creationflags=subprocess.CREATE_NO_WINDOW)
        events = []
        last_event = None
        last_log = 0
        self.startup = None
        service_ready_seconds = None

        def ready():
            nonlocal last_event, last_log, service_ready_seconds
            if self.process.poll() is not None:
                raise RuntimeError("Launcher exited before readiness")
            try:
                state = json.loads(state_file.read_text("utf-8"))
                if state["pid"] != self.process.pid:
                    return False
                self.state = state
                self.base = "http://127.0.0.1:" + str(state["port"])
                self.token = (self.data / "launcher/session-token.txt").read_text("utf-8").strip()
                status = json.loads((self.data / "launcher/startup-state.json").read_text("utf-8"))
                self.startup = status
                event = {key: status.get(key) for key in ("status", "verification", "reason", "notice", "message")}
                if event != last_event:
                    events.append({"seconds_since_observation": round(time.monotonic() - started, 3), **event})
                    last_event = event
                if status["status"] == "failed":
                    raise RuntimeError(status["message"])
                if time.monotonic() - last_log > 20:
                    print(json.dumps({"phase": "startup", "message": status["message"]}, ensure_ascii=False), flush=True)
                    last_log = time.monotonic()
                self.health = self.api("/health", timeout=1)
                if self.health["status"] == "ready" and service_ready_seconds is None and not adopt:
                    service_ready_seconds = round(time.monotonic() - started, 3)
                return self.health["status"] == "ready" and state.get("ready") is True and status["status"] == "passed"
            except (OSError, ValueError, urllib.error.URLError):
                return False

        until(ready, 600)
        assert self.health["review_only"] == review_only
        elapsed = (state_file.stat().st_mtime - self.process.process.create_time()) if adopt else (time.monotonic() - started)
        measurement = {"ready_seconds": round(elapsed, 3), "service_ready_seconds": service_ready_seconds,
                       "startup": self.startup, "events": events,
                       "clock": "ready-state-file minus process creation" if adopt else "launch to observed GUI and service readiness"}
        print(json.dumps({"phase": "ready", "verification": self.startup["verification"],
                          "ready_seconds": measurement["ready_seconds"]}), flush=True)
        return measurement

    def stop(self):
        if not self.process or self.process.poll() is not None:
            return
        if not self.startup or self.startup.get("status") == "failed" or not getattr(self, "state", {}).get("ready"):
            self.process.terminate()
            self.process.wait(15)
            return
        super().stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Retain measurements for the same exact bundle")
    parser.add_argument("--adopt-running-startup", action="store_true", help="Finish the isolated test launcher's already-running forced startup")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=args.resume)
    app = MeasuredApplication(args.bundle.resolve(), output)
    report = {"passed": False, "starts": []}
    if args.resume:
        report = json.loads((output / "verification.json").read_text("utf-8"))
        assert not report["passed"]
    report["scope"] = "Actual compiled launcher and embedded service startup only; isolated profile. No OCR, fusion, correction or export tasks submitted by this verifier."
    try:
        for key, path in (("manifest_sha256", app.bundle / "manifest.json"),
                          ("launcher_sha256", app.bundle / "launcher/OfflineOCRLauncher.exe")):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert key not in report or report[key] == digest
            report[key] = digest
        names = {s["name"] for s in report["starts"]}
        if "first" not in names:
            first = app.start()
            report["starts"].append({"name": "first", **first})
            assert first["startup"]["verification"] == "full" and first["startup"]["reason"] == "first_start"
            assert any("首次启动需要" in (e["notice"] or "") for e in first["events"])
            app.stop()
        for index in range(3):
            name = f"cached-{index + 1}"
            if name in names:
                continue
            cached = app.start()
            report["starts"].append({"name": name, **cached})
            assert cached["startup"]["verification"] == "cached"
            assert "native-runtime-imports" not in cached["startup"]["checks"]
            app.stop()
        if "forced" not in names:
            forced = app.start(force=True, adopt=args.adopt_running_startup)
            report["starts"].append({"name": "forced", **forced})
            assert forced["startup"]["verification"] == "full" and forced["startup"]["reason"] == "forced"
            assert "full-file-sha256" in forced["startup"]["checks"]
            app.stop()
        review = app.start(review_only=True)
        report["starts"].append({"name": "review-only", **review})
        assert review["startup"]["verification"] == "cached"
        report["passed"] = True
    finally:
        try:
            app.stop()
        finally:
            if app.process and app.process.poll() is None:
                app.process.terminate()
                app.process.wait(15)
            (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({"passed": report["passed"], "starts": [{"name": s["name"], "ready_seconds": s["ready_seconds"]} for s in report["starts"]]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
