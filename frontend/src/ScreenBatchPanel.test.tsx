import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("./api", () => ({ api: vi.fn(), request: vi.fn() }));
vi.mock("./RegionPreview", () => ({ RegionPreview: () => null }));
import { ScreenBatchPanel } from "./ScreenBatchPanel";
import { ScreenCandidatePreview } from "./ScreenCandidatePreview";
import { ImageCanvas } from "./ImageCanvas";
import { ScreenDiagnosticPanel } from "./ScreenDiagnosticPanel";
import type { ScreenCandidate } from "./screenBatchWorkflow";
import type { Version } from "./types";

describe("ordinary OCR batch entry", () => {
  it("shows unlocalized source evidence and disabled unverified actions without a guessed cell", () => {
    const markup = renderToStaticMarkup(<ScreenDiagnosticPanel index={0} busy={false} select={() => {}} action={() => {}}
      data={{items:[{id:"i1",code:"difference",severity:"high",reason:"未能可靠对应错格",location:{level:"table",table:0,basis:"candidate_table_without_source_geometry"},evidence:[{kind:"auxiliary_ocr",current:"01.10",alternative:"01.20"}],applicable_actions:[{action:"local_reread",label:"局部复识",enabled:false,reason:"capability_not_verified"}]}],summary:{issues:1,groups:1,cell:0,table:1,image:0},budget:{remaining_seconds:null}}} />);
    expect(markup).toContain("无可靠单格定位"); expect(markup).toContain("能力尚未核实"); expect(markup).toContain("01.20");
    expect(markup).not.toContain("可靠单格）"); expect(markup).toContain("disabled");
  });
  it("offers the business batch action without requiring a model choice", () => {
    const markup = renderToStaticMarkup(<ScreenBatchPanel projectId="project-1" images={[]} selectedImageIds={[]}
      onError={() => {}} onRefresh={async () => {}} onOpenResult={async () => {}} />);
    expect(markup).toContain("拍屏表格批量转 Excel");
    expect(markup).not.toContain("Hunyuan");
    expect(markup).not.toContain("GLM");
  });
  it("disables entry before a project exists", () => {
    const markup = renderToStaticMarkup(<ScreenBatchPanel projectId="" images={[]} selectedImageIds={[]}
      onError={() => {}} onRefresh={async () => {}} onOpenResult={async () => {}} />);
    expect(markup).toContain("disabled");
  });
  it("shows all whole candidate engines, table previews and the reason a cell cannot align", () => {
    const candidate: ScreenCandidate = {id:"candidate-a",engine:"hunyuan",raw:{project_image_version:"v-a",tables:[
      {rows:1,columns:1,cells:[{row:0,column:0,row_span:1,column_span:1,text:"00123"}]}]}};
    const markup = renderToStaticMarkup(<ScreenCandidatePreview candidates={[candidate,{...candidate,id:"candidate-b",engine:"paddlevl"}]}
      inputVersionId="v-a" currentTables={candidate.raw.tables} tableIndex={0} row={0} column={0} currentValue="draft"
      issues={[]} busy={false} hasDraft={true} canAdopt={true} onFill={() => {}} onAdopt={() => {}} />);
    expect(markup).toContain("HunyuanOCR");
    expect(markup).toContain("PaddleOCR-VL");
    expect(markup).toContain("00123");
    expect(markup).toContain("含待保存修改");
    expect(markup).toContain("无法建立可靠对应");
    expect(markup).toContain("采用前请先保存或清除");
    expect(markup).not.toContain("填入当前格草稿");
  });
  it("starts with the adopted whole candidate and otherwise the first candidate with a table", () => {
    const empty: ScreenCandidate = { id:"empty",engine:"hunyuan",raw:{project_image_version:"v-a",tables:[]} };
    const glm: ScreenCandidate = { id:"glm",engine:"glm",raw:{project_image_version:"v-a",tables:[
      {rows:1,columns:1,cells:[{row:0,column:0,row_span:1,column_span:1,text:"保留当前表格"}]}]} };
    const render = (selectedCandidateId?: string) => renderToStaticMarkup(<ScreenCandidatePreview candidates={[empty,glm]}
      inputVersionId="v-a" currentTables={glm.raw.tables} tableIndex={0} row={0} column={0} currentValue="draft"
      selectedCandidateId={selectedCandidateId} issues={[{code:"no_table",candidate_id:"empty"}]}
      busy={false} hasDraft={true} canAdopt={true} onFill={() => {}} onAdopt={() => {}} />);
    for (const preferred of ["glm","stale-candidate",undefined]) {
      expect(render(preferred)).toContain("保留当前表格");
      expect(render(preferred)).toContain('aria-label="GLM-OCR 表 1 候选预览"');
      expect(render(preferred)).toContain("含待保存修改");
    }
    // An explicit current candidate remains inspectable even when it is empty.
    expect(render("empty")).toContain("此候选未提供表格");
  });
  it("offers original-pixel zoom and panning in read-only review without image edit actions or guessed cell boxes", () => {
    const version: Version = {id:"v-a",image_id:"image-a",width:1520,height:1708,parent_id:null,operations:"[]",created:""};
    const render = (polygon: number[][] | null, versionId="v-a") => renderToStaticMarkup(<ImageCanvas
      version={version} versions={[version]} blocks={[]} highlight={null} busy={false} readOnly
      onVersion={() => {}} onTransform={() => {}} onRegion={() => {}}
      reviewLocation={{level:polygon ? "cell" : "image",polygon,version_id:versionId,reason:"actual source"}} />);
    const full=render(null);
    expect(full).toContain("原图核对工作区");
    expect(full).toContain("100%");
    expect(full).toContain('aria-label="拖动图片"');
    expect(full).toContain('aria-label="放大"');
    expect(full).not.toContain('aria-label="裁剪"');
    expect(full).not.toContain('aria-label="图片版本"');
    expect(full).not.toContain('aria-label="增强对比度"');
    expect(full).not.toContain("放大疑点区域");
    const actual=[[10,10],[90,10],[90,50],[10,50]];
    expect(render(actual)).toContain("放大疑点区域");
    expect(render(actual,"other-version")).not.toContain("放大疑点区域");
  });
});
