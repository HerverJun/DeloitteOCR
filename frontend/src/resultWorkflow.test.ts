import { expect, it } from "vitest";
import { adoptedResult, exportPhotos } from "./resultWorkflow";
import { filterPhotos } from "./workspacePreferences";
import type { Photo, Task } from "./types";

it("explicit adoption survives later failure and preview-independent batch selection", () => {
  const photo = { id: "A", selected_result: "original" } as Photo;
  const tasks = [
    { image_id: "A", result_id: "other", status: "succeeded" },
    { image_id: "A", result_id: null, status: "failed" },
  ] as Task[];
  expect(adoptedResult(photo, tasks)).toBe("original");
  expect(adoptedResult({ ...photo, selected_result: null }, tasks)).toBe(
    "other",
  );
});

it("selected scope never silently expands to the project and confirmed filter preserves explicit scope", () => {
  const photos = [
    { id: "A", name: "A", review_status: "confirmed" },
    { id: "B", name: "B", review_status: "question" },
    { id: "C", name: "C", review_status: "confirmed" },
  ] as Photo[];
  expect(exportPhotos(photos, [], "selected", false)).toEqual([]);
  expect(
    exportPhotos(photos, ["A", "B"], "selected", true).map((p) => p.id),
  ).toEqual(["A"]);
  expect(exportPhotos(photos, ["B"], "project", true).map((p) => p.id)).toEqual(
    ["A", "C"],
  );
  expect(
    filterPhotos(photos, [], "", "review:question").map((p) => p.id),
  ).toEqual(["B"]);
});
