import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("./api", () => ({ api: vi.fn(), request: vi.fn() }));
vi.mock("./RegionPreview", () => ({ RegionPreview: () => null }));
import { ScreenBatchPanel } from "./ScreenBatchPanel";

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
});
