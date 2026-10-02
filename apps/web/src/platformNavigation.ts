const ID = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const TICKET = /^[A-Za-z0-9_-]{32,128}$/;
const ROUTE = /^\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*$/;
const FIELDS = ['app_id', 'instance_id', 'workspace_id', 'link_id', 'module_project_id', 'asset_id', 'return_url'] as const;

export type PlatformNavigation = {
  app_id: 'classification';
  instance_id: string;
  workspace_id: string;
  link_id: string;
  module_project_id: string;
  asset_id: string | null;
  return_url: string;
};

export function consumePlatformTicket(): string | null {
  const hash = new URLSearchParams(location.hash.slice(1));
  const tickets = hash.getAll('platform_ticket');
  const ticket = tickets[0] ?? null;
  if (ticket === null) return null;
  hash.delete('platform_ticket');
  history.replaceState(history.state, '', location.pathname + location.search + (hash.size ? `#${hash}` : ''));
  return tickets.length === 1 && TICKET.test(ticket) ? ticket : null;
}

export function safeReturnUrl(value: PlatformNavigation): string | null {
  if (!value || typeof value !== 'object' || value.app_id !== 'classification' || ![value.instance_id, value.link_id,
    value.workspace_id, value.module_project_id].every(id => ID.test(id))
    || (value.asset_id !== null && !ID.test(value.asset_id))) return null;
  try {
    const url = new URL(value.return_url);
    const port = Number(url.port);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !Number.isInteger(port)
      || port < 1 || port > 65535 || url.username || url.password || url.hash || !ROUTE.test(url.pathname)
      || url.searchParams.size !== 1 || url.searchParams.get('workspace') !== value.workspace_id) return null;
    return url.href;
  } catch { return null; }
}

export async function resolvePlatformNavigation(ticket: string): Promise<PlatformNavigation> {
  if (!TICKET.test(ticket)) throw new Error('平台入口无效');
  const response = await fetch(`/api/v1/platform/navigation/tickets/${ticket}`, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error('平台入口已失效');
  const value = await response.json() as PlatformNavigation;
  if (!safeReturnUrl(value)) throw new Error('平台返回入口无效');
  return value;
}

const STORAGE_KEY = 'prism-platform-navigation';
export function savedPlatformNavigation(): PlatformNavigation | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as PlatformNavigation;
    if (Object.keys(value).length === FIELDS.length && safeReturnUrl(value)) return value;
  } catch { /* Invalid or unavailable tab storage is never authoritative. */ }
  try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* no storage */ }
  return null;
}

export function savePlatformNavigation(value: PlatformNavigation): void {
  if (!safeReturnUrl(value)) return;
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(FIELDS.map(key => [key, value[key]])))); } catch { /* no storage */ }
}

async function restoreNavigation(value: PlatformNavigation): Promise<PlatformNavigation | null> {
  try {
    const response = await fetch('/api/platform/v1/identity', { headers: { Accept: 'application/json' } });
    if (response.ok) {
      const identity = await response.json();
      if (identity.app_id === value.app_id && identity.instance_id === value.instance_id) return value;
    }
  } catch { /* Cached navigation cannot establish a current service identity. */ }
  try { sessionStorage.removeItem(STORAGE_KEY); sessionStorage.removeItem('prism-platform-page'); } catch { /* no storage */ }
  return null;
}

let pendingNavigation: Promise<PlatformNavigation | null> | null | undefined;
export function navigationFromLaunch(): Promise<PlatformNavigation | null> | null {
  if (pendingNavigation === undefined) {
    const hasTicket = new URLSearchParams(location.hash.slice(1)).has('platform_ticket');
    const ticket = consumePlatformTicket();
    if (hasTicket) {
      try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* no storage */ }
      pendingNavigation = ticket ? resolvePlatformNavigation(ticket) : Promise.reject(new Error('平台入口无效，请从平台重新打开。'));
    } else {
      const value = savedPlatformNavigation();
      pendingNavigation = value ? restoreNavigation(value) : null;
    }
  }
  return pendingNavigation;
}
