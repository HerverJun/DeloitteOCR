import type { Cell, Photo, Table } from "./types";

export type ScreenIssue = {
  code: string; message?: string; severity?: string; table?: number; row?: number; column?: number;
  value?: string; alternative?: string; visible_text?: string; box?: number[]; polygon?: number[][];
  candidate_id?: string; resolution?: string; [key: string]: unknown;
};
export type ScreenCounts = { total: number; completed: number; needs_review: number; failed: number; cancelled: number; pending: number };
export type ScreenItem = {
  input_version_id: string; image_id: string; source_name: string; status: string; stage: string;
  result_id?: string | null; result_revision?: number | null; review_state: string; human_confirmed: boolean;
  current_result_revision?: number | null; review_stale?: boolean;
  error?: string | null; issues: ScreenIssue[];
  candidates?: { id: string; engine: string; table_count: number }[];
};
export type ScreenBatch = {
  batch_id: string; project_id: string; status: string; counts: ScreenCounts; items: ScreenItem[];
  total_items: number; next_offset: number | null; created: string; updated: string; worker_error?: string | null;
  eta?: ScreenEta;
  delivery_snapshot_id?: string; delivery_fresh?: boolean; stale_item_count?: number;
};
export type ScreenDiagnosticAction = { action: string; label: string; enabled: boolean; reason?: string | null; budget_seconds?: number };
export type ScreenDiagnostic = { id: string; code: string; severity: string; reason: string; location: { level: "cell" | "table" | "image"; table?: number | null; row?: number; column?: number; box?: number[] | null; basis?: string; range_semantics?: string }; evidence: Record<string, unknown>[]; applicable_actions: ScreenDiagnosticAction[] };
export type ScreenDiagnostics = { items: ScreenDiagnostic[]; summary: { issues: number; groups: number; cell: number; table: number; image: number }; budget: { remaining_seconds: number | null }; result_id?: string; result_revision?: number; input_version_id?: string };
export type ScreenComparison = { result_id: string; result_revision: number; input_version_id: string; current_id: string; alternative_id: string; recommended_candidate_id: string; diagnostics: ScreenDiagnostics; conflicts: { code: string; value?: string; alternative?: string; message?: string }[]; selection_applied: false; values_modified: false };

export function screenDiagnosticLocation(item?: ScreenDiagnostic) {
  const loc = item?.location, box = loc?.box;
  const validBox = !!box && box.length === 4 && box.every(Number.isFinite) && box[0] >= 0 && box[1] >= 0 && box[2] > box[0] && box[3] > box[1];
  const cell = loc?.level === "cell" && loc.basis === "observed_full_cell_geometry" && loc.range_semantics === "full_cell"
    && [loc.table, loc.row, loc.column].every(value => Number.isInteger(value) && Number(value) >= 0);
  const table = loc?.level === "table" && loc.basis === "observed_table_region";
  if (validBox && (cell || table)) return { level: cell ? "cell" : "table", polygon: [[box![0], box![1]], [box![2], box![1]], [box![2], box![3]], [box![0], box![3]]], reason: cell ? `表 ${Number(loc!.table) + 1}，行 ${Number(loc!.row) + 1}，列 ${Number(loc!.column) + 1}（可靠单格）` : "可靠业务表区，未定位到单格" };
  return { level: "image", polygon: null, reason: loc?.level === "table" ? "表区疑点无可靠单格定位，请核对原图" : "无可靠单格定位，显示全图" };
}

export function screenResultRevision(item?: Pick<ScreenItem, "current_result_revision" | "result_revision"> & { result?: { revision: number } | null }) {
  const revision = item?.result?.revision ?? item?.current_result_revision ?? item?.result_revision;
  return Number.isInteger(revision) && Number(revision) >= 0 ? Number(revision) : undefined;
}

export function screenComparisonBody(item: ScreenItem & { result?: { revision: number } | null }, candidateId: string, revision: number) {
  if (!item.result_id || revision !== screenResultRevision(item) || !Number.isInteger(revision) || !item.candidates?.some(candidate => candidate.id === candidateId)) throw Error("比较对应的图片、候选或结果版本已变化，请刷新后再试。");
  return { result_id: item.result_id, expected_revision: revision, candidate_id: candidateId };
}
export type ScreenEta = {
  remaining_seconds: number | null; stage: { kind: string; engine: string; pass_index: number; label: string } | null;
  sample_count: number; pending_tasks: number; cached_pending_tasks: number; reason: string;
};
export type ScreenCellEdit = { table_index: number; row: number; column: number; before: string; after: string };
export type ScreenCandidate = {
  id: string; engine: string;
  raw: { tables: Table[]; project_image_version?: string; elapsed_seconds?: number;
    warnings?: { code: string; message?: string }[]; truncated?: boolean;
    screen?: { sources?: { prepared_id?: string; engine?: string; revision?: number }[] } };
};

export const screenStatuses: Record<string, string> = {
  queued: "等待处理", prepared: "表区处理完成", running: "处理中", awaiting_upgrade: "提高识别精度",
  completed: "可交付", needs_review: "待核对", failed: "失败", cancelled: "已取消", paused: "已暂停",
  succeeded: "已完成", partial: "处理结束，部分需核对",
};

export function screenEtaText(eta: ScreenEta) {
  if (eta.remaining_seconds !== null && Number.isFinite(eta.remaining_seconds) && eta.remaining_seconds >= 0) {
    const duration = eta.remaining_seconds < 60 ? `${Math.ceil(eta.remaining_seconds)} 秒` : `${Math.ceil(eta.remaining_seconds / 60)} 分钟`;
    return `已排队的${eta.stage?.label || "当前阶段"}预计还需约 ${duration}。`;
  }
  if (eta.reason === "paused") return "已暂停，继续后再估计当前阶段剩余时间。";
  if (eta.reason === "active_task_exceeds_observed_duration") return "当前识别耗时超出本批已完成样本，暂无法估计剩余时间。";
  if (eta.reason === "no_queued_compute") return "正在准备或汇总当前阶段，暂无法估计剩余时间。";
  return "正在积累本批识别耗时，暂无法估计剩余时间。";
}

export function selectedScreenVersions(images: Photo[], selectedIds: string[], scope: "selected" | "project") {
  const ids = new Set(selectedIds);
  return [...new Set(images.filter(image => scope === "project" || ids.has(image.id)).map(image => image.active_version).filter(Boolean))];
}

export function screenCell(table: Table | undefined, row: number, column: number): Cell | undefined {
  return table?.cells.find(cell => row >= cell.row && row < cell.row + cell.row_span
    && column >= cell.column && column < cell.column + cell.column_span);
}

export function screenCellEdit(tableIndex: number, cell: Cell, after: string): ScreenCellEdit {
  return { table_index: tableIndex, row: cell.row, column: cell.column, before: cell.text, after };
}

export function screenEditKey(edit: Pick<ScreenCellEdit, "table_index" | "row" | "column">) {
  return `${edit.table_index}:${edit.row}:${edit.column}`;
}

export function screenReviewBody(item: ScreenItem, revision: number, state: "accepted" | "retain" | "deferred", edits: ScreenCellEdit[], requestId: string) {
  if (!item.result_id || !Number.isInteger(revision) || revision < 0) throw Error("当前结果不可复核，请重新加载。");
  if (edits.length > 100) throw Error("请分次保存，每次最多修改 100 个单元格。");
  return { action: "review", request_id: requestId, version_ids: [item.input_version_id], result_id: item.result_id,
    expected_revision: revision, state, edits: state === "deferred" ? [] : edits.filter(edit => edit.before !== edit.after) };
}

export function screenIssueTitle(issue: ScreenIssue) {
  if (issue.message) return issue.message;
  return ({ candidate_value_conflict: "识别结果出现转录分歧", candidate_structure_conflict: "行列或合并结构有分歧",
    table_count_conflict: "识别出的表格数量不同", unmapped_candidate_difference: "转录不同，单格定位不足",
    upgrade_budget_reached: "已达到本图处理预算，保留最佳结果待核对", no_table: "没有可交付的业务表格" } as Record<string,string>)[issue.code] || "需要核对原图与识别结果";
}

export function screenProgress(counts: ScreenCounts) {
  const terminal = counts.completed + counts.needs_review + counts.failed + counts.cancelled;
  return { terminal, percent: counts.total ? Math.min(100, Math.round(terminal / counts.total * 100)) : 0 };
}

export function screenNumericColumnOptions(table: Table) {
  const idHeader = /编号|账号|账户|代码|流水|证件|序号|identifier|\b(id|code|account|reference)\b/i;
  return Array.from({ length: table.columns }, (_, column) => {
    const headers = table.cells.filter(cell => (cell.is_header || cell.row === 0) && cell.column <= column && column < cell.column + cell.column_span);
    const title = headers.map(cell => cell.text).filter(Boolean).join(" / ") || `第 ${column + 1} 列`;
    const samples = table.cells.filter(cell => cell.column === column && cell.row > 0 && !cell.is_header).slice(0, 3).map(cell => cell.text);
    return { column, title, samples, identifier: idHeader.test(title) };
  });
}

export function screenNumericExport(item: ScreenItem & { result?: { revision: number; edited: { tables: Table[] } } | null }, selections: Record<string, number[]>, confirmed: boolean) {
  const chosen = Object.entries(selections).filter(([, columns]) => columns.length > 0);
  if (!chosen.length) return undefined;
  if (!confirmed || !item.human_confirmed || item.review_stale || item.status !== "completed" || !item.result || item.result.revision !== item.result_revision)
    throw Error("请先保存并完成本图核对，再确认需要计算的金额或数量列。");
  const tables: Record<string, number[]> = {};
  for (const [tableIndex, columns] of chosen) {
    if (!/^\d+$/.test(tableIndex) || !item.result.edited.tables[Number(tableIndex)]) throw Error("数值列对应的表格已变化。");
    const table = item.result.edited.tables[Number(tableIndex)];
    const options = screenNumericColumnOptions(table);
    if (columns.some(column => !Number.isInteger(column) || column < 0 || column >= table.columns || options[column].identifier))
      throw Error("编号列不能转换为数值，请重新选择金额或数量列。");
    tables[tableIndex] = [...new Set(columns)].sort((a, b) => a - b);
  }
  return { [item.input_version_id]: tables };
}

function validTopology(table: Table) {
  const slots = table.cells.map(cell => [cell.row, cell.column, cell.row_span, cell.column_span]);
  if (slots.some(slot => slot.some(value => !Number.isInteger(value)) || slot[0] < 0 || slot[1] < 0
    || slot[2] < 1 || slot[3] < 1 || slot[0] + slot[2] > table.rows || slot[1] + slot[3] > table.columns)) return null;
  const anchors = slots.map(slot => `${slot[0]}:${slot[1]}`);
  if (new Set(anchors).size !== anchors.length) return null;
  if (table.rows * table.columns > 100000) return null;
  const occupied = new Set<string>();
  for (const [row, column, rows, columns] of slots) for (let r = row; r < row + rows; r++) for (let c = column; c < column + columns; c++) {
    const key = `${r}:${c}`;
    if (occupied.has(key)) return null;
    occupied.add(key);
  }
  if (occupied.size !== table.rows * table.columns) return null;
  return JSON.stringify([table.rows, table.columns, slots.map(slot => slot.join(":")).sort()]);
}

function samePolygon(left?: number[][] | null, right?: number[][] | null) {
  if (!left || !right || left.length < 3 || left.length !== right.length
    || [...left, ...right].some(point => point.length !== 2 || point.some(value => !Number.isFinite(value)))) return false;
  const area = (points: number[][]) => Math.abs(points.reduce((sum, point, index) => {
    const next = points[(index + 1) % points.length];
    return sum + point[0] * next[1] - next[0] * point[1];
  }, 0)) / 2;
  if (area(left) < 1 || area(right) < 1) return false;
  const xs = [...left, ...right].map(point => point[0]), ys = [...left, ...right].map(point => point[1]);
  const side = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
  if (side < 2) return false;
  const tolerance = 1 + side * .02;
  return [right, [...right].reverse()].some(points => points.some((_, offset) => left.every((point, index) => {
    const other = points[(index + offset) % points.length];
    return Math.hypot(point[0] - other[0], point[1] - other[1]) <= tolerance;
  })));
}

/** Indexes alone never establish correspondence between model candidates. */
export function screenCandidateCell(current: Table[], alternate: Table[], tableIndex: number, row: number, column: number) {
  if (current.length !== alternate.length) return { cell: undefined, reason: "表格数量不同，请分别核对整图候选。" };
  const left = current[tableIndex], right = alternate[tableIndex];
  if (!left || !right || !validTopology(left) || validTopology(left) !== validTopology(right))
    return { cell: undefined, reason: "行列或合并结构不同，无法可靠对应这个单元格。" };
  const cell = screenCell(left, row, column), other = screenCell(right, row, column);
  if (!cell || !other) return { cell: undefined, reason: "当前格位没有独立单元格。" };
  const allLeft = current.flatMap(table => table.cells), allRight = alternate.flatMap(table => table.cells);
  const geometry = samePolygon(cell.polygon, other.polygon)
    && allRight.filter(candidate => samePolygon(cell.polygon, candidate.polygon)).length === 1
    && allLeft.filter(candidate => samePolygon(other.polygon, candidate.polygon)).length === 1;
  if (geometry) return { cell: other, reason: "相同行列及合并结构，原图单元格几何唯一对应。", basis: "geometry" as const };
  const normal = (value: string) => value.replace(/\s/g, "");
  const lookup = new Map(right.cells.map(value => [`${value.row}:${value.column}`, value]));
  const agreement = left.cells.filter(value => normal(value.text) && normal(value.text) === normal(lookup.get(`${value.row}:${value.column}`)?.text || "")).length / Math.max(1, left.cells.length);
  const uniqueAnchor = (value: Cell) => normal(value.text) && allLeft.filter(candidate => normal(candidate.text) === normal(value.text)).length === 1
    && allRight.filter(candidate => normal(candidate.text) === normal(value.text)).length === 1
    && normal(lookup.get(`${value.row}:${value.column}`)?.text || "") === normal(value.text);
  const rowAnchor = left.cells.some(value => value.row === cell.row && value.column !== cell.column && uniqueAnchor(value));
  const columnAnchor = left.cells.some(value => value.column === cell.column && value.row !== cell.row && uniqueAnchor(value));
  if (agreement >= .6 && rowAnchor && columnAnchor)
    return { cell: other, reason: "相同行列及合并结构，同一行和列均有唯一相同文字锚点。", basis: "unique_text_anchors" as const };
  return { cell: undefined, reason: "单格几何或唯一文字锚点不足，请查看整表；无法仅凭行列编号填入另一转录。" };
}

export function screenCandidateReviewBody(item: ScreenItem, revision: number, candidateId: string, edits: ScreenCellEdit[], requestId: string) {
  if (item.status !== "completed" && item.status !== "needs_review") throw Error("请等待当前图片处理结束后采用整图候选。");
  if (edits.some(edit => edit.before !== edit.after)) throw Error("请先保存或清除当前修改，再采用整图候选。");
  if (!item.candidates?.some(candidate => candidate.id === candidateId)) throw Error("当前图片没有这个整图候选，请重新加载。");
  return { ...screenReviewBody(item, revision, "retain", [], requestId), candidate_id: candidateId };
}
