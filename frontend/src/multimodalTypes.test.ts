import { beforeEach, expect, it } from "vitest";
import { readMultimodalPending, writeMultimodalPending, type PendingMultimodalRequest } from "./multimodalTypes";

const entries = new Map<string, string>();
beforeEach(() => {
  entries.clear();
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => entries.set(key, value),
    removeItem: (key: string) => entries.delete(key),
  } });
});

const submission = (): PendingMultimodalRequest => ({ kind: "submit", result_id: "result-A", version_id: "image-v1", body: {
  request_id: "original-submission", revision: 7, version_id: "image-v1", model_id: "configured-vision-model", scope: "target",
  target: { kind: "cell", table: 0, row: 2, column: 3 },
} });

it("restores the exact original submission after a lost response without regenerating its identity or revision", () => {
  const intent = submission();
  writeMultimodalPending("result-A", "image-v1", intent);
  const restored = readMultimodalPending("result-A", "image-v1");
  expect(restored).toEqual(intent);
  expect(restored).not.toBe(intent);
  expect(readMultimodalPending("result-A", "image-v1")?.body.request_id).toBe("original-submission");
});

it("does not reuse an in-flight decision on a different result or image version", () => {
  const intent: PendingMultimodalRequest = { kind: "decision", result_id: "result-A", version_id: "image-v1", proposal_id: "proposal-A",
    body: { request_id: "accept-once", revision: 7, version_id: "image-v1", action: "accept" } };
  writeMultimodalPending("result-A", "image-v1", intent);
  expect(readMultimodalPending("result-B", "image-v1")).toBeNull();
  expect(readMultimodalPending("result-A", "image-v2")).toBeNull();
  expect(() => writeMultimodalPending("result-A", "image-v2", intent)).toThrow("审校请求与当前页面不匹配");
  expect(readMultimodalPending("result-A", "image-v1")?.body).toEqual(intent.body);
});

it("clearing an acknowledged request retains unrelated page submissions", () => {
  const first = submission();
  const second = { ...submission(), result_id: "result-B" };
  writeMultimodalPending("result-A", "image-v1", first);
  writeMultimodalPending("result-B", "image-v1", second);
  writeMultimodalPending("result-A", "image-v1", null);
  expect(readMultimodalPending("result-A", "image-v1")).toBeNull();
  expect(readMultimodalPending("result-B", "image-v1")).toEqual(second);
});

it("ignores incomplete, mismatched, and corrupted recovery records", () => {
  writeMultimodalPending("result-A", "image-v1", submission());
  const key = [...entries.keys()][0];
  for (const value of ["{broken", "null", JSON.stringify({ ...submission(), body: {} }),
    JSON.stringify({ ...submission(), body: { ...submission().body, version_id: "image-v2" } }),
    JSON.stringify({ ...submission(), kind: "unexpected" })]) {
    entries.set(key, value);
    expect(readMultimodalPending("result-A", "image-v1")).toBeNull();
  }
});

it("a browser storage failure does not block the current in-memory operation", () => {
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, get() { throw new Error("storage unavailable"); } });
  expect(() => writeMultimodalPending("result-A", "image-v1", submission())).not.toThrow();
  expect(readMultimodalPending("result-A", "image-v1")).toBeNull();
});
