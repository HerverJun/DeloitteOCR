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
    removeItem: (key: string) => entries.delete(key),
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
  expect(safeReturnUrl(context!)).toBe("http://127.0.0.1:8765/apps/ocr?workspace=workspaceA");
  expect(location.hash).toBe("");
  expect(entries.get("ocr-token")).toBe("s".repeat(32));
  expect(JSON.parse(entries.get("ocr-platform-navigation")!)).toEqual(context);
  expect(entries.get("ocr-platform-navigation")).not.toContain("session_token");
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock).toHaveBeenCalledWith(`/api/platform/navigation/tickets/${"t".repeat(43)}`,
    expect.objectContaining({ headers: { Authorization: "Bearer " } }));
});

const navigation = { app_id: "ocr" as const, instance_id: "instanceA", link_id: "linkA",
  workspace_id: "workspaceA", project_id: "projectA",
  return_url: "http://127.0.0.1:8765/apps/ocr?workspace=workspaceA" };

it("restores the same tab's project and return link after ticket consumption only when the service identity matches", async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ ...navigation,
    session_token: "s".repeat(32) })));
  vi.stubGlobal("fetch", fetchMock);
  const firstModule = await import("./platformNavigation");
  expect(await firstModule.navigationFromLaunch()).toEqual(navigation);
  expect(location.hash).toBe("");

  vi.resetModules();
  fetchMock.mockClear().mockResolvedValueOnce(new Response(JSON.stringify({ app_id: "ocr", instance_id: "instanceA" })));
  const refreshedModule = await import("./platformNavigation");
  const pending = refreshedModule.navigationFromLaunch();
  expect(pending).toBe(refreshedModule.navigationFromLaunch());
  expect(await pending).toEqual(navigation);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock).toHaveBeenCalledWith("/api/platform/v1/identity",
    expect.objectContaining({ headers: { Authorization: "Bearer " + "s".repeat(32) } }));
  expect(JSON.parse(entries.get("ocr-platform-navigation")!)).toEqual(navigation);
});

it.each([
  { app_id: "ocr", instance_id: "instanceOther" },
  { app_id: "classification", instance_id: "instanceA" },
])("clears a saved handoff when the current native service identity is $app_id / $instance_id", async (identity) => {
  location.hash = "";
  entries.set("ocr-platform-navigation", JSON.stringify(navigation));
  entries.set("ocr-token", "s".repeat(32));
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(identity))));
  const { navigationFromLaunch } = await import("./platformNavigation");
  expect(await navigationFromLaunch()).toBeNull();
  expect(entries.has("ocr-platform-navigation")).toBe(false);
  expect(entries.get("ocr-token")).toBe("s".repeat(32));
});

it.each([
  "not-json", "null", "[]", "{}",
  JSON.stringify({ ...navigation, project_id: "../wrong" }),
  JSON.stringify({ ...navigation, return_url: "http://127.0.0.1:8765/apps/ocr?workspace=other" }),
  JSON.stringify({ ...navigation, app_id: "classification" }),
  JSON.stringify({ ...navigation, session_token: "secret" }),
])("rejects and removes malformed or unsafe cached navigation without contacting the service (%s)", async (raw) => {
  location.hash = "";
  entries.set("ocr-platform-navigation", raw);
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const { navigationFromLaunch } = await import("./platformNavigation");
  expect(navigationFromLaunch()).toBeNull();
  expect(entries.has("ocr-platform-navigation")).toBe(false);
  expect(fetchMock).not.toHaveBeenCalled();
});

it("discards saved context when identity cannot be confirmed and keeps the native credential", async () => {
  location.hash = "";
  entries.set("ocr-platform-navigation", JSON.stringify(navigation));
  entries.set("ocr-token", "s".repeat(32));
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"detail":"unavailable"}', { status: 503 })));
  const { navigationFromLaunch } = await import("./platformNavigation");
  expect(await navigationFromLaunch()).toBeNull();
  expect(entries.has("ocr-platform-navigation")).toBe(false);
  expect(entries.get("ocr-token")).toBe("s".repeat(32));
});

it("a new ticket replaces saved workspace context without reusing the older service identity", async () => {
  entries.set("ocr-platform-navigation", JSON.stringify(navigation));
  const replacement = { ...navigation, instance_id: "instanceB", workspace_id: "workspaceB", project_id: "projectB",
    return_url: "http://127.0.0.1:8765/apps/ocr?workspace=workspaceB" };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...replacement, session_token: "b".repeat(32) })));
  vi.stubGlobal("fetch", fetchMock);
  const { navigationFromLaunch } = await import("./platformNavigation");
  expect(await navigationFromLaunch()).toEqual(replacement);
  expect(JSON.parse(entries.get("ocr-platform-navigation")!)).toEqual(replacement);
  expect(entries.get("ocr-token")).toBe("b".repeat(32));
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/navigation/tickets/"), expect.anything());
});

it("an invalid new ticket clears older context instead of silently reopening the previous platform project", async () => {
  location.hash = "#platform_ticket=invalid";
  entries.set("ocr-platform-navigation", JSON.stringify(navigation));
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const { navigationFromLaunch } = await import("./platformNavigation");
  expect(navigationFromLaunch()).toBeNull();
  expect(location.hash).toBe("");
  expect(entries.has("ocr-platform-navigation")).toBe(false);
  expect(fetchMock).not.toHaveBeenCalled();
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
