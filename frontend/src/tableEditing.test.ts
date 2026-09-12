import { describe, expect, it } from "vitest";
import {
  createTable,
  extendSelection,
  parseTsv,
  pasteTsv,
} from "./tableEditing";
import { cellAt, merge } from "./tableOps";

describe("manual table repair", () => {
  it("pastes quoted spreadsheet cells without changing identifiers or formulas", () => {
    const input =
      '001234567890123456789\t"two\tparts"\t"line 1\nline 2"\r\n=SUM(A1:A2)\t"quote ""inside"""\t\r\n';
    expect(parseTsv(input)).toEqual([
      ["001234567890123456789", "two\tparts", "line 1\nline 2"],
      ["=SUM(A1:A2)", 'quote "inside"', ""],
    ]);
    const table = pasteTsv(createTable(1, 1), input, 1, 1);
    expect([table.rows, table.columns]).toEqual([3, 4]);
    expect(cellAt(table, 1, 1)?.text).toBe("001234567890123456789");
    expect(cellAt(table, 2, 1)?.text).toBe("=SUM(A1:A2)");
  });

  it("applies a rectangular paste atomically while retaining outside cells and the undo source", () => {
    const original = createTable(3, 3);
    original.cells[0].text = "keep me";
    const result = pasteTsv(original, "01\t02\n03", 1, 1);
    expect(cellAt(result, 0, 0)?.text).toBe("keep me");
    expect(cellAt(result, 2, 2)?.text).toBe("");
    expect(cellAt(original, 1, 1)?.text).toBe("");
    expect(result.cells).toHaveLength(9);
  });

  it("rejects partial merged-cell overwrites, and splits a completely covered merge", () => {
    const original = merge(createTable(2, 3), [0, 0, 1, 1]);
    expect(() => pasteTsv(original, "first\tsecond", 0, 0)).toThrow("部分合并");
    expect(original.cells[0].row_span).toBe(2);
    const result = pasteTsv(original, "01\t02\n03\t04", 0, 0);
    expect(result.cells).toHaveLength(6);
    expect(cellAt(result, 1, 1)?.text).toBe("04");
    expect(
      result.cells.every(
        (cell) => cell.row_span === 1 && cell.column_span === 1,
      ),
    ).toBe(true);
  });

  it("rejects malformed clipboard quoting and oversized tables without altering a source", () => {
    expect(() => parseTsv('"incomplete\tcell')).toThrow("未闭合");
    expect(() => parseTsv('"closed"tail')).toThrow("引号后");
    expect(() => createTable(0, 2)).toThrow();
    expect(() => createTable(50_000, 2)).toThrow();
    expect(() => pasteTsv(createTable(1, 1), "x", 50_000, 0)).toThrow();
  });

  it("extends B2 to C3 by keyboard and contracts with the same fixed anchor", () => {
    const right = extendSelection([1, 1, 1, 1], "ArrowRight", 3, 3);
    const down = extendSelection(right, "ArrowDown", 3, 3);
    expect(down).toEqual([1, 1, 2, 2]);
    expect(extendSelection(down, "ArrowDown", 3, 3)).toEqual(down);
    expect(extendSelection(down, "ArrowLeft", 3, 3)).toEqual([1, 1, 2, 1]);
  });
});
