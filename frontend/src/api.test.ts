import { afterEach, beforeEach, expect, it, vi } from "vitest";

let events: EventTarget;
let entries: Map<string, string>;
beforeEach(() => {
  vi.resetModules();
  events = new EventTarget();
  entries = new Map();
  vi.stubGlobal("window", events);
  vi.stubGlobal("location", { hash: "#token=first", pathname: "/", search: "?launch=test" });
  vi.stubGlobal("history", { replaceState: vi.fn((_state, _title, url: string) => {
    location.hash = new URL(url, "http://localhost").hash;
  }) });
  vi.stubGlobal("sessionStorage", { getItem: (key: string) => entries.get(key), setItem: (key: string, value: string) => entries.set(key, value) });
});
afterEach(() => vi.unstubAllGlobals());

it("accepts a new launch fragment without reloading the page or leaving its token in the URL", async () => {
  const { request } = await import("./api");
  const changed = vi.fn();
  events.addEventListener("ocr-session-changed", changed);
  location.hash = "#token=renewed";
  events.dispatchEvent(new Event("hashchange"));
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}")));
  await request("/state");
  expect(changed).toHaveBeenCalledOnce();
  expect(location.hash).toBe("");
  expect(history.replaceState).toHaveBeenLastCalledWith(null, "", "/?launch=test");
  expect(fetch).toHaveBeenCalledWith("/api/state", expect.objectContaining({ headers: { Authorization: "Bearer renewed" } }));
});

it("signals expired access but ignores a late 401 for a replaced credential", async () => {
  const { request } = await import("./api");
  const expired = vi.fn();
  events.addEventListener("ocr-session-expired", expired);
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { finish = resolve; })));
  const pending = request("/state");
  location.hash = "#token=renewed";
  events.dispatchEvent(new Event("hashchange"));
  finish(new Response('{"detail":"expired"}', { status: 401 }));
  await expect(pending).rejects.toThrow("expired");
  expect(expired).not.toHaveBeenCalled();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"detail":"expired"}', { status: 401 })));
  await expect(request("/state")).rejects.toThrow("expired");
  expect(expired).toHaveBeenCalledOnce();
});
