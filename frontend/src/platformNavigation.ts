import { api } from "./api";

const ID = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const TICKET = /^[A-Za-z0-9_-]{32,128}$/;
const ROUTE = /^\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*$/;

export type PlatformNavigation = {
  app_id: "ocr";
  instance_id: string;
  link_id: string;
  workspace_id: string;
  return_url: string;
};

export function consumePlatformTicket(): string | null {
  const hash = new URLSearchParams(location.hash.slice(1));
  const ticket = hash.get("platform_ticket");
  if (ticket === null) return null;
  hash.delete("platform_ticket");
  history.replaceState(history.state, "", location.pathname + location.search + (hash.size ? `#${hash}` : ""));
  return TICKET.test(ticket) ? ticket : null;
}

export function safeReturnUrl(value: PlatformNavigation): string | null {
  if (value.app_id !== "ocr" || ![value.instance_id, value.link_id, value.workspace_id].every(id => ID.test(id))) return null;
  try {
    const url = new URL(value.return_url);
    const port = Number(url.port);
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !Number.isInteger(port)
      || port < 1 || port > 65535 || url.username || url.password || url.hash || !ROUTE.test(url.pathname)
      || url.searchParams.size !== 1 || url.searchParams.get("workspace") !== value.workspace_id) return null;
    return url.href;
  } catch {
    return null;
  }
}

export async function resolvePlatformNavigation(ticket: string): Promise<PlatformNavigation> {
  if (!TICKET.test(ticket)) throw new Error("平台入口无效");
  const value = await api<PlatformNavigation>(`/platform/navigation/tickets/${ticket}`);
  if (!safeReturnUrl(value)) throw new Error("平台返回入口无效");
  return value;
}

let pendingNavigation: Promise<PlatformNavigation> | null | undefined;
export function navigationFromLaunch(): Promise<PlatformNavigation> | null {
  if (pendingNavigation === undefined) {
    const ticket = consumePlatformTicket();
    pendingNavigation = ticket ? resolvePlatformNavigation(ticket) : null;
  }
  return pendingNavigation;
}
