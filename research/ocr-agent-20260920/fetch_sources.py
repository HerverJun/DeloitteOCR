"""Download pinned public source excerpts for the OCR agent architecture review."""
import concurrent.futures
import base64
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).parent / "sources"
FILES = {
    "deepseek-ai/deepseek-harness": [
        "README.zh.md", "LICENSE", "package.json", "docs/architecture.zh.md",
        "packages/core/agent-loop/src/agent.ts",
        "packages/core/agent-loop/src/tool-calls.ts",
        "packages/core/tools/src/index.ts",
        "packages/core/session/src/index.ts",
        "packages/sdk/server/src/index.ts",
        "packages/compaction/compaction/src/tool-pairing.ts",
        "docs/user/guide/python-sdk.zh.md",
        "docs/tool-execution-pipeline.zh.md",
        "packages/bundle/sdk-minimal/README.zh.md",
    ],
    "anomalyco/opencode": [
        "README.md", "LICENSE", "package.json",
        "packages/core/src/session/prompt.ts",
        "packages/core/src/session/execution.ts",
        "packages/core/src/session/compaction.ts",
        "packages/core/src/tool/registry.ts",
        "packages/core/src/tool/tool.ts",
        "packages/core/src/permission.ts",
        "packages/server/src/handlers/event.ts",
        "packages/server/src/handlers/session.ts",
    ],
    "langchain-ai/deepagents": [
        "README.md", "LICENSE",
        "libs/deepagents/pyproject.toml",
        "libs/deepagents/deepagents/graph.py",
        "libs/deepagents/deepagents/middleware/permissions.py",
        "libs/deepagents/deepagents/middleware/summarization.py",
    ],
}


def fetch(item):
    repo, path = item
    key = repo.replace("/", "__")
    meta = json.loads((ROOT / (key + ".meta.json")).read_text(encoding="utf-8"))
    url = f"https://raw.githubusercontent.com/{repo}/{meta['sha']}/{path}"
    record = {"repo": repo, "path": path, "commit": meta["sha"], "url": url}
    target = ROOT / key / path
    if target.is_file():
        data = target.read_bytes()
        record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), saved=target.relative_to(ROOT.parent).as_posix(), retrieval="local snapshot from pinned fetch")
        return record
    try:
        api_url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={meta['sha']}"
        request = urllib.request.Request(api_url, headers={"User-Agent": "OCR-Agent-Research"})
        with urllib.request.urlopen(request, timeout=25) as response:
            data = base64.b64decode(json.load(response)["content"])
        record["retrieval_url"] = api_url
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), saved=target.relative_to(ROOT.parent).as_posix())
    except Exception as exc:
        record["error"] = str(exc)
    return record


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(fetch, [(repo, path) for repo, paths in FILES.items() for path in paths]))
    (ROOT.parent / "source-index.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"downloaded": sum("saved" in r for r in records), "errors": [r for r in records if "error" in r]}, ensure_ascii=True))
