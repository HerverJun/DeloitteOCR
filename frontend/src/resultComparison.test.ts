import { describe, expect, it } from "vitest";
import type { Result } from "./types";
import {
  alignedTextDiff,
  groupResults,
  tableDifferences,
  type ComparisonTask,
} from "./comparisonOps";
import { createTable, pasteTsv } from "./tableEditing";
import { merge } from "./tableOps";

describe("comparable recognition results", () => {
  const result = (id: string, version: string): Result => ({
    id,
    task_id: `task-${id}`,
    revision: 0,
    cursor: 0,
    can_undo: false,
    can_redo: false,
    edited: { text: "", tables: [] },
    original: {
      engine: "ppocr",
      text: "",
      tables: [],
      blocks: [],
      elapsed_seconds: 1,
      load_seconds: 0,
      project_image_version: version,
      image: { width: 100, height: 100 },
    },
  });
  const task = (
    id: string,
    version: string,
    batch?: string,
  ): ComparisonTask => ({
    id: `task-${id}`,
    image_id: "photo",
    version_id: version,
    engine: "ppocr",
    status: "succeeded",
    phase: "done",
    error: null,
    result_id: id,
    created: "2026-09-12T12:00:00",
    batch,
  });
  it("groups only matching actual image versions and batches", () => {
    const results = [
      result("a", "v1"),
      result("b", "v1"),
      result("c", "v2"),
      result("d", "v1"),
    ];
    const tasks = [
      task("a", "v1", "one"),
      task("b", "v1", "one"),
      task("c", "v2", "one"),
      task("d", "v1", "two"),
    ];
    expect(
      groupResults(results, tasks).map((group) =>
        group.results.map((value) => value.id),
      ),
    ).toEqual([["a", "b"], ["c"], ["d"]]);
    expect(
      groupResults(
        [result("a", "v1"), result("b", "v1")],
        [task("a", "v1"), task("b", "v1")],
      ),
    ).toHaveLength(2);
  });

  it("aligns inserted and deleted lines, including repeated text", () => {
    const { lines, coarse } = alignedTextDiff("A\nB\nC", "A\n新增\nB\nC");
    expect(coarse).toBe(false);
    expect(lines.filter((line) => line.kind !== "context")).toEqual([
      { kind: "added", text: "新增", after: 2 },
    ]);
    const duplicate = alignedTextDiff("A\nB\nA\nC", "A\nA\nC");
    expect(duplicate.lines.filter((line) => line.kind !== "context")).toEqual([
      { kind: "removed", text: "B", before: 2 },
    ]);
  });

  it("preserves both complete documents through alignment on large sparse changes", () => {
    const before = Array.from({ length: 2000 }, (_, i) => `line-${i}`).join(
      "\n",
    );
    const after = before
      .replace("line-10\n", "new-first\nline-10\n")
      .replace("line-1990\n", "new-last\nline-1990\n");
    const diff = alignedTextDiff(before, after);
    expect(diff.coarse).toBe(false);
    expect(diff.lines.filter((line) => line.kind !== "context")).toHaveLength(
      2,
    );
    expect(
      diff.lines
        .filter((line) => line.kind !== "added")
        .map((line) => line.text)
        .join("\n"),
    ).toBe(before);
    expect(
      diff.lines
        .filter((line) => line.kind !== "removed")
        .map((line) => line.text)
        .join("\n"),
    ).toBe(after);
  });

  it("reports table additions, cell text, dimensions and merged spans separately", () => {
    const before = createTable(2, 2);
    const changed = pasteTsv(before, "001\t002", 0, 0);
    const candidate = merge({ ...changed, rows: 3 }, [0, 0, 0, 1]);
    const diff = tableDifferences([before], [candidate, createTable(1, 1)]);
    expect(new Set(diff.map((value) => value.kind))).toEqual(
      new Set(["shape", "cell", "merge", "table"]),
    );
    expect(
      diff.some(
        (value) => value.kind === "merge" && value.after === "1 行 × 2 列",
      ),
    ).toBe(true);
  });
});
