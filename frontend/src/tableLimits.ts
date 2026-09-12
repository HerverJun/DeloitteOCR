// The server permits 10,000 rows, 1,000 columns and 100,000 cells per table.
// Interactive creation/paste has a smaller 50,000-cell budget.
export function validateTableSize(rows: number, columns: number, maxCells = 100_000) {
  if (!Number.isInteger(rows) || !Number.isInteger(columns) || rows < 1 || columns < 1)
    throw Error("行数和列数必须是正整数");
  if (rows > 10_000 || columns > 1_000)
    throw Error("单个表格最多支持 10,000 行、1,000 列，请缩小区域");
  if (rows * columns > maxCells)
    throw Error(`单个表格最多支持 ${maxCells.toLocaleString("en-US")} 个单元格，请缩小区域`);
}
