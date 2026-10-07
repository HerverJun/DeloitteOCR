import { describe, expect, it } from "vitest";
import { screenCell, screenCellEdit, screenEditKey, screenProgress, screenReviewBody, selectedScreenVersions, type ScreenItem } from "./screenBatchWorkflow";
import type { Photo, Table } from "./types";

const item: ScreenItem = { input_version_id: "version-a", image_id: "image-a", source_name: "财务.png", status: "needs_review", stage: "review",
  result_id: "result-a", result_revision: 7, review_state: "automatic", human_confirmed: false, issues: [] };
const table: Table = { rows: 2, columns: 2, cells: [
  { row: 0, column: 0, row_span: 1, column_span: 2, text: "明细" },
  { row: 1, column: 0, row_span: 1, column_span: 1, text: "000123456789012345678" },
  { row: 1, column: 1, row_span: 1, column_span: 1, text: "-10.05" },
] };

describe("screen batch workflow", () => {
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
});
