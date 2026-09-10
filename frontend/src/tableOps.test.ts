import { describe, it, expect } from "vitest";
import {
  normalize,
  merge,
  split,
  insertAxis,
  deleteAxis,
  cellAt,
} from "./tableOps";
import type { Table } from "./types";
const base = (): Table =>
  normalize({
    rows: 3,
    columns: 3,
    cells: [
      { row: 0, column: 0, row_span: 1, column_span: 3, text: "标题" },
      {
        row: 1,
        column: 0,
        row_span: 1,
        column_span: 1,
        text: "00123456789012345678",
      },
    ],
  });
const valid = (t: Table) => {
  const seen = new Set();
  for (const cell of t.cells)
    for (let r = cell.row; r < cell.row + cell.row_span; r++)
      for (let c = cell.column; c < cell.column + cell.column_span; c++) {
        expect(r).toBeLessThan(t.rows);
        expect(c).toBeLessThan(t.columns);
        expect(seen.has(`${r},${c}`)).toBe(false);
        seen.add(`${r},${c}`);
      }
  expect(seen.size).toBe(t.rows * t.columns);
};
describe("editable table structure", () => {
  it("preserves long identifiers through merge and split", () => {
    const a = base();
    const b = merge(a, [1, 0, 2, 1]);
    valid(b);
    expect(cellAt(b, 1, 0)?.text).toBe("00123456789012345678");
    const c = split(b, 1, 0);
    valid(c);
    expect(cellAt(c, 1, 0)?.text).toBe("00123456789012345678");
    expect(a.rows).toBe(3);
  });
  it("rejects merges crossing existing spans", () => {
    expect(() => merge(base(), [0, 1, 1, 2])).toThrow();
  });
  it("extends crossing spans and keeps every cell covered on insert/delete", () => {
    for (const axis of ["row", "column"] as const) {
      const a = insertAxis(base(), axis, 1);
      valid(a);
      const b = deleteAxis(a, axis, 1);
      valid(b);
      expect(b).toEqual(base());
    }
  });
  it("retains merged anchor text when deleting its first row or column", () => {
    const a = merge(base(), [1, 0, 2, 1]);
    const b = deleteAxis(a, "row", 1);
    valid(b);
    expect(cellAt(b, 1, 0)?.text).toBe("00123456789012345678");
    const c = deleteAxis(base(), "column", 0);
    valid(c);
    expect(cellAt(c, 0, 0)?.text).toBe("标题");
  });
  it("never deletes the last row or column", () => {
    const a = normalize({ rows: 1, columns: 1, cells: [] });
    expect(() => deleteAxis(a, "row", 0)).toThrow();
    expect(() => deleteAxis(a, "column", 0)).toThrow();
  });
});
