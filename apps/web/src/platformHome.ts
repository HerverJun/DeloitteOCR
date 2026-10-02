const ID = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const STORAGE_KEY = 'clear-platform-home';
type HomeContext = { instance_id: string; home_url: string };
type Identity = { app_id: string; instance_id: string };

export function platformHomeUrl(returnUrl: string): string | null {
  try {
    const url = new URL(returnUrl);
    const port = Number(url.port);
    const workspace = url.searchParams.get('workspace');
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password
      || !Number.isInteger(port) || port < 1 || port > 65535 || url.hash
      || url.pathname.startsWith('//') || url.searchParams.size > (workspace === null ? 0 : 1)
      || (workspace !== null && !ID.test(workspace))) return null;
    url.pathname = '/';
    return url.href;
  } catch { return null; }
}

function validContext(value: unknown): value is HomeContext {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const context = value as HomeContext;
  return Object.keys(context).length === 2 && typeof context.instance_id === 'string'
    && ID.test(context.instance_id) && typeof context.home_url === 'string'
    && platformHomeUrl(context.home_url) === context.home_url;
}

/** A launch hint supplies a destination, never credentials or project authority. */
export async function platformHomeFromLaunch(appId: string, identity: () => Promise<Identity>): Promise<string | null> {
  let context: HomeContext | null = null;
  let saved: string | null = null;
  try {
    const hash = new URLSearchParams(location.hash.slice(1));
    const hinted = hash.has('platform_home') || hash.has('platform_instance');
    if (hinted) {
      const homes = hash.getAll('platform_home'), instances = hash.getAll('platform_instance');
      hash.delete('platform_home'); hash.delete('platform_instance');
      history.replaceState(history.state, '', location.pathname + location.search + (hash.size ? `#${hash}` : ''));
      sessionStorage.removeItem(STORAGE_KEY);
      const value = { home_url: homes[0], instance_id: instances[0] };
      if (homes.length !== 1 || instances.length !== 1 || !validContext(value)) return null;
      context = value;
      saved = JSON.stringify(context);
      sessionStorage.setItem(STORAGE_KEY, saved);
    } else {
      saved = sessionStorage.getItem(STORAGE_KEY);
      if (saved) {
        const value = JSON.parse(saved);
        if (validContext(value)) context = value;
      }
    }
    if (context) {
      const current = await identity();
      if (current.app_id === appId && current.instance_id === context.instance_id) return context.home_url;
    }
  } catch { /* An unavailable identity or invalid cache cannot establish a platform link. */ }
  try {
    if (sessionStorage.getItem(STORAGE_KEY) === saved) sessionStorage.removeItem(STORAGE_KEY);
  } catch { /* Optional storage. */ }
  return null;
}
