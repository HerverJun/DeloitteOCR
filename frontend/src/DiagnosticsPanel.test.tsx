import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { DiagnosticsPanel } from "./DiagnosticsPanel";

const report = {
  platform: "Windows", python: "3.12.10", bundle: "E:\\portable",
  gpu: "RTX 4070, 595.97, 16376 MiB, 13284 MiB", gpu_exit: 0,
  missing: [], runtimes: { ppocr: true, glm: true }, disk_free_bytes: 8 * 1024 ** 3,
};

describe("diagnostics panel", () => {
  it("shows a readable checklist and folds away the raw report", () => {
    const markup = renderToStaticMarkup(<DiagnosticsPanel diagnostics={{
      passed: true, details: JSON.stringify(report), free_bytes: 4 * 1024 ** 3,
    }} />);
    expect(markup).toContain("环境检查通过");
    expect(markup).toContain("PP-OCRv6：已找到");
    expect(markup).toContain("4.0 GiB");
    expect(markup).toContain("<details class=\"environment-technical\"");
    expect(markup).not.toContain("<details open");
  });

  it("does not call unreadable or conflicting results a success", () => {
    const unreadable = renderToStaticMarkup(<DiagnosticsPanel diagnostics={{ passed: true, details: "bad" }} />);
    expect(unreadable).toContain("结果需复核");
    const failed = renderToStaticMarkup(<DiagnosticsPanel diagnostics={{
      passed: false, details: JSON.stringify({ ...report, gpu_exit: 1, missing: ["missing-model"] }),
    }} />);
    expect(failed).toContain("查询失败");
    expect(failed).toContain("1 项缺失");
    expect(failed).not.toContain("环境检查通过");
  });
});
