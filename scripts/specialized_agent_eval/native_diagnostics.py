"""Read actual source-bound detail diagnostics; never infer truth or acceptance."""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import ast
import importlib
import math
import re
import sys
from common import digest, fingerprint, fpath, read_json

SUPPORTED_VERSIONS = {"screen-diagnostics-v1", "screen-diagnostics-v2"}
STATIC_ITEM_FIELDS = ("id", "code", "severity", "reason", "location", "evidence", "details",
                      "human_review", "automatic_apply")
STATIC_DIAGNOSTIC_FIELDS = ("version", "groups", "checks", "summary", "binding", "review_required",
                            "human_review", "values_modified", "reference_input_used")


@contextmanager
def diagnostic_source(source, expected_files=None):
    """Import only the frozen pure CPU diagnostic package, restoring prior imports."""
    source = fpath(source)
    package = source / "src/ocr_workbench"
    needed = [package / (name + ".py") for name in
              ("__init__", "coordinates", "tables", "screen_quality", "screen_diagnostics", "image_utils")]
    if (package / 'screen_rectified_grid.py').is_file():
        needed.extend(package / (name + '.py') for name in ('screen_rectified_grid', 'screen_imaging'))
    hashes = {str(path): digest(path) for path in needed}
    if expected_files is not None and any(
            hashes[str(path)] != expected_files.get(path.relative_to(source).as_posix()) for path in needed):
        raise ValueError("Diagnostic module differs from the original frozen source inventory")
    previous = {name: value for name, value in sys.modules.items()
                if name == "ocr_workbench" or name.startswith("ocr_workbench.")}
    for name in previous:
        sys.modules.pop(name)
    sys.path.insert(0, str(source / "src"))
    try:
        module = importlib.import_module("ocr_workbench.screen_diagnostics")
        yield module
        loaded = {name: value for name, value in sys.modules.items()
                  if name == "ocr_workbench" or name.startswith("ocr_workbench.")}
        allowed = {"ocr_workbench", "ocr_workbench.coordinates", "ocr_workbench.tables",
                   "ocr_workbench.screen_quality", "ocr_workbench.screen_diagnostics", "ocr_workbench.image_utils"}
        if (package / 'screen_rectified_grid.py').is_file():
            allowed.update({'ocr_workbench.screen_rectified_grid', 'ocr_workbench.screen_imaging'})
        if not set(loaded) <= allowed or any(
                not fpath(value.__file__).is_relative_to(package) for value in loaded.values()):
            raise ValueError("Offline diagnostics imported an unexpected native module/source")
        if any(digest(path) != sha for path, sha in hashes.items()):
            raise ValueError("Frozen diagnostic source changed during CPU replay")
    finally:
        sys.path.pop(0)
        for name in list(sys.modules):
            if name == "ocr_workbench" or name.startswith("ocr_workbench."):
                sys.modules.pop(name)
        sys.modules.update(previous)


def object_value(value):
    import json
    answer = json.loads(value) if isinstance(value, str) else deepcopy(value)
    if not isinstance(answer, dict):
        raise ValueError("Native diagnostics requires an actual object")
    return answer


def validate_preparation(captured, original, row, index):
    if captured == original:
        return
    if not isinstance(original, dict) or not isinstance(captured, dict):
        raise ValueError('Diagnostic preparation differs from captured native DB evidence')
    expected = deepcopy(original)
    actual_images = captured.get('prepared_images', [])
    expected_images = expected.get('prepared_images', [])
    if not isinstance(actual_images, list) or len(actual_images) != len(expected_images):
        raise ValueError('Presented preparation image scope differs from native DB')
    workspace = None
    for actual, image in zip(actual_images, expected_images):
        url = actual.get('url')
        match = re.fullmatch(r'/api/platform/v1/workspaces/([A-Za-z][A-Za-z0-9_-]{0,63})/ocr/screen-batches/'
            + re.escape(index['batch_id']) + '/items/' + re.escape(row['input_version_id'])
            + '/files/' + re.escape(str(image['id'])), str(url))
        if match is None or workspace is not None and workspace != match[1] or 'path' in actual:
            raise ValueError('Presented preparation image URL/path refers to another native scope')
        workspace = match[1]
        image.pop('path', None)
        image['url'] = url
    if captured != expected:
        raise ValueError('Preparation changed beyond the explicit path-to-platform-URL presentation')


def validate_detail(detail, row, index, native_item, module, *, receipt_path, receipt_sha256):
    """Validate envelope, DB inputs and pure diagnostic replay before normalization."""
    task = row.get("stored_result_task") or {}
    expected = {"sample_id": row["id"], "source_image_sha256": row["input_sha256"],
                "input_version_id": row["input_version_id"], "image_id": task.get("image_id"),
                "status": row["native_status"], "result_id": row["result_id"],
                "result_revision": row["result_revision"], "review_state": row["review_state"]}
    if any(detail.get(key) != value for key, value in expected.items()):
        raise ValueError("Diagnostic detail sample/input/result/review identity differs")
    if (native_item.get("batch_id") != index["batch_id"] or any(
            native_item.get(key) != expected[key] for key in
            ("input_version_id", "image_id", "status", "result_id", "result_revision", "review_state"))):
        raise ValueError("Diagnostic detail is not bound to the actual batch input")
    result = detail.get("result") or {}
    if (result.get("id") != row["result_id"] or result.get("revision") != row["result_revision"]
            or result.get("task_id") != task.get("id")
            or detail.get("current_result_revision") != row["result_revision"]
            or detail.get("review_stale") is not False):
        raise ValueError("Diagnostic result revision is stale or missing")
    original, edited = object_value(result.get("original")), object_value(result.get("edited"))
    if (fingerprint(original) != row["result_original_sha256"]
            or fingerprint(edited) != row["result_edited_sha256"]):
        raise ValueError("Diagnostic original/edited result differs from validated native content")
    raw = {**deepcopy(original), **deepcopy(edited)}
    screen = raw.get("screen") or {}
    if (raw.get("project_image_version") != row["input_version_id"]
            or screen.get("batch_id") != index["batch_id"]
            or screen.get("input_sha256") != row["native_version_sha256"]
            or (screen.get("selection") or {}).get("selected_id") != row["selected_candidate_id"]
            or screen.get("sources") != row["source_receipts"]):
        raise ValueError("Diagnostic source/selected candidate identity differs")
    preparation = object_value(native_item["preparation"]) if native_item.get("preparation") else None
    evidence = object_value(native_item["evidence"]) if native_item.get("evidence") else {}
    validate_preparation(detail.get('preparation'), preparation, row, index)
    if any((part.get("provenance") or {}).get("contains_reference_input") is True
           for part in (raw, evidence, preparation or {})):
        raise ValueError("Diagnostics cannot consume reference input")
    diagnostic = detail.get("diagnostics")
    if (not isinstance(diagnostic, dict) or diagnostic.get("version") not in SUPPORTED_VERSIONS
            or diagnostic.get("version") != module.VERSION):
        raise ValueError("Unsupported or missing explicit native diagnostics version")
    version = diagnostic['version']
    binding = {"image_version": row["input_version_id"], "input_sha256": row["native_version_sha256"],
               "candidate_id": (raw.get("screen_selection") or {}).get("candidate_id"),
               "table_snapshot_sha256": fingerprint(raw.get("tables", []))}
    if (diagnostic.get("binding") != binding or diagnostic.get("result_id") != row["result_id"]
            or diagnostic.get("result_revision") != row["result_revision"]
            or diagnostic.get("input_version_id") != row["input_version_id"]):
        raise ValueError("Diagnostic table snapshot/input/result revision binding differs")
    remaining = (diagnostic.get("budget") or {}).get("remaining_seconds")
    if (type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining < 0
            or remaining > index["actual_policy"]["budget_seconds"]):
        raise ValueError("Diagnostic captured budget is outside the actual batch allowance")
    replay = module.build_screen_diagnostics(raw, preparation, evidence, remaining_seconds=remaining)
    if any(diagnostic.get(key) != replay.get(key) for key in STATIC_DIAGNOSTIC_FIELDS):
        raise ValueError("Diagnostic content/geometry differs from frozen-source CPU replay")
    grid = None
    if version == 'screen-diagnostics-v2':
        grid = diagnostic.get('grid_localization')
        if (not isinstance(grid, dict) or grid != replay.get('grid_localization')
                or grid.get('version') not in {'screen-grid-localization-v1','screen-grid-localization-v2'}
                or grid.get('reference_input_used') is not False
                or grid.get('values_modified') is not False
                or grid.get('human_review') != 'not_performed'):
            raise ValueError('Full observed grid proof differs from source-pixel/topology/anchor replay')
        if grid.get('cell_count', 0):
            source_pixels = fpath(preparation['input_path'])
            if (digest(source_pixels) != row['native_version_sha256']
                    or preparation.get('input_sha256') != row['native_version_sha256']
                    or grid['binding']['input_sha256'] != row['native_version_sha256']
                    or grid['binding']['table_snapshot_sha256'] != binding['table_snapshot_sha256']):
                raise ValueError('Observed grid source pixels/table are not bound to the actual native version')
    actual_items, replay_items = diagnostic.get("items"), replay.get("items")
    if not isinstance(actual_items, list) or len(actual_items) != len(replay_items):
        raise ValueError("Diagnostic item scope/count differs from frozen-source CPU replay")
    for actual, expected_item in zip(actual_items, replay_items):
        if not isinstance(actual, dict) or any(actual.get(key) != expected_item.get(key) for key in STATIC_ITEM_FIELDS):
            raise ValueError("Diagnostic issue/location/evidence differs from CPU replay")
    regions = []
    for item in actual_items:
        location = item["location"]
        level = location["level"]
        region = {"scope": level, "code": item["code"], "issue_id": item["id"],
                  "table_index": location.get("table"), "reliable": False,
                  "evidence_ref": str(receipt_path) + "#diagnostics/items/" + item["id"],
                  "native_detail_sha256": receipt_sha256,
                  "source_binding": deepcopy(binding), "observed_location": deepcopy(location),
                  "qualification": "source_bound_cpu_replay; no human review or automatic acceptance"}
        if level == "cell":
            # CPU replay independently resolves this location from an observed
            # full-cell source polygon; no self-reported reliable flag is read.
            if location.get("range_semantics") != "full_cell" or not location.get("box"):
                raise ValueError("Diagnostic cell has no replayed full-cell source geometry")
            basis = location.get('basis')
            if basis in {'source_pixels_observed_partition_and_independent_row_column_anchors',
                         'source_pixels_rectified_partition_independent_anchors_inverse_projection'}:
                table_index = location.get('table')
                tables = raw.get('tables', [])
                if not grid or type(table_index) is not int or not 0 <= table_index < len(tables):
                    raise ValueError('Grid cell location has no bound complete table proof')
                cells = [cell for cell in tables[table_index]['cells']
                         if cell['row'] == location['row'] and cell['column'] == location['column']]
                if (len(cells) != 1 or module.localized_grid_box(grid, table_index, cells[0]) != location['box']):
                    raise ValueError('Grid cell location does not match independently observed full-cell boundaries')
                if hasattr(module,'localized_grid_cell') and module.localized_grid_cell(
                        grid,table_index,cells[0])['polygon'] != location.get('polygon'):
                    raise ValueError('Grid cell polygon differs from independently observed source boundary')
            elif basis != 'observed_full_cell_geometry':
                raise ValueError('Diagnostic cell location uses an unsupported geometry basis')
            region.update(row=location["row"], column=location["column"], reliable=True)
        regions.append(region)
    return {"version": version, "receipt_path": str(receipt_path), "receipt_sha256": receipt_sha256,
            "source_binding": binding, "source_replay_verified": True, "review_regions": regions,
            "reported_items": deepcopy(actual_items), "automatic_delivery_qualification": False,
            "human_review_qualification": False,
            "grid_localization_replayed": version == 'screen-diagnostics-v2',
            "observed_grid_cell_count": grid['cell_count'] if grid else 0}


def collect_diagnostics(index, tables):
    """Consume only detail files already captured by the unchanged identity adapter."""
    run = fpath(index["native_run"])
    prepared_path = run / "prepared.json"
    if digest(prepared_path) != index["prepared_sha256"]:
        raise ValueError("Native prepared receipt changed before diagnostic collection")
    prepared = read_json(prepared_path)
    source = fpath(prepared["native_source"])
    batches_file = source / 'src/ocr_workbench/screen_batches.py'
    requires_diagnostics = False
    if batches_file.relative_to(source).as_posix() in prepared['native_source_files']:
        if digest(batches_file) != prepared['native_source_files'][batches_file.relative_to(source).as_posix()]:
            raise ValueError('Frozen native detail implementation changed')
        for definition in ast.parse(batches_file.read_text('utf-8')).body:
            if not isinstance(definition, ast.ClassDef) or definition.name != 'ScreenBatches':
                continue
            for method in definition.body:
                if not isinstance(method, ast.FunctionDef) or method.name != 'detail':
                    continue
                requires_diagnostics = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'build_screen_diagnostics' for node in ast.walk(method))
    actual_items = {(item["batch_id"], item["input_version_id"]): item for item in tables["screen_items"]}
    output = {}
    paths = index.get("detail_receipt_sha256", {})
    for row in index["rows"]:
        path = run / "native-receipts" / (row["id"] + ".json")
        expected_sha = paths.get(str(path))
        if expected_sha is None:
            if path.exists() and index.get("native_flavor") == "agent":
                raise ValueError("A detail receipt appeared after identity collection")
            continue
        if digest(path) != expected_sha:
            raise ValueError("Native detail changed after identity collection")
        detail = read_json(path)
        if "diagnostics" not in detail:
            if requires_diagnostics and row.get('scoring_status') == 'success':
                raise ValueError('Current frozen native detail omitted required diagnostics')
            continue  # Explicit legacy absence is not invented localization.
        if row.get("scoring_status") != "success":
            raise ValueError("Diagnostics claims selected content for a non-success native input")
        native_item = actual_items[(index["batch_id"], row["input_version_id"])]
        with diagnostic_source(source, prepared["native_source_files"]) as module:
            output[row["id"]] = validate_detail(detail, row, index, native_item, module,
                receipt_path=path, receipt_sha256=expected_sha)
        if digest(path) != expected_sha:
            raise ValueError("Native detail changed during diagnostic replay")
    if digest(prepared_path) != index["prepared_sha256"]:
        raise ValueError("Native prepared receipt changed during diagnostic collection")
    return output
