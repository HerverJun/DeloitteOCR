import type { Cell, Table } from "./types";
import { normalize } from "./tableOps";

const MAX_CELLS = 50_000;

export function createTable(rows: number, columns: number): Table {
  if (
    !Number.isInteger(rows) ||
    !Number.isInteger(columns) ||
    rows < 1 ||
    columns < 1
  )
    throw Error("行数和列数必须是正整数");
  if (rows * columns > MAX_CELLS)
    throw Error("单个表格最多支持 50,000 个单元格，请缩小区域");
  return normalize({ rows, columns, cells: [] });
}

// Spreadsheet clipboard TSV may quote cells containing tabs, quotes or newlines.
// All values stay strings, including leading zeros, formulas and long identifiers.
export function parseTsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let value = "";
  let quoted = false;
  let closedQuote = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (quoted) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          value += '"';
          i++;
        } else {
          quoted = false;
          closedQuote = true;
        }
      } else value += char;
      continue;
    }
    if (char === '"' && value === "" && !closedQuote) {
      quoted = true;
    } else if (char === "\t") {
      row.push(value);
      value = "";
      closedQuote = false;
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[i + 1] === "\n") i++;
      row.push(value);
      rows.push(row);
      row = [];
      value = "";
      closedQuote = false;
    } else {
      if (closedQuote) throw Error("TSV 引号后应为制表符或换行");
      value += char;
    }
  }
  if (quoted) throw Error("TSV 存在未闭合的引号，请检查粘贴内容");
  if (row.length || value || closedQuote || !rows.length) {
    row.push(value);
    rows.push(row);
  }
  const columns = rows.reduce((maximum, r) => Math.max(maximum, r.length), 0);
  if (rows.length * columns > MAX_CELLS)
    throw Error("粘贴区域超过 50,000 个单元格");
  return rows.map((r) => [...r, ...Array<string>(columns - r.length).fill("")]);
}

export function pasteTsv(
  input: Table,
  text: string,
  row: number,
  column: number,
): Table {
  if (
    !Number.isInteger(row) ||
    !Number.isInteger(column) ||
    row < 0 ||
    column < 0
  )
    throw Error("请选择有效的粘贴起点");
  const values = parseTsv(text);
  const endRow = row + values.length;
  const endColumn = column + values[0].length;
  const rows = Math.max(input.rows, endRow);
  const columns = Math.max(input.columns, endColumn);
  if (rows * columns > MAX_CELLS)
    throw Error("粘贴后的表格超过 50,000 个单元格");
  const overlaps = (cell: Cell) =>
    cell.row < endRow &&
    cell.row + cell.row_span > row &&
    cell.column < endColumn &&
    cell.column + cell.column_span > column;
  if (
    input.cells.some(
      (cell) =>
        overlaps(cell) &&
        (cell.row < row ||
          cell.column < column ||
          cell.row + cell.row_span > endRow ||
          cell.column + cell.column_span > endColumn),
    )
  )
    throw Error("粘贴区域仅覆盖了部分合并单元格，请先拆分或完整覆盖该单元格");
  const cells = input.cells
    .filter((cell) => !overlaps(cell))
    .map((cell) => ({ ...cell }));
  values.forEach((line, r) =>
    line.forEach((text, c) =>
      cells.push({
        row: row + r,
        column: column + c,
        row_span: 1,
        column_span: 1,
        text,
        confidence: null,
        polygon: null,
      }),
    ),
  );
  return normalize({ ...input, rows, columns, cells });
}

export function extendSelection(
  selection: number[],
  key: string,
  rows: number,
  columns: number,
): number[] {
  const [ar, ac, br, bc] = selection;
  const delta: Record<string, [number, number]> = {
    ArrowUp: [-1, 0],
    ArrowDown: [1, 0],
    ArrowLeft: [0, -1],
    ArrowRight: [0, 1],
  };
  const step = delta[key];
  if (!step) return selection;
  return [
    ar,
    ac,
    Math.max(0, Math.min(rows - 1, br + step[0])),
    Math.max(0, Math.min(columns - 1, bc + step[1])),
  ];
}
