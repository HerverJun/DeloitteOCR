import { useEffect, useState } from "react";
import { Button } from "@fluentui/react-components";
import { Plus, Minus, Merge, Split, Table2 } from "lucide-react";
import type { Table } from "./types";
import {
  normalize,
  merge,
  split,
  insertAxis,
  deleteAxis,
  cellAt,
} from "./tableOps";

export function TableEditor({
  tables,
  onChange,
  onError,
}: {
  tables: Table[];
  onChange: (tables: Table[]) => void;
  onError: (message: string) => void;
}) {
  const [index, setIndex] = useState(0);
  const [selection, setSelection] = useState([0, 0, 0, 0]);
  useEffect(() => {
    setIndex((i) => Math.min(i, Math.max(0, tables.length - 1)));
  }, [tables.length]);
  if (!tables.length)
    return (
      <div className="empty-panel">
        <Table2 size={32} />
        <h3>此结果没有结构化表格</h3>
        <p>使用 PaddleOCR-VL、GLM-OCR 或 HunyuanOCR 识别表格照片。</p>
      </div>
    );
  const table = normalize(tables[Math.min(index, tables.length - 1)]);
  const bounded = selection.map((v, i) =>
    Math.min(v, i % 2 ? table.columns - 1 : table.rows - 1),
  );
  const update = (value: Table) =>
    onChange(tables.map((old, i) => (i === index ? value : old)));
  const action = (fn: () => Table) => {
    try {
      update(fn());
    } catch (error) {
      onError(String(error));
    }
  };
  const [ar, ac, br, bc] = bounded;
  const rect = [
    Math.min(ar, br),
    Math.min(ac, bc),
    Math.max(ar, br),
    Math.max(ac, bc),
  ];
  const columnName = (index: number) => {
    let text = "";
    for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26))
      text = String.fromCharCode(65 + ((n - 1) % 26)) + text;
    return text;
  };
  const pick = (r: number, c: number, shift: boolean) =>
    setSelection(shift ? [ar, ac, r, c] : [r, c, r, c]);
  return (
    <div className="table-editor">
      <div className="table-command-row">
        <div className="table-selector">
          <label>
            <select
              aria-label="选择表格"
              value={index}
              onChange={(e) => {
                setIndex(+e.target.value);
                setSelection([0, 0, 0, 0]);
              }}
            >
              {tables.map((_, i) => (
                <option key={i} value={i}>
                  表 {i + 1}
                </option>
              ))}
            </select>
          </label>
          <span>
            {table.rows} 行 × {table.columns} 列
          </span>
        </div>
        <span className="cell-address" aria-label="当前单元格">
          {columnName(rect[1])}
          {rect[0] + 1}
        </span>
        <div className="table-actions">
          <Button
            size="small"
            icon={<Plus size={14} />}
            onClick={() =>
              action(() =>
                insertAxis(table, "row", Math.min(ar + 1, table.rows)),
              )
            }
          >
            加行
          </Button>
          <Button
            size="small"
            icon={<Plus size={14} />}
            onClick={() =>
              action(() =>
                insertAxis(table, "column", Math.min(ac + 1, table.columns)),
              )
            }
          >
            加列
          </Button>
          <Button
            size="small"
            icon={<Minus size={14} />}
            onClick={() => action(() => deleteAxis(table, "row", ar))}
          >
            删行
          </Button>
          <Button
            size="small"
            icon={<Minus size={14} />}
            onClick={() => action(() => deleteAxis(table, "column", ac))}
          >
            删列
          </Button>
          <Button
            size="small"
            icon={<Merge size={14} />}
            onClick={() => action(() => merge(table, rect))}
          >
            合并
          </Button>
          <Button
            size="small"
            icon={<Split size={14} />}
            onClick={() => action(() => split(table, ar, ac))}
          >
            拆分
          </Button>
        </div>
      </div>
      <details className="table-settings">
        <summary>表格标题与操作说明</summary>
        <label className="caption-input">
          表格标题
          <input
            aria-label="表格标题"
            value={table.caption || ""}
            onChange={(e) => update({ ...table, caption: e.target.value })}
          />
        </label>
        <p className="table-hint">
          按住 Shift 点击选择矩形区域。合并保留文字；编号始终按文本导出。
        </p>
      </details>
      <div className="table-scroll">
        <table className="editable-grid">
          <thead>
            <tr>
              <th className="row-header"></th>
              {Array.from({ length: table.columns }, (_, c) => (
                <th
                  key={c}
                  scope="col"
                  tabIndex={0}
                  aria-label={`选择第 ${c + 1} 列`}
                  onKeyDown={(e) => {
                    if (["Enter", " "].includes(e.key)) {
                      e.preventDefault();
                      setSelection([0, c, table.rows - 1, c]);
                    }
                  }}
                  onClick={() => setSelection([0, c, table.rows - 1, c])}
                >
                  {columnName(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: table.rows }, (_, r) => (
              <tr key={r}>
                <th
                  className="row-header"
                  scope="row"
                  tabIndex={0}
                  aria-label={`选择第 ${r + 1} 行`}
                  onKeyDown={(e) => {
                    if (["Enter", " "].includes(e.key)) {
                      e.preventDefault();
                      setSelection([r, 0, r, table.columns - 1]);
                    }
                  }}
                  onClick={() => setSelection([r, 0, r, table.columns - 1])}
                >
                  {r + 1}
                </th>
                {Array.from({ length: table.columns }, (_, c) => {
                  const cell = cellAt(table, r, c);
                  if (!cell || cell.row !== r || cell.column !== c) return null;
                  const selected =
                    r >= rect[0] &&
                    r <= rect[2] &&
                    c >= rect[1] &&
                    c <= rect[3];
                  return (
                    <td
                      key={c}
                      rowSpan={cell.row_span}
                      colSpan={cell.column_span}
                      className={selected ? "selected-cell" : ""}
                      onClick={(e) => pick(r, c, e.shiftKey)}
                    >
                      <textarea
                        aria-label={`第 ${r + 1} 行第 ${c + 1} 列`}
                        spellCheck={false}
                        wrap="off"
                        rows={Math.min(
                          4,
                          Math.max(1, cell.text.split("\n").length),
                        )}
                        style={
                          /^\d{12,}$/.test(cell.text.trim())
                            ? {
                                minWidth: Math.min(
                                  360,
                                  cell.text.trim().length * 8.5 + 24,
                                ),
                              }
                            : undefined
                        }
                        value={cell.text}
                        onChange={(e) =>
                          update({
                            ...table,
                            cells: table.cells.map((x) =>
                              x === cell ? { ...x, text: e.target.value } : x,
                            ),
                          })
                        }
                      />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
