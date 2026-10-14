import type { Edit, Result, Table } from "./types";

type Segment = {
  start: number; end: number; kind: "text" | "cell" | "caption" | "separator";
  rawStart?: number; rawEnd?: number; table?: number; cell?: number;
};
export type TextView = { text: string; segments: Segment[] };

export const editableResult = (result: Result): Edit => ({
  ...result.edited, text_sources: result.text_sources,
});
export const savedEdit = (edit: Edit) => ({ text: edit.text, tables: edit.tables });

// The server parser supplies protected HTML/Markdown ranges. Never strip tags
// with regex: documents can contain literal code, repeated tables and emoji.
export function documentText(edit: Edit): TextView {
  const segments: Segment[] = [];
  const chunks: string[] = [];
  let length = 0;
  const append = (text: string, detail: Omit<Segment, "start" | "end">) => {
    segments.push({ ...detail, start: length, end: length + text.length });
    chunks.push(text); length += text.length;
  };
  const table = (value: Table, index?: number) => {
    if (value.caption) {
      append(value.caption, { kind: "caption", table: index });
      append("\n", { kind: "separator" });
    }
    const cells = new Map(value.cells.map((cell, i) => [`${cell.row}:${cell.column}`, i]));
    for (let r = 0; r < value.rows; r++) {
      if (r) append("\n", { kind: "separator" });
      for (let c = 0; c < value.columns; c++) {
        if (c) append("\t", { kind: "separator" });
        const cell = cells.get(`${r}:${c}`);
        append(cell === undefined ? "" : value.cells[cell].text,
          cell === undefined ? { kind: "separator" } : { kind: "cell", table: index, cell });
      }
    }
  };
  const used = new Set<number>();
  let offset = 0;
  for (const source of edit.text_sources || []) {
    if (source.start < offset || source.end > edit.text.length) continue;
    append(edit.text.slice(offset, source.start), { kind: "text", rawStart: offset, rawEnd: source.start });
    const index = source.table_index;
    if (index !== null && edit.tables[index]) {
      table(edit.tables[index], index); used.add(index);
    } else table(source.original_table);
    offset = source.end;
  }
  append(edit.text.slice(offset), { kind: "text", rawStart: offset, rawEnd: edit.text.length });
  edit.tables.forEach((value, index) => {
    if (used.has(index)) return;
    // Legacy drafts without a source map stay literal until saved/reloaded.
    if (edit.text_sources === undefined) return;
    append("\n\n", { kind: "separator" }); table(value, index);
  });
  return { text: chunks.join(""), segments };
}

export function changeDocumentText(edit: Edit, next: string): Edit {
  const view = documentText(edit);
  if (view.text === next) return edit;
  let start = 0, suffix = 0;
  while (start < Math.min(view.text.length, next.length) && view.text[start] === next[start]) start++;
  while (suffix < Math.min(view.text.length, next.length) - start &&
    view.text[view.text.length - suffix - 1] === next[next.length - suffix - 1]) suffix++;
  const end = view.text.length - suffix, replacement = next.slice(start, next.length - suffix);
  const candidates = view.segments.filter(s => s.kind !== "separator" && start >= s.start && end <= s.end);
  // At an insertion boundary prefer a non-empty field rather than a synthetic
  // zero-length paragraph next to a table.
  const segment = candidates.find(s => s.end > s.start) || candidates[0];
  if (!segment) throw Error("跨单元格或行列的修改请在「表格」页签操作；文字和单个单元格可直接校对。");
  const value = view.text.slice(segment.start, start) + replacement + view.text.slice(end, segment.end);
  if (segment.kind === "text") {
    const rawStart = segment.rawStart!, rawEnd = segment.rawEnd!;
    const delta = value.length - (rawEnd - rawStart);
    return { ...edit, text: edit.text.slice(0, rawStart) + value + edit.text.slice(rawEnd),
      text_sources: edit.text_sources?.map(s => s.start >= rawEnd ? { ...s, start: s.start + delta, end: s.end + delta } : s) };
  }
  if (segment.table === undefined)
    throw Error("此表已移除结构，当前显示保留的原文；请撤销删除后继续校对表格。");
  return { ...edit, tables: edit.tables.map((t, index) => index !== segment.table ? t :
    segment.kind === "caption" ? { ...t, caption: value } :
      { ...t, cells: t.cells.map((c, index) => index === segment.cell ? { ...c, text: value } : c) }) };
}

export function changeDocumentTables(edit: Edit, tables: Table[]): Edit {
  const find = (old: Table, oldIndex: number) => {
    let index = tables.indexOf(old);
    if (index < 0) index = tables.findIndex(t => old.fusion_id ? t.fusion_id === old.fusion_id :
      old.source && JSON.stringify(t.source) === JSON.stringify(old.source));
    if (index < 0 && tables.length === edit.tables.length) index = oldIndex;
    return index < 0 ? null : index;
  };
  return { ...edit, tables, text_sources: edit.text_sources?.map(s => ({ ...s,
    table_index: s.table_index === null ? null : find(edit.tables[s.table_index], s.table_index) })) };
}

export function displayOffset(edit: Edit, rawOffset: number): number {
  const view = documentText(edit);
  const segment = view.segments.find(s => s.kind === "text" && rawOffset >= s.rawStart! && rawOffset <= s.rawEnd!);
  if (segment) return segment.start + rawOffset - segment.rawStart!;
  return view.segments.find(s => s.kind === "text" && s.rawStart! > rawOffset)?.start ?? view.text.length;
}
