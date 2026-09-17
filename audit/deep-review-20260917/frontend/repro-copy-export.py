"""Exercise the real export path without models or a browser; temporary store only."""
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from ocr_workbench.exporting import build_export
from ocr_workbench.editing import export_text
from test_fusion_store import FusionStoreTests

case = FusionStoreTests()
case.setUp()
try:
    result_id, originals = case.create()
    target = build_export(case.store, [result_id], "txt")
    payload = target.read_bytes()
    expected = export_text(case.store.result(result_id)["edited"])
    # This is the same UTF-8 decoding Response.text() performs in App.tsx.
    actual = payload.decode("utf-8", errors="replace")
    with zipfile.ZipFile(target) as archive:
        members = archive.namelist()
        embedded_text = archive.read("OCR-result.txt").decode("utf-8")
    control_target = build_export(case.store, [originals[0]], "txt")
    assert actual.startswith("PK\x03\x04")
    assert actual != expected
    assert embedded_text.replace("\r\n", "\n") == expected.replace("\r\n", "\n")
    assert control_target.suffix == ".txt"
    output = {
        "reproduced": True,
        "source_handler": "frontend/src/App.tsx:1203-1212",
        "request": {"result_ids": ["synthetic-fusion-result"], "format": "txt"},
        "returned_filename": target.name,
        "returned_prefix_hex": payload[:4].hex(),
        "zip_members": members,
        "clipboard_equals_expected_text": actual == expected,
        "clipboard_character_count": len(actual),
        "expected_text": expected,
        "plain_result_control_extension": control_target.suffix,
    }
    destination = Path(__file__).with_name("copy-export-repro.json")
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
finally:
    case.doCleanups()
