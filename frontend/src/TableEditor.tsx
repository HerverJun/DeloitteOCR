import { useEffect, useRef, useState } from "react";
import { Button } from "@fluentui/react-components";
import {
  Plus,
  Minus,
  Merge,
  Split,
  Table2,
  ClipboardPaste,
  Trash2,
  AlertCircle,
  ArrowRight,
} from "lucide-react";
import type { Table } from "./types";
import {
  createTable,
  extendSelection,
  parseTsv,
  pasteTsv,
} from "./tableEditing";
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
  disabled = false,
  focusTarget,
  activeIndex = 0,
  onIndexChange,
  onCellFocus,
}: {
  tables: Table[];
  onChange: (tables: Table[]) => void;
  onError: (message: string) => void;
  disabled?: boolean;
  activeIndex?: number;
  onIndexChange?: (index: number) => void;
  onCellFocus?: (table: number, row: number, column: number) => void;
  focusTarget?: { tableId?: string; tableIndex?: number; row: number; column: number; nonce: number } | null;
}) {
  const [localIndex, setLocalIndex] = useState(activeIndex);
  const index = onIndexChange ? activeIndex : localIndex;
  const setIndex = (next: number) => {
    setLocalIndex(next);
    onIndexChange?.(next);
  };
  const [selection, setSelection] = useState([0, 0, 0, 0]);
  const [creating, setCreating] = useState(false);
  const [newRows, setNewRows] = useState(2);
  const [newColumns, setNewColumns] = useState(2);
  const [pasting, setPasting] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [confidenceThreshold, setConfidenceThreshold] = useState(0.8);
  const pointerSelection = useRef(false);
  const keyboardSelection = useRef(false);
  const editorElement = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (index >= tables.length && index !== 0) setIndex(Math.max(0, tables.length - 1));
  }, [tables.length]);
  useEffect(() => {
    if (!focusTarget) return;
    const next = focusTarget.tableIndex ?? tables.findIndex(t => t.fusion_id === focusTarget.tableId);
    if (next < 0) return;
    setIndex(next);
    setSelection([focusTarget.row, focusTarget.column, focusTarget.row, focusTarget.column]);
    const frame = requestAnimationFrame(() => {
      editorElement.current?.querySelector<HTMLTextAreaElement>(`[data-cell="${focusTarget.row}:${focusTarget.column}"]`)?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [focusTarget?.nonce]);
  const addTable = () => {
    try {
      if (tables.length >= 100) throw Error("单个结果最多支持 100 张表格，请先整理已有表格");
      onChange([...tables, createTable(newRows, newColumns)]);
      setIndex(tables.length);
      setSelection([0, 0, 0, 0]);
      setCreating(false);
    } catch (error) {
      onError(String(error));
    }
  };
  const newTableForm = (
    <form
      className="table-create-form"
      onSubmit={(event) => {
        event.preventDefault();
        addTable();
      }}
    >
      <label>
        行数{" "}
        <input
          aria-label="新表格行数"
          type="number"
          min={1}
          max={10000}
          required
          value={Number.isFinite(newRows) ? newRows : ""}
          onChange={(event) => setNewRows(event.target.valueAsNumber)}
        />
      </label>
      <label>
        列数{" "}
        <input
          aria-label="新表格列数"
          type="number"
          min={1}
          max={1000}
          required
          value={Number.isFinite(newColumns) ? newColumns : ""}
          onChange={(event) => setNewColumns(event.target.valueAsNumber)}
        />
      </label>
      <Button type="submit" size="small" disabled={disabled}>
        创建表格
      </Button>
    </form>
  );
  if (!tables.length)
    return (
      <div className="empty-panel" inert={disabled}>
        <Table2 size={32} />
        <h3>此结果没有结构化表格</h3>
        <p>可以手动创建表格，再粘贴 TSV 数据或填写单元格。</p>
        {newTableForm}
      </div>
    );
  const table = normalize(tables[Math.min(index, tables.length - 1)]);
  const anchors = new Map(
    table.cells.map((cell) => [`${cell.row}:${cell.column}`, cell]),
  );
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
  const focusCell = (r: number, c: number) => {
    const cell = cellAt(table, r, c);
    if (!cell) return;
    keyboardSelection.current = true;
    editorElement.current
      ?.querySelector<HTMLTextAreaElement>(
        `[data-cell="${cell.row}:${cell.column}"]`,
      )
      ?.focus();
    keyboardSelection.current = false;
  };
  const pasteRegion = (text: string) => {
    try {
      const values = parseTsv(text);
      update(pasteTsv(table, text, rect[0], rect[1]));
      setSelection([
        rect[0],
        rect[1],
        rect[0] + values.length - 1,
        rect[1] + values[0].length - 1,
      ]);
      setPasting(false);
      setPasteText("");
    } catch (error) {
      onError(String(error));
    }
  };
  const scoredCells = table.cells.filter(
    (cell) =>
      typeof cell.confidence === "number" && Number.isFinite(cell.confidence),
  );
  const suspectCells = scoredCells.filter(
    (cell) => cell.confidence! < confidenceThreshold,
  );
  const nextSuspect = () => {
    const cell =
      suspectCells.find(
        (cell) => cell.row > br || (cell.row === br && cell.column > bc),
      ) ?? suspectCells[0];
    if (cell) {
      setSelection([cell.row, cell.column, cell.row, cell.column]);
      focusCell(cell.row, cell.column);
    }
  };
  return (
    <div
      ref={editorElement}
      className="table-editor"
      inert={disabled}
      onKeyDownCapture={() => {
        pointerSelection.current = false;
      }}
    >
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
          {(rect[0] !== rect[2] || rect[1] !== rect[3]) &&
            `:${columnName(rect[3])}${rect[2] + 1}`}
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
      {creating && newTableForm}
      {pasting && (
        <form
          className="table-paste-form"
          onSubmit={(event) => {
            event.preventDefault();
            pasteRegion(pasteText);
          }}
        >
          <label>
            TSV 数据（从当前选区左上角填入）
            <textarea
              aria-label="TSV 区域数据"
              rows={4}
              value={pasteText}
              onChange={(event) => setPasteText(event.target.value)}
            />
          </label>
          <Button type="submit" size="small">
            应用粘贴
          </Button>
          <Button size="small" onClick={() => setPasting(false)}>
            取消
          </Button>
        </form>
      )}
      <details className="table-settings">
        <summary>表格设置与更多操作</summary>
        <div className="table-actions" style={{ flexWrap: "wrap" }}>
          <Button
            size="small"
            icon={<Plus size={14} />}
            onClick={() => setCreating(!creating)}
            aria-expanded={creating}
          >
            新建表格
          </Button>
          <Button
            size="small"
            icon={<Trash2 size={14} />}
            onClick={() => {
              onChange(tables.filter((_, i) => i !== index));
              setSelection([0, 0, 0, 0]);
            }}
          >
            删除整表
          </Button>
          <Button
            size="small"
            icon={<ClipboardPaste size={14} />}
            onClick={() => setPasting(!pasting)}
            aria-expanded={pasting}
          >
            粘贴区域
          </Button>
        </div>
        <label className="caption-input">
          表格标题
          <input
            aria-label="表格标题"
            value={table.caption || ""}
            onChange={(e) => update({ ...table, caption: e.target.value })}
          />
        </label>
        <p className="table-hint">
          Shift + 方向键或 Shift 点击扩展矩形选区。Ctrl + V
          粘贴含制表符的区域；单列数据可用「粘贴区域」。新建、删除和粘贴均可撤销，编号保留为文本。删除整表保留文字视图中的原文。
        </p>
        <div className="confidence-controls">
          <label>
            待核对阈值{" "}
            <input
              aria-label="表格置信度阈值"
              type="number"
              step="0.05"
              value={confidenceThreshold}
              onChange={(event) => {
                if (Number.isFinite(event.target.valueAsNumber))
                  setConfidenceThreshold(event.target.valueAsNumber);
              }}
            />
          </label>
          <span>
            分数未知 {table.cells.length - scoredCells.length}{" "}
            处。仅显示当前引擎提供的分数，不代表跨引擎统一准确率。
          </span>
        </div>
      </details>
      {suspectCells.length > 0 && (
        <div className="table-review-strip">
          <span>
            <AlertCircle size={14} />
            {suspectCells.length} 处低于待核对阈值
          </span>
          <Button
            size="small"
            appearance="subtle"
            icon={<ArrowRight size={14} />}
            aria-label={`下一处低于阈值（${suspectCells.length}）`}
            onClick={nextSuspect}
          >
            下一处
          </Button>
        </div>
      )}
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
                  const cell = anchors.get(`${r}:${c}`);
                  if (!cell) return null;
                  const selected =
                    r <= rect[2] &&
                    r + cell.row_span > rect[0] &&
                    c <= rect[3] &&
                    c + cell.column_span > rect[1];
                  const confidenceKnown =
                    typeof cell.confidence === "number" &&
                    Number.isFinite(cell.confidence);
                  const suspect =
                    confidenceKnown && cell.confidence! < confidenceThreshold;
                  return (
                    <td
                      key={c}
                      rowSpan={cell.row_span}
                      colSpan={cell.column_span}
                      className={`${selected ? "selected-cell" : ""} ${suspect ? "suspect-cell" : ""}`}
                      title={
                        confidenceKnown
                          ? `模型原始分数：${cell.confidence}`
                          : "模型分数未知"
                      }
                      onMouseDown={() => {
                        pointerSelection.current = true;
                      }}
                      onMouseUp={() => {
                        pointerSelection.current = false;
                      }}
                      onClick={(e) => pick(r, c, e.shiftKey)}
                    >
                      <textarea
                        data-cell={`${r}:${c}`}
                        aria-label={`第 ${r + 1} 行第 ${c + 1} 列`}
                        aria-description={`${selected ? "已选择。" : ""}${suspect ? "低于待核对阈值。" : ""}${confidenceKnown ? `模型原始分数 ${cell.confidence}` : "模型分数未知"}`}
                        spellCheck={false}
                        onFocus={() => {
                          onCellFocus?.(index, cell.row, cell.column);
                          // Mouse clicks apply their own Shift anchor after focus.
                          if (
                            !pointerSelection.current &&
                            !keyboardSelection.current
                          )
                            pick(r, c, false);
                          pointerSelection.current = false;
                        }}
                        onKeyDown={(event) => {
                          if (event.shiftKey && event.key.startsWith("Arrow")) {
                            event.preventDefault();
                            const next = extendSelection(
                              bounded,
                              event.key,
                              table.rows,
                              table.columns,
                            );
                            setSelection(next);
                            focusCell(next[2], next[3]);
                          } else if (event.key === "Escape") pick(r, c, false);
                        }}
                        onPaste={(event) => {
                          const text =
                            event.clipboardData.getData("text/plain");
                          if (text.includes("\t")) {
                            event.preventDefault();
                            pasteRegion(text);
                          }
                        }}
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
