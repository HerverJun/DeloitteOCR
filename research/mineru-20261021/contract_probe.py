"""Synthetic compatibility probes against Clear; never loads MinerU or models."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "contract-probe.json"
assert ROOT.drive.upper() == "F:"
sys.dont_write_bytecode = True


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


contracts = module("clear_research_contracts", ROOT / "products/Clear/platform/packages/contracts/models.py")
tables = module("clear_research_tables", ROOT / "products/Clear/ocr/src/ocr_workbench/tables.py")
checks = []


def check(name, action):
    action()
    checks.append({"name": name, "passed": True})


def equal(actual, expected):
    assert actual == expected, (actual, expected)


def rejected(payload):
    try:
        contracts.SourceLocator.model_validate(payload)
    except ValueError:
        return
    raise AssertionError("Expected locator validation rejection")


base = dict(schema_version="1.0", asset_id="asset1", asset_revision=1, blob_hash="a" * 64)
page = dict(base, kind="page", page_number=3, precision="page_only")
region = dict(base, kind="region", page_number=3, precision="approximate",
              coordinate_space="normalized", image_version_id="image1", transform_revision=1,
              box=[0.1, 0.2, 0.5, 0.6])

check("zero_based_source_pages_remain_noncontiguous", lambda: equal([p + 1 for p in [0, 2, 9]], [1, 3, 10]))
check("page_only_locator_supported", lambda: equal(contracts.SourceLocator.model_validate(page).precision, "page_only"))
check("page_zero_rejected", lambda: rejected(dict(page, page_number=0)))
check("normalized_region_with_transform_supported", lambda: equal(contracts.SourceLocator.model_validate(region).box, (0.1, 0.2, 0.5, 0.6)))
check("region_without_transform_rejected", lambda: rejected(dict(region, transform_revision=None)))
check("legacy_thousand_scale_box_rejected", lambda: rejected(dict(region, box=[100.0, 200.0, 500.0, 600.0])))

html = '<table><tr><th rowspan="2">科目</th><th>账号</th><th>原值</th></tr><tr><td>00123456789012345678</td><td>=1+1</td></tr></table>'
parsed = tables.parse_tables(html)
check("html_spans_supported", lambda: equal((parsed[0]["rows"], parsed[0]["columns"], parsed[0]["cells"][0]["row_span"]), (2, 3, 2)))
check("account_and_formula_like_text_remain_literal", lambda: equal([c["text"] for c in parsed[0]["cells"]][-2:], ["00123456789012345678", "=1+1"]))
check("html_does_not_invent_cell_geometry_or_confidence", lambda: equal([(c["polygon"], c["confidence"]) for c in parsed[0]["cells"]], [(None, None)] * 5))

result = {"kind": "synthetic_clear_contract_probe", "mineru_installed_or_executed": False,
          "accuracy_or_throughput_measured": False, "checks": checks,
          "passed": len(checks), "total": len(checks),
          "limitations": "Synthetic HTML and locator payloads only; not a MinerU output fixture or production adapter."}
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"passed": len(checks), "total": len(checks), "output": str(OUT)}, ensure_ascii=False))
