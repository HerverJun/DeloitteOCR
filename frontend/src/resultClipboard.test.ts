import { describe, expect, it, vi } from "vitest";
import { copySavedResultText } from "./resultClipboard";

function fixture() {
  const state = { id: "saved-result" };
  return {
    state,
    options: {
      id: state.id, currentId: () => state.id,
      flush: vi.fn(async () => {}),
      requestText: vi.fn(async (_path: string) => new Response("编号\t00123\n金额\t-12.50", { headers: { "Content-Type": "text/plain; charset=utf-8" } })),
      writeText: vi.fn(async (_: string) => {}),
    },
  };
}

describe("copy saved result text", () => {
  it("flushes first and copies literal Unicode text from the dedicated endpoint", async () => {
    const { options } = fixture();
    options.requestText.mockImplementation(async path => {
      expect(options.flush).toHaveBeenCalledOnce();
      expect(path).toBe("/results/saved-result/text");
      return new Response("编号\t00123\n金额\t-12.50", { headers: { "Content-Type": "text/plain; charset=utf-8" } });
    });
    await copySavedResultText(options);
    expect(options.writeText).toHaveBeenCalledWith("编号\t00123\n金额\t-12.50");
  });

  it.each(["application/zip", "application/json", ""])("does not copy a %s response as text", async type => {
    const { options } = fixture();
    options.requestText.mockResolvedValue(new Response(new Uint8Array([80, 75, 3, 4]), { headers: type ? { "Content-Type": type } : {} }));
    await expect(copySavedResultText(options)).rejects.toThrow("未返回纯文本");
    expect(options.writeText).not.toHaveBeenCalled();
  });

  it("preserves the clipboard if saving fails", async () => {
    const { options } = fixture();
    options.flush.mockRejectedValue(Error("revision conflict"));
    await expect(copySavedResultText(options)).rejects.toThrow("revision conflict");
    expect(options.requestText).not.toHaveBeenCalled();
    expect(options.writeText).not.toHaveBeenCalled();
  });

  it("rejects a target switch while flushing before requesting text", async () => {
    const { options, state } = fixture();
    options.flush.mockImplementation(async () => { state.id = "another-result"; });
    await expect(copySavedResultText(options)).rejects.toThrow("结果已切换");
    expect(options.requestText).not.toHaveBeenCalled();
    expect(options.writeText).not.toHaveBeenCalled();
  });

  it("does not put the old result into the clipboard if navigation occurs during the request", async () => {
    const { options, state } = fixture();
    options.requestText.mockImplementation(async () => {
      state.id = "another-result";
      return new Response("old result", { headers: { "Content-Type": "text/plain" } });
    });
    await expect(copySavedResultText(options)).rejects.toThrow("结果已切换");
    expect(options.writeText).not.toHaveBeenCalled();
  });
});
