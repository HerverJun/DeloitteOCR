"""Independently read browser exports and final acceptance receipts."""

import argparse
import hashlib
import json
from pathlib import Path
from openpyxl import load_workbook

p = argparse.ArgumentParser()
p.add_argument("--evidence", type=Path, required=True)
a = p.parse_args()
root = a.evidence
for name in [
    "application-audit.json",
    "offline/network-probes.json",
    "offline/result.json",
    "offline/application.json",
    "relocation.json",
    "browser/result.json",
]:
    assert json.loads((root / name).read_text("utf-8"))["passed"], name
assert all(
    r["status"] == "passed"
    for r in json.loads((root / "dependency-audit.json").read_text("utf-8"))
)
assert (
    json.loads((root / "npm-audit.json").read_text("utf-8-sig"))["metadata"][
        "vulnerabilities"
    ]["total"]
    == 0
)
workbook = load_workbook(root / "browser/edited.xlsx")
cells = [c for sheet in workbook for row in sheet for c in row]
assert any(c.value == "扫描仪（浏览器校对）" for c in cells)
assert any(c.value == "00123456789012345678" and c.data_type == "s" for c in cells)
assert any(sheet.merged_cells.ranges for sheet in workbook)
for suffix in ["txt", "md"]:
    text = (root / ("browser/edited." + suffix)).read_text("utf-8")
    assert "扫描仪（浏览器校对）" in text
    assert "00123456789012345678" in text
result = json.loads((root / "browser/edited.json").read_text("utf-8"))
assert any(
    c["text"] == "扫描仪（浏览器校对）"
    for t in result["edited"]["tables"]
    for c in t["cells"]
)
assert not any(
    c["text"] == "扫描仪（浏览器校对）"
    for t in result["original"]["tables"]
    for c in t["cells"]
)
records = []
for file in sorted((root / "browser").glob("edited.*")):
    records.append(
        {
            "path": file.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
        }
    )
report = {
    "passed": True,
    "checks": [
        "all acceptance receipts passed",
        "dependency closure and npm audit",
        "browser XLSX edited text, string identifier and merged cells",
        "TXT and Markdown edited cells",
        "JSON original isolated from edits",
    ],
    "files": records,
}
(root / "independent-verification.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
)
print(json.dumps(report, ensure_ascii=False))
