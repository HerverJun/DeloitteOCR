import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("./api", () => ({ api: vi.fn(), request: vi.fn() }));
vi.mock("./RegionPreview", () => ({ RegionPreview: () => null }));
import { ScreenBatchPanel } from "./ScreenBatchPanel";
import { ScreenCandidatePreview } from "./ScreenCandidatePreview";
import type { ScreenCandidate } from "./screenBatchWorkflow";

describe("ordinary OCR batch entry", () => {
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
});
