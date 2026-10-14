import { beforeEach, expect, it } from "vitest";
import { readRecovery, writeRecovery, type Recovery } from "./editorRecovery";

function memoryStorage() {
  const entries: Record<string, string> = {};
  return new Proxy({
    getItem: (key: string) => entries[key] ?? null,
    setItem: (key: string, value: string) => { entries[key] = value; },
    removeItem: (key: string) => { delete entries[key]; },
  }, {
    ownKeys: () => Object.keys(entries),
    getOwnPropertyDescriptor: (_, key) => key in entries ? { configurable: true, enumerable: true } : undefined,
  });
}
const draft = (updated: number): Recovery => ({ resultId: "result", revision: 2,
  edit: { text: "未保存中文", tables: [] }, review: null, decision: null, updated });
beforeEach(() => Object.defineProperty(globalThis, "localStorage", { value: memoryStorage(), configurable: true }));

it("transfers another window's exact draft so saving cannot resurrect it", () => {
  writeRecovery("closed-window", draft(1));
  expect(readRecovery("new-window", "result")?.edit?.text).toBe("未保存中文");
  writeRecovery("new-window", { ...draft(2), edit: null });
  expect(readRecovery("third-window", "result")).toBeNull();
});

it("retains a newer draft from a concurrently active window", () => {
  writeRecovery("first", draft(1));
  readRecovery("second", "result");
  writeRecovery("first", { ...draft(3), edit: { text: "另一窗口更新", tables: [] } });
  writeRecovery("second", { ...draft(2), edit: null });
  expect(readRecovery("third", "result")?.edit?.text).toBe("另一窗口更新");
});

it("preserves the request ID and expected revision of an unacknowledged decision", () => {
  const pending = { resultId: "result", issueId: "issue", body: { request_id: "same-request", revision: 2, value: "00123" } };
  writeRecovery("old", { ...draft(1), edit: null, decision: pending });
  expect(readRecovery("new", "result")?.decision).toEqual(pending);
});
