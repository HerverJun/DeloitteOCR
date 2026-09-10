import type { Table, Cell } from "./types";
const empty = (r: number, c: number): Cell => ({
  row: r,
  column: c,
  row_span: 1,
  column_span: 1,
  text: "",
  confidence: null,
  polygon: null,
});
export function normalize(input: Table): Table {
  const table = structuredClone(input);
  const occupied = new Set<string>();
  for (const cell of table.cells)
    for (let r = cell.row; r < cell.row + cell.row_span; r++)
      for (let c = cell.column; c < cell.column + cell.column_span; c++)
        occupied.add(`${r}:${c}`);
  for (let r = 0; r < table.rows; r++)
    for (let c = 0; c < table.columns; c++)
      if (!occupied.has(`${r}:${c}`)) table.cells.push(empty(r, c));
  table.cells.sort((a, b) => a.row - b.row || a.column - b.column);
  return table;
}
export function cellAt(table: Table, r: number, c: number) {
  return table.cells.find(
    (x) =>
      r >= x.row &&
      r < x.row + x.row_span &&
      c >= x.column &&
      c < x.column + x.column_span,
  );
}
export function merge(input: Table, rect: number[]): Table {
  const [r1, c1, r2, c2] = rect;
  const table = normalize(input);
  if (r1 < 0 || c1 < 0 || r2 >= table.rows || c2 >= table.columns)
    throw Error("选择区域超出表格");
  const selected = table.cells.filter(
    (x) =>
      x.row <= r2 &&
      x.row + x.row_span > r1 &&
      x.column <= c2 &&
      x.column + x.column_span > c1,
  );
  if (
    selected.some(
      (x) =>
        x.row < r1 ||
        x.column < c1 ||
        x.row + x.row_span - 1 > r2 ||
        x.column + x.column_span - 1 > c2,
    )
  )
    throw Error("请完整选择已有的合并单元格");
  const text = selected
    .map((x) => x.text)
    .filter(Boolean)
    .join("\n");
  table.cells = table.cells.filter((x) => !selected.includes(x));
  table.cells.push({
    ...empty(r1, c1),
    row_span: r2 - r1 + 1,
    column_span: c2 - c1 + 1,
    text,
  });
  return normalize(table);
}
export function split(input: Table, r: number, c: number): Table {
  const table = structuredClone(input);
  const cell = cellAt(table, r, c);
  if (!cell) return table;
  cell.row_span = 1;
  cell.column_span = 1;
  return normalize(table);
}
export function insertAxis(
  input: Table,
  axis: "row" | "column",
  position: number,
): Table {
  const table = structuredClone(input);
  const size = axis === "row" ? "rows" : "columns";
  const span = axis === "row" ? "row_span" : "column_span";
  if (position < 0 || position > table[size]) throw Error("无效插入位置");
  for (const cell of table.cells) {
    if (cell[axis] >= position) cell[axis]++;
    else if (cell[axis] + cell[span] > position) cell[span]++;
  }
  table[size]++;
  return normalize(table);
}
export function deleteAxis(
  input: Table,
  axis: "row" | "column",
  position: number,
): Table {
  const table = structuredClone(input);
  const size = axis === "row" ? "rows" : "columns";
  const span = axis === "row" ? "row_span" : "column_span";
  if (table[size] <= 1) throw Error("至少保留一行和一列");
  if (position < 0 || position >= table[size]) throw Error("请选择有效行列");
  table.cells = table.cells.filter((cell) => {
    if (cell[axis] > position) cell[axis]--;
    else if (cell[axis] <= position && cell[axis] + cell[span] > position) {
      if (cell[span] === 1) return false;
      cell[span]--;
    }
    return true;
  });
  table[size]--;
  return normalize(table);
}
