"""Trusted gate: wait for Job Object assignment before executing a frozen command."""

import json
import subprocess
import sys


def main():
    line = sys.stdin.readline()
    if not line:
        return 125
    payload = json.loads(line)
    argv = payload["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        return 125
    # This process is already assigned to the parent's non-breakaway Job Object.
    child = subprocess.Popen(argv, cwd=payload["cwd"], env=payload["env"], shell=False)
    result = child.wait()
    print("HARNESS_RESULT:" + json.dumps({"exit_code": result, "pid": child.pid}), flush=True)
    return result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("HARNESS_ERROR:" + type(exc).__name__, flush=True)
        sys.exit(125)
