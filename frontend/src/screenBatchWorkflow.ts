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
  error?: string | null; issues: ScreenIssue[];
  candidates?: { id: string; engine: string; table_count: number }[];
};
export type ScreenBatch = {
  batch_id: string; project_id: string; status: string; counts: ScreenCounts; items: ScreenItem[];
  total_items: number; next_offset: number | null; created: string; updated: string; worker_error?: string | null;
};
export type ScreenCellEdit = { table_index: number; row: number; column: number; before: string; after: string };

export const screenStatuses: Record<string, string> = {
  queued: "等待处理", prepared: "表区处理完成", running: "处理中", awaiting_upgrade: "提高识别精度",
  completed: "可交付", needs_review: "待核对", failed: "失败", cancelled: "已取消", paused: "已暂停",
  succeeded: "已完成", partial: "处理结束，部分需核对",
};

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
