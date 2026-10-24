import { describe, expect, it } from "vitest";
import { screenBatchStartBody, screenCell, screenCellEdit, screenEditKey, screenEtaText, screenProgress, screenReviewBody, selectedScreenVersions, screenNumericExport, screenNumericColumnOptions, screenCandidateCell, screenCandidateReviewBody, type ScreenItem, type ScreenEta } from "./screenBatchWorkflow";
import type { Photo, Table } from "./types";
import { screenDiagnosticLocation, screenComparisonBody, type ScreenDiagnostic } from "./screenBatchWorkflow";

const item: ScreenItem = { input_version_id: "version-a", image_id: "image-a", source_name: "财务.png", status: "needs_review", stage: "review",
  result_id: "result-a", result_revision: 7, review_state: "automatic", human_confirmed: false, issues: [] };
const table: Table = { rows: 2, columns: 2, cells: [
  { row: 0, column: 0, row_span: 1, column_span: 2, text: "明细" },
  { row: 1, column: 0, row_span: 1, column_span: 1, text: "000123456789012345678" },
  { row: 1, column: 1, row_span: 1, column_span: 1, text: "-10.05" },
] };

describe("new batch precision submission", () => {
  it.each([[1, 36000], [200, 72000], [240, 86400], [1000, 360000]])(
    "allocates the original budget for %i images before submission",
    (count, batchBudget) => {
      const versions = Array.from({ length: count }, (_, i) => `version-${i}`);
      const body = screenBatchStartBody(versions, "screen-ui-retry", true);
      expect(body).toEqual({ version_ids: versions, request_id: "screen-ui-retry", policy: {
        primary_engine: "glm", fallback_engines: ["hunyuan"], max_upgrades: 1,
        mode: "accuracy", upgrade_trigger: "failure_only", layout_evidence: true,
        require_human_review: true, budget_seconds: 360, request_timeout_seconds: 180,
        engine_route: "business_language_v1", technical_fallback_reserve_seconds: 90,
        batch_budget_seconds: batchBudget,
      } });
      expect(screenBatchStartBody(versions, body.request_id, true)).toEqual(body);
      expect(body.version_ids).not.toBe(versions);
    });
  it("keeps ordinary mode's existing request and rejects unsupported counts", () => {
    expect(screenBatchStartBody(["version-a"], "screen-ui-normal", false)).toEqual({
      version_ids: ["version-a"], request_id: "screen-ui-normal",
      policy: { primary_engine: "auto", mode: "auto" },
    });
    for (const versions of [[], Array(1001).fill("version-a")]) {
      expect(() => screenBatchStartBody(versions, "screen-ui-invalid", true)).toThrow("1–1000");
    }
  });
});

describe("diagnostic evidence gates", () => {
  it("pins same-revision human review changes in the native review body", () => {
    const token="a".repeat(64);
    expect(screenReviewBody({...item,review_binding_token:token},7,"retain",[],"review-one").expected_review_token).toBe(token);
  });
  const diagnostic: ScreenDiagnostic = { id:"issue-1",code:"difference",severity:"high",reason:"金额不同",location:{level:"cell",table:0,row:1,column:1,box:[10,20,90,60],basis:"observed_full_cell_geometry",range_semantics:"full_cell"},evidence:[],applicable_actions:[] };
  it("keeps reliable cell/table geometry distinct and never highlights indexes alone", () => {
    expect(screenDiagnosticLocation(diagnostic).level).toBe("cell");
    expect(screenDiagnosticLocation({...diagnostic,location:{...diagnostic.location,box:null}}).polygon).toBeNull();
    expect(screenDiagnosticLocation({...diagnostic,location:{...diagnostic.location,basis:"same_indexes"}}).level).toBe("image");
    expect(screenDiagnosticLocation({...diagnostic,location:{...diagnostic.location,box:[90,20,10,60]}}).polygon).toBeNull();
    const region=screenDiagnosticLocation({...diagnostic,location:{level:"table",table:0,box:[10,20,190,160],basis:"observed_table_region"}});
    expect(region.level).toBe("table"); expect(region.reason).toContain("未定位到单格");
  });
  it("keeps verified perspective polygons and rejects unsupported or malformed projections", () => {
    const polygon = [[10, 20], [85, 25], [90, 60], [15, 55]];
    const projected = { ...diagnostic, location: { ...diagnostic.location, polygon,
      basis: "source_pixels_rectified_partition_independent_anchors_inverse_projection" } };
    expect(screenDiagnosticLocation(projected)).toMatchObject({ level: "cell", polygon });
    expect(screenDiagnosticLocation({ ...projected, location: { ...projected.location, polygon: null } }).polygon).toBeNull();
    expect(screenDiagnosticLocation({ ...projected, location: { ...projected.location, polygon: [[10, 20], [90, 60], [85, 25], [15, 55]] } }).polygon).toBeNull();
    expect(screenDiagnosticLocation({ ...projected, location: { ...projected.location, polygon: [[10, 20], [85, 25], [91, 60], [15, 55]] } }).polygon).toBeNull();
    expect(screenDiagnosticLocation({ ...projected, location: { ...projected.location, basis: "candidate_interpolated_grid" } }).level).toBe("image");
    expect(screenDiagnosticLocation({ ...projected, location: { ...projected.location, basis: "source_pixels_observed_partition_and_independent_row_column_anchors" } }).level).toBe("cell");
    const presented = screenDiagnosticLocation(projected).polygon!;
    presented[0][0] = 99;
    expect(polygon[0][0]).toBe(10);
  });
  it("comparison pins current result and image candidate without confirming or editing", () => {
    const input={...item,candidates:[{id:"alternative",engine:"glm",table_count:1}]};
    expect(screenComparisonBody(input,"alternative",7)).toEqual({result_id:"result-a",expected_revision:7,candidate_id:"alternative"});
    expect(() => screenComparisonBody(input,"alternative",6)).toThrow("版本已变化");
    expect(() => screenComparisonBody(input,"other-image",7)).toThrow("候选");
  });
  it("compares the actual edited revision while leaving the prior human review stale", () => {
    const edited = { ...item, result_revision: 7, current_result_revision: 8, review_stale: true,
      result: { revision: 8, edited: { tables: [table] } }, candidates: [{ id: "alternative", engine: "glm", table_count: 1 }] };
    expect(screenComparisonBody(edited, "alternative", 8).expected_revision).toBe(8);
    expect(() => screenComparisonBody(edited, "alternative", 7)).toThrow("版本已变化");
    expect(() => screenNumericExport({ ...edited, human_confirmed: true, status: "completed" }, { "0": [1] }, true)).toThrow("核对");
    expect(edited.result_revision).toBe(7);
    expect(edited.review_stale).toBe(true);
  });
});

describe("screen batch workflow", () => {
  it("labels an ETA as the queued phase and keeps unknown or paused time explicit", () => {
    const eta: ScreenEta = { remaining_seconds: 125, stage: { kind:"ocr",engine:"hunyuan",pass_index:0,label:"Hunyuan首轮识别" },
      sample_count:3,pending_tasks:2,cached_pending_tasks:1,reason:"estimated" };
    expect(screenEtaText(eta)).toBe("已排队的Hunyuan首轮识别预计还需约 3 分钟。");
    expect(screenEtaText({...eta,remaining_seconds:null,reason:"insufficient_samples"})).toContain("暂无法估计");
    expect(screenEtaText({...eta,remaining_seconds:null,reason:"active_task_exceeds_observed_duration"})).toContain("超出本批已完成样本");
    expect(screenEtaText({...eta,remaining_seconds:null,reason:"paused"})).toContain("继续后再估计");
    expect(screenEtaText({...eta,remaining_seconds:null})).not.toContain("0 秒");
  });
  it("submits one batch reference list for 240 current-project images", () => {
    const images = Array.from({ length: 240 }, (_, index) => ({ id: `image-${index}`, active_version: `v-${index}` } as Photo));
    const ids = selectedScreenVersions(images, [], "project");
    expect(ids).toHaveLength(240);
    expect(ids[239]).toBe("v-239");
    expect(selectedScreenVersions(images, ["image-239", "image-1"], "selected")).toEqual(["v-1", "v-239"]);
  });
  it("does not silently change scope when selection is empty", () => {
    expect(selectedScreenVersions([{ id: "a", active_version: "v-a" } as Photo], [], "selected")).toEqual([]);
  });
  it("maps a covered merged slot to the actual editable anchor", () => {
    const cell = screenCell(table, 0, 1)!;
    expect(cell.column).toBe(0);
    expect(screenCellEdit(0, cell, "新明细")).toEqual({ table_index: 0, row: 0, column: 0, before: "明细", after: "新明细" });
    expect(screenCell(table, 2, 0)).toBeUndefined();
  });
  it("keeps exact long ID text, signs and decimal values in edits", () => {
    const edit = screenCellEdit(0, table.cells[1], "0000123456789012345678");
    expect(edit.before).toBe("000123456789012345678");
    expect(edit.after).toBe("0000123456789012345678");
    expect(screenEditKey(edit)).toBe("0:1:0");
    expect(screenCellEdit(0, table.cells[2], "-10.50").before).toBe("-10.05");
  });
  it("saves only changed cells with exact result revision and original image scope", () => {
    const edit = screenCellEdit(0, table.cells[2], "-10.50");
    const unchanged = screenCellEdit(0, table.cells[1], table.cells[1].text);
    const body = screenReviewBody(item, 7, "accepted", [edit, unchanged], "screen-test-123");
    expect(body).toMatchObject({ action: "review", version_ids: ["version-a"], result_id: "result-a", expected_revision: 7, state: "accepted", edits: [edit] });
    expect(screenReviewBody(item, 7, "retain", [edit], "screen-test-456").state).toBe("retain");
  });
  it("deferral never applies a pending edit or pretends to confirm", () => {
    const body = screenReviewBody(item, 7, "deferred", [screenCellEdit(0, table.cells[2], "10.00")], "screen-test-789");
    expect(body.edits).toEqual([]);
    expect(body.state).toBe("deferred");
    expect(item.human_confirmed).toBe(false);
  });
  it("rejects an absent result, stale invalid revision and oversized edit collection", () => {
    expect(() => screenReviewBody({ ...item, result_id: null }, 7, "accepted", [], "screen-test-1")).toThrow();
    expect(() => screenReviewBody(item, -1, "accepted", [], "screen-test-1")).toThrow();
    expect(() => screenReviewBody(item, 7, "accepted", Array(101).fill(screenCellEdit(0,table.cells[2],"10")), "screen-test-1")).toThrow("100");
  });
  it("counts pending review and failure as processed without calling them delivered", () => {
    const counts = { total: 240, completed: 180, needs_review: 25, failed: 5, cancelled: 0, pending: 30 };
    expect(screenProgress(counts)).toEqual({ terminal: 210, percent: 88 });
    expect(counts.completed).toBe(180);
    expect(screenProgress({ total: 0, completed: 0, needs_review: 0, failed: 0, cancelled: 0, pending: 0 })).toEqual({ terminal: 0, percent: 0 });
  });

  it("leaves the export unchanged by default and scopes chosen numeric columns to one reviewed image", () => {
    const numericTable: Table = { rows: 2, columns: 3, cells: [
      { row: 0, column: 0, row_span: 1, column_span: 1, text: "编号" },
      { row: 0, column: 1, row_span: 1, column_span: 1, text: "金额" },
      { row: 0, column: 2, row_span: 1, column_span: 1, text: "数量" },
      { row: 1, column: 0, row_span: 1, column_span: 1, text: "0001" },
      { row: 1, column: 1, row_span: 1, column_span: 1, text: "-10.05" },
      { row: 1, column: 2, row_span: 1, column_span: 1, text: "2" },
    ] };
    const reviewed = { ...item, status: "completed", human_confirmed: true, result: { revision: 7, edited: { tables: [numericTable] } } };
    expect(screenNumericExport(reviewed, {}, false)).toBeUndefined();
    expect(screenNumericExport(reviewed, { "0": [2, 1, 1] }, true)).toEqual({ "version-a": { "0": [1, 2] } });
    expect(screenNumericColumnOptions(numericTable)[0].identifier).toBe(true);
    expect(() => screenNumericExport(reviewed, { "0": [0] }, true)).toThrow("编号");
    expect(() => screenNumericExport(reviewed, { "0": [1] }, false)).toThrow("确认");
    expect(() => screenNumericExport({ ...reviewed, human_confirmed: false }, { "0": [1] }, true)).toThrow();
    expect(() => screenNumericExport({ ...reviewed, result_revision: 6 }, { "0": [1] }, true)).toThrow();
    expect(numericTable.cells[3].text).toBe("0001");
  });

  it("compares candidate cells only with the same valid topology and unique source geometry", () => {
    const current = structuredClone(table);
    current.cells[2].polygon = [[20,20],[40,20],[40,40],[20,40]];
    const alternate = structuredClone(current);
    alternate.cells[2].text = "-10.50";
    const match = screenCandidateCell([current],[alternate],0,1,1);
    expect(match.cell?.text).toBe("-10.50");
    expect(match.basis).toBe("geometry");
    alternate.cells[1].polygon = alternate.cells[2].polygon;
    expect(screenCandidateCell([current],[alternate],0,1,1).cell).toBeUndefined();
    expect(screenCandidateCell([current],[alternate,alternate],0,1,1).cell).toBeUndefined();
    alternate.cells[0].column_span = 1;
    expect(screenCandidateCell([current],[alternate],0,1,1).cell).toBeUndefined();
  });

  it("requires distinct row and column anchors without geometry and refuses repeated text matches", () => {
    const current: Table = { rows: 2, columns: 2, cells: [
      {row:0,column:0,row_span:1,column_span:1,text:"编号"},
      {row:0,column:1,row_span:1,column_span:1,text:"金额"},
      {row:1,column:0,row_span:1,column_span:1,text:"0001"},
      {row:1,column:1,row_span:1,column_span:1,text:"-10.05"},
    ] };
    const alternate = structuredClone(current);
    alternate.cells[3].text = "-10.50";
    expect(screenCandidateCell([current],[alternate],0,1,1).basis).toBe("unique_text_anchors");
    current.cells[0].text = alternate.cells[0].text = "0001";
    expect(screenCandidateCell([current],[alternate],0,1,1).cell).toBeUndefined();
    // A second identical table makes the textual anchors ambiguous across tables.
    expect(screenCandidateCell([table,table],[table,table],0,1,1).cell).toBeUndefined();
  });

  it("adopts only a registered complete image candidate with the current result guard and no draft", () => {
    const reviewed = {...item,candidates:[{id:"whole-image-candidate",engine:"glm",table_count:1}]};
    const body = screenCandidateReviewBody(reviewed,7,"whole-image-candidate",[],"request-candidate");
    expect(body.result_id).toBe("result-a");
    expect(body.expected_revision).toBe(7);
    expect(body.state).toBe("retain");
    expect(body.candidate_id).toBe("whole-image-candidate");
    expect(body.version_ids).toEqual(["version-a"]);
    expect(() => screenCandidateReviewBody(reviewed,7,"raw-crop-result",[],"request-candidate")).toThrow();
    expect(() => screenCandidateReviewBody(reviewed,7,"whole-image-candidate",[screenCellEdit(0,table.cells[2],"draft")],"request-candidate")).toThrow("先保存");
  });
});
