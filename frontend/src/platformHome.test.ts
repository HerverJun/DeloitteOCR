import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { platformHomeFromLaunch, platformHomeUrl } from './platformHome';

let entries: Map<string, string>;
const home = 'http://127.0.0.1:9123/?workspace=workspaceA';
beforeEach(() => {
  entries = new Map();
  vi.stubGlobal('location', { hash: '', pathname: '/', search: '' });
  vi.stubGlobal('history', { state: null, replaceState: vi.fn((_state, _title, address: string) => {
    location.hash = new URL(address, 'http://127.0.0.1:5555').hash;
  }) });
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => entries.set(key, value),
    removeItem: (key: string) => entries.delete(key),
  });
});
afterEach(() => vi.unstubAllGlobals());

it('links to the homepage while preserving the workspace and actual platform port', () => {
  expect(platformHomeUrl('http://127.0.0.1:9123/apps/ocr/workflow?workspace=workspaceA')).toBe(home);
  expect(platformHomeUrl('http://127.0.0.1:9123/')).toBe('http://127.0.0.1:9123/');
  for (const url of ['https://example.invalid/', 'http://127.0.0.1:9123@evil.invalid/',
    'http://127.0.0.1:9123/?token=secret', home+'#secret', home+'&workspace=workspaceB',
    'http://127.0.0.1:65536/', 'http://127.0.0.1:9123/?workspace=../escape'])
    expect(platformHomeUrl(url)).toBeNull();
});

it('consumes the hint, preserves the session fragment, and verifies the same instance on refresh', async () => {
  location.hash = '#token='+'s'.repeat(32)+'&'+new URLSearchParams({platform_home:home,platform_instance:'instanceA'});
  const identity = vi.fn().mockResolvedValue({app_id:'ocr',instance_id:'instanceA'});
  expect(await platformHomeFromLaunch('ocr',identity)).toBe(home);
  expect(location.hash).toBe('#token='+'s'.repeat(32));
  expect(entries.get('clear-platform-home')).not.toContain('token');
  expect(await platformHomeFromLaunch('ocr',identity)).toBe(home);
  expect(identity).toHaveBeenCalledTimes(2);
});

it.each([{app_id:'ocr',instance_id:'instanceOther'},{app_id:'classification',instance_id:'instanceA'}])(
  'clears the home link when the native identity changes', async current => {
    entries.set('clear-platform-home',JSON.stringify({home_url:home,instance_id:'instanceA'}));
    expect(await platformHomeFromLaunch('ocr',async()=>current)).toBeNull();
    expect(entries.has('clear-platform-home')).toBe(false);
  });

it('an unavailable identity or an invalid new hint does not revive an old destination', async () => {
  entries.set('clear-platform-home',JSON.stringify({home_url:home,instance_id:'instanceA'}));
  location.hash = '#platform_home=https%3A%2F%2Fevil.invalid%2F&platform_instance=instanceA';
  const identity = vi.fn();
  expect(await platformHomeFromLaunch('ocr',identity)).toBeNull();
  expect(identity).not.toHaveBeenCalled();
  expect(entries.has('clear-platform-home')).toBe(false);
  entries.set('clear-platform-home',JSON.stringify({home_url:home,instance_id:'instanceA'}));
  expect(await platformHomeFromLaunch('ocr',async()=>{throw new Error('offline')})).toBeNull();
  expect(entries.has('clear-platform-home')).toBe(false);
});
