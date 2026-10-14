import { describe, expect, it } from "vitest";
import { operationSummary } from "./operationSummary";

describe("image processing summary", () => {
  it("turns stored operation arrays into readable descriptions", () => {
    expect(operationSummary("[]")).toBe("无");
    expect(operationSummary(JSON.stringify([{ kind: "rotate", degrees: 90 }, { kind: "contrast", factor: 1.3 }])))
      .toBe("旋转 90°、对比度 1.3 倍");
    expect(operationSummary(JSON.stringify({ kind: "batch", preset: [{ kind: "crop", box: [1, 2, 3, 4] }] })))
      .toBe("批次处理：裁剪");
    expect(operationSummary(JSON.stringify({ kind: "import", exif_normalized: true })))
      .toBe("导入（已校正照片方向）");
  });

  it("does not leak unrecognized JSON into the primary description", () => {
    expect(operationSummary("{broken")).toBe("处理记录无法读取");
    expect(operationSummary(JSON.stringify({ kind: "future", data: { x: 1 } }))).toBe("其他处理");
  });
});
