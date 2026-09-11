import { describe, it, expect } from "vitest";
import {
  filterPhotos,
  imageStatus,
  toggleVisible,
} from "./workspacePreferences";
import type { Photo, Task } from "./types";
const photos = ["凭证01.PNG", "凭证02.png", "合同.jpg"].map(
  (name, i) => ({ id: String(i), name }) as Photo,
);
const task = (image_id: string, status: string, engine = "ppocr") =>
  ({ image_id, status, engine }) as Task;
describe("资料筛选与批量范围", () => {
  it("筛选后全选仅增减可见项目，保留隐藏选择", () => {
    expect(toggleVisible(["2"], ["0", "1"], true)).toEqual(["2", "0", "1"]);
    expect(toggleVisible(["2", "0", "1"], ["0", "1"], false)).toEqual(["2"]);
    expect(toggleVisible(["0"], ["0", "1"], true)).toEqual(["0", "1"]);
  });
  it("文件名大小写无关，并联合真实状态筛选", () => {
    const tasks = [task("0", "succeeded"), task("1", "failed")];
    expect(
      filterPhotos(photos, tasks, "PNG", "failed").map((p) => p.id),
    ).toEqual(["1"]);
    expect(
      filterPhotos(photos, tasks, "", "unrecognized").map((p) => p.id),
    ).toEqual(["2"]);
  });
  it("多模型批次仍有任务等待时保持处理中，图像修正不冒充识别成功", () => {
    expect(
      imageStatus("0", [task("0", "running"), task("0", "succeeded", "glm")]),
    ).toBe("processing");
    expect(
      imageStatus("0", [task("0", "failed"), task("0", "succeeded", "dewarp")]),
    ).toBe("failed");
    expect(imageStatus("0", [task("0", "succeeded", "dewarp")])).toBe(
      "unrecognized",
    );
  });
});
