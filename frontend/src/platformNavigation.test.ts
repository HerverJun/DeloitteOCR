import { afterEach, beforeEach, expect, it, vi } from "vitest";

let entries: Map<string, string>;
beforeEach(() => {
  vi.resetModules();
  entries = new Map();
  vi.stubGlobal("window", new EventTarget());
  vi.stubGlobal("location", { hash: `#platform_ticket=${"t".repeat(43)}`,
    pathname: "/", search: "" });
  vi.stubGlobal("history", { state: { preserved: true }, replaceState: vi.fn((_state, _title, address: string) => {
    location.hash = new URL(address, "http://127.0.0.1:5555").hash;
  }) });
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => entries.get(key),
    setItem: (key: string, value: string) => entries.set(key, value),
  });
});
afterEach(() => vi.unstubAllGlobals());

it("scrubs the ticket, keeps the native session, and resolves a one-time handoff", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ app_id: "ocr", instance_id: "instanceA",
    link_id: "linkA", workspace_id: "workspaceA", project_id: "projectA",
    session_token: "s".repeat(32),
    return_url: "http://127.0.0.1:8765/apps/ocr?workspace=workspaceA" })));
  vi.stubGlobal("fetch", fetchMock);
  const { navigationFromLaunch, safeReturnUrl } = await import("./platformNavigation");
  const pending = navigationFromLaunch();
  expect(pending).toBe(navigationFromLaunch());
  const context = await pending!;
  expect(safeReturnUrl(context)).toBe("http://127.0.0.1:8765/apps/ocr?workspace=workspaceA");
  expect(location.hash).toBe("");
  expect(entries.get("ocr-token")).toBe("s".repeat(32));
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock).toHaveBeenCalledWith(`/api/platform/navigation/tickets/${"t".repeat(43)}`,
    expect.objectContaining({ headers: { Authorization: "Bearer " } }));
});

it("does not offer an external, deceptive, or mismatched return destination", async () => {
  const { safeReturnUrl } = await import("./platformNavigation");
  const value = { app_id: "ocr" as const, instance_id: "instanceA", link_id: "linkA",
    workspace_id: "workspaceA", project_id: "projectA",
    return_url: "http://127.0.0.1:8765/apps/ocr?workspace=workspaceA" };
  for (const return_url of ["https://example.invalid/apps/ocr?workspace=workspaceA",
    "http://127.0.0.1:8765@evil.invalid/apps/ocr?workspace=workspaceA",
    "http://127.0.0.1:8765//evil.invalid?workspace=workspaceA",
    "http://127.0.0.1:8765/apps/ocr?workspace=other",
    "http://127.0.0.1:8765/apps/ocr?workspace=workspaceA&token=secret",
    "http://127.0.0.1:8765/apps/ocr?workspace=workspaceA#secret"]) {
    expect(safeReturnUrl({ ...value, return_url })).toBeNull();
  }
});
