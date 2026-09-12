import type { Result, Table, Task } from "./types";

export type ComparisonTask = Task & {
  batch?: string;
  batch_id?: string;
  input_version_id?: string;
  preprocess?: string;
  engine_package?: string;
};
export type ComparisonGroup = {
  key: string;
  versionId: string;
  batch: string | null;
  results: Result[];
  created: string;
};

export function groupResults(
  results: Result[],
  tasks: ComparisonTask[],
): ComparisonGroup[] {
  const byTask = new Map(tasks.map((task) => [task.id, task]));
  const groups = new Map<string, ComparisonGroup>();
  for (const result of results) {
    const task = byTask.get(result.task_id);
    const versionId =
      result.original.project_image_version ?? task?.version_id ?? "未知版本";
    const batch = task?.batch_id ?? task?.batch ?? null;
    // Unknown batch metadata cannot establish comparable conditions.
    const key = JSON.stringify([versionId, batch ?? result.task_id]);
    const group = groups.get(key) ?? {
      key,
      versionId,
      batch,
      results: [],
      created: task?.created ?? "",
    };
    group.results.push(result);
    if ((task?.created ?? "") > group.created) group.created = task!.created;
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) =>
    b.created.localeCompare(a.created),
  );
}

export type DiffLine = {
  kind: "context" | "added" | "removed";
  text: string;
  before?: number;
  after?: number;
};

// Patience anchors retain alignment in large documents. Small changed spans use
// exact LCS, with bounded allocation for wholly unrelated multi-megabyte results.
export function alignedTextDiff(
  before: string,
  after: string,
): { lines: DiffLine[]; coarse: boolean } {
  const left = before.replace(/\r\n?/g, "\n").split("\n");
  const right = after.replace(/\r\n?/g, "\n").split("\n");
  const lines: DiffLine[] = [];
  let coarse = false;
  const same = (a: number, b: number) =>
    lines.push({ kind: "context", text: left[a], before: a + 1, after: b + 1 });
  const removed = (a: number) =>
    lines.push({ kind: "removed", text: left[a], before: a + 1 });
  const added = (b: number) =>
    lines.push({ kind: "added", text: right[b], after: b + 1 });
  const visit = (a0: number, a1: number, b0: number, b1: number, depth = 0) => {
    while (a0 < a1 && b0 < b1 && left[a0] === right[b0]) {
      same(a0++, b0++);
    }
    let tail = 0;
    while (
      a0 < a1 - tail &&
      b0 < b1 - tail &&
      left[a1 - tail - 1] === right[b1 - tail - 1]
    )
      tail++;
    a1 -= tail;
    b1 -= tail;
    const n = a1 - a0,
      m = b1 - b0;
    if (!n) {
      for (let b = b0; b < b1; b++) added(b);
    } else if (!m) {
      for (let a = a0; a < a1; a++) removed(a);
    } else if (n * m <= 250_000) {
      const width = m + 1;
      const lengths = new Uint32Array((n + 1) * width);
      for (let a = n - 1; a >= 0; a--)
        for (let b = m - 1; b >= 0; b--)
          lengths[a * width + b] =
            left[a0 + a] === right[b0 + b]
              ? lengths[(a + 1) * width + b + 1] + 1
              : Math.max(
                  lengths[(a + 1) * width + b],
                  lengths[a * width + b + 1],
                );
      let a = 0,
        b = 0;
      while (a < n || b < m) {
        if (a < n && b < m && left[a0 + a] === right[b0 + b]) {
          same(a0 + a++, b0 + b++);
        } else if (
          a < n &&
          (b === m ||
            lengths[(a + 1) * width + b] >= lengths[a * width + b + 1])
        )
          removed(a0 + a++);
        else added(b0 + b++);
      }
    } else {
      const counts = (values: string[], start: number, end: number) => {
        const map = new Map<string, number>();
        for (let i = start; i < end; i++)
          map.set(values[i], map.has(values[i]) ? -1 : i);
        return map;
      };
      const aa = counts(left, a0, a1),
        bb = counts(right, b0, b1);
      const pairs: [number, number][] = [];
      for (const [value, a] of aa) {
        const b = bb.get(value);
        if (a >= 0 && b !== undefined && b >= 0) pairs.push([a, b]);
      }
      const tails: number[] = [],
        previous: number[] = [];
      for (let i = 0; i < pairs.length; i++) {
        let lo = 0,
          hi = tails.length;
        while (lo < hi) {
          const mid = (lo + hi) >>> 1;
          if (pairs[tails[mid]][1] < pairs[i][1]) lo = mid + 1;
          else hi = mid;
        }
        previous[i] = lo ? tails[lo - 1] : -1;
        tails[lo] = i;
      }
      if (!tails.length || depth >= 64) {
        coarse = true;
        for (let a = a0; a < a1; a++) removed(a);
        for (let b = b0; b < b1; b++) added(b);
      } else {
        const anchors: [number, number][] = [];
        for (let i = tails[tails.length - 1]; i >= 0; i = previous[i])
          anchors.push(pairs[i]);
        for (const [a, b] of anchors.reverse()) {
          visit(a0, a, b0, b, depth + 1);
          same(a, b);
          a0 = a + 1;
          b0 = b + 1;
        }
        visit(a0, a1, b0, b1, depth + 1);
      }
    }
    for (let i = 0; i < tail; i++) same(a1 + i, b1 + i);
  };
  visit(0, left.length, 0, right.length);
  return { lines, coarse };
}

export type TableDifference = {
  table: number;
  kind: "table" | "shape" | "caption" | "cell" | "merge";
  location: string;
  before: string;
  after: string;
};
export function tableDifferences(
  before: Table[],
  after: Table[],
): TableDifference[] {
  const differences: TableDifference[] = [];
  for (let index = 0; index < Math.max(before.length, after.length); index++) {
    const a = before[index],
      b = after[index];
    const add = (
      kind: TableDifference["kind"],
      location: string,
      old: string,
      next: string,
    ) =>
      differences.push({
        table: index + 1,
        kind,
        location,
        before: old,
        after: next,
      });
    if (!a || !b) {
      add(
        "table",
        "整表",
        a ? `${a.rows} 行 × ${a.columns} 列` : "不存在",
        b ? `${b.rows} 行 × ${b.columns} 列` : "不存在",
      );
      continue;
    }
    if (a.rows !== b.rows || a.columns !== b.columns)
      add(
        "shape",
        "行列数量",
        `${a.rows} 行 × ${a.columns} 列`,
        `${b.rows} 行 × ${b.columns} 列`,
      );
    if ((a.caption ?? "") !== (b.caption ?? ""))
      add("caption", "标题", a.caption ?? "", b.caption ?? "");
    const key = (cell: Table["cells"][number]) => `${cell.row}:${cell.column}`;
    const oldCells = new Map(a.cells.map((cell) => [key(cell), cell]));
    const newCells = new Map(b.cells.map((cell) => [key(cell), cell]));
    for (const position of new Set([...oldCells.keys(), ...newCells.keys()])) {
      const old = oldCells.get(position),
        next = newCells.get(position),
        cell = old ?? next!;
      const location = `第 ${cell.row + 1} 行第 ${cell.column + 1} 列`;
      if (!old || !next || old.text !== next.text)
        add(
          "cell",
          location,
          old?.text ?? "（无单元格）",
          next?.text ?? "（无单元格）",
        );
      if (
        (old?.row_span ?? 1) !== (next?.row_span ?? 1) ||
        (old?.column_span ?? 1) !== (next?.column_span ?? 1)
      )
        add(
          "merge",
          location + " 合并范围",
          old ? `${old.row_span} 行 × ${old.column_span} 列` : "不存在",
          next ? `${next.row_span} 行 × ${next.column_span} 列` : "不存在",
        );
    }
  }
  return differences;
}
