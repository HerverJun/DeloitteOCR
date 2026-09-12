import { describe, expect, it } from "vitest";
import { changeDocumentTables, changeDocumentText, displayOffset, documentText, savedEdit } from "./documentText";
import { createTable, pasteTsv } from "./tableEditing";
import { insertAxis } from "./tableOps";
import type { Edit, Table } from "./types";

const table = (value: string): Table => ({ rows: 1, columns: 2, cells: [
  { row: 0, column: 0, row_span: 1, column_span: 1, text: "00001234567890123456" },
  { row: 0, column: 1, row_span: 1, column_span: 1, text: value },
] });
function fixture(): Edit {
  const a = table("旧金额"), b = table("第二表");
  const first = '<table><tr><td>00001234567890123456</td><td>旧金额</td></tr></table>';
  const second = '| 编号 | 值 |\n| --- | --- |\n| 00001 | 第二表 |';
  const text = '😀标题\n' + first + '\n中间说明\n' + second + '\n尾注';
  a.source = { start: 0, end: 1, format: "html", sha256: "a" };
  b.source = { start: 2, end: 3, format: "markdown", sha256: "b" };
  return { text, tables: [a, b], text_sources: [
    { start: text.indexOf(first), end: text.indexOf(first)+first.length, table_index: 0, original_table: a },
    { start: text.indexOf(second), end: text.indexOf(second)+second.length, table_index: 1, original_table: b },
  ] };
}
describe("document text presentation and editing", () => {
  it("renders saved table cells in document order without tags and retains all prose", () => {
    const edit = fixture();
    edit.tables[0] = table("校对后");
    expect(documentText(edit).text).toBe('😀标题\n00001234567890123456\t校对后\n中间说明\n00001234567890123456\t第二表\n尾注');
    expect(edit.text).toContain('<table>');
  });
  it("writes text edits into the correct cell, preserving spans and immutable source", () => {
    const edit = fixture();
    const next = changeDocumentText(edit, documentText(edit).text.replace("第二表", "新值\n含换行\t和制表符"));
    expect(next.tables[1].cells[1].text).toBe("新值\n含换行\t和制表符");
    expect(next.text).toBe(edit.text);
    expect(edit.tables[1].cells[1].text).toBe("第二表");
    expect(next.tables[1].cells[1].column_span).toBe(1);
  });
  it("relocates UTF-16 ranges across consecutive prose edits", () => {
    let edit = fixture();
    edit = changeDocumentText(edit, documentText(edit).text.replace("😀标题", "😀扩展标题🚀"));
    edit = changeDocumentText(edit, documentText(edit).text.replace("中间说明", "说明"));
    edit = changeDocumentText(edit, documentText(edit).text.replace("旧金额", "000.00"));
    expect(documentText(edit).text).toContain("000.00\n说明\n");
    expect(edit.text).toContain("😀扩展标题🚀\n<table>");
    expect(displayOffset(edit, edit.text.indexOf("尾注"))).toBe(documentText(edit).text.indexOf("尾注"));
    expect(Object.keys(savedEdit(edit)).sort()).toEqual(["tables", "text"]);
  });
  it("preserves literal code when parser sources omit it, even for repeated table markup", () => {
    const edit = fixture(), before = edit.text;
    const prefix = '```html\n' + before.slice(edit.text_sources![0].start, edit.text_sources![0].end) + '\n```\n';
    edit.text = prefix + before;
    edit.text_sources = edit.text_sources!.map(s => ({...s,start:s.start+prefix.length,end:s.end+prefix.length}));
    expect(documentText(edit).text.startsWith(prefix)).toBe(true);
    expect(documentText(edit).text.slice(prefix.length)).not.toContain('<table>');
  });
  it("remaps the second table after deletion and retains the first table's source text", () => {
    const edit = fixture();
    const next = changeDocumentTables(edit, [edit.tables[1]]);
    expect(next.text_sources!.map(s=>s.table_index)).toEqual([null,0]);
    const edited = changeDocumentText(next, documentText(next).text.replace("第二表","仍可编辑"));
    expect(edited.tables[0].cells[1].text).toBe("仍可编辑");
    expect(documentText(edited).text).toContain("旧金额");
  });
  it("rejects ambiguous edits crossing structural delimiters without mutating content", () => {
    const edit = fixture();
    expect(()=>changeDocumentText(edit, "全部覆盖")).toThrow("跨单元格");
    expect(edit.tables[0].cells[1].text).toBe("旧金额");
  });
  it("supports plain documents, empty cells, captions and appended manual tables", () => {
    expect(changeDocumentText({text:"plain",tables:[],text_sources:[]},"changed").text).toBe("changed");
    const t = table(""); t.caption = "表题";
    let edit: Edit = {text:"正文",tables:[t],text_sources:[]};
    edit = changeDocumentText(edit, documentText(edit).text.replace("表题","新表题"));
    edit = changeDocumentText(edit, documentText(edit).text + "空格填值");
    expect(edit.tables[0].caption).toBe("新表题");
    expect(edit.tables[0].cells[1].text).toBe("空格填值");
  });
});
describe("table edit limits match server validation", () => {
  it("rejects create, paste and insert beyond individual axes before changing the document", () => {
    expect(()=>createTable(1,1001)).toThrow("1,000 列");
    expect(()=>createTable(10001,1)).toThrow("10,000 行");
    const original = createTable(1,1000);
    expect(()=>insertAxis(original,"column",1)).toThrow();
    expect(()=>pasteTsv(original,"last",0,1000)).toThrow();
    expect(original.columns).toBe(1000);
  });
});
