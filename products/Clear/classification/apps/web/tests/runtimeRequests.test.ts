import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { readRuntime, verifyDevice, RUNTIME_CHECK_TIMEOUT_MS } from '../src/runtimeRequests';

const runtime = {
  python: '3.12', data_dir: 'F:/test', free_bytes: 1000,
  packages: { torch: '2.9', 'onnxruntime-gpu': '1.23' }, gpu: null,
};
const response = (value: unknown) => ({ ok: true, json: async () => value });

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

test('a hanging runtime HTTP request reaches its deadline, aborts and releases waiting state', async () => {
  const fetch = vi.fn(() => new Promise(() => {}));
  vi.stubGlobal('fetch', fetch);
  let waiting = true;
  const request = readRuntime().finally(() => { waiting = false; });
  const rejected = expect(request).rejects.toMatchObject({ code: 'timeout' });
  await vi.advanceTimersByTimeAsync(RUNTIME_CHECK_TIMEOUT_MS - 1);
  expect(waiting).toBe(true);
  await vi.advanceTimersByTimeAsync(1);
  await rejected;
  expect(waiting).toBe(false);
  expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});

test('the deadline also covers a response whose JSON body never completes', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: () => new Promise(() => {}) })));
  const rejected = expect(readRuntime({ refresh: true })).rejects.toMatchObject({ code: 'timeout' });
  await vi.advanceTimersByTimeAsync(RUNTIME_CHECK_TIMEOUT_MS);
  await rejected;
  expect(vi.getTimerCount()).toBe(0);
});

test('query or component cancellation settles immediately even when fetch ignores its signal', async () => {
  const fetch = vi.fn(() => new Promise(() => {}));
  vi.stubGlobal('fetch', fetch);
  const controller = new AbortController();
  let waiting = true;
  const request = readRuntime({ signal: controller.signal }).finally(() => { waiting = false; });
  const rejected = expect(request).rejects.toMatchObject({ name: 'AbortError' });
  controller.abort(new DOMException('关闭表单', 'AbortError'));
  await rejected;
  expect(waiting).toBe(false);
  expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});

test('an already cancelled read never starts HTTP or leaves a deadline timer', async () => {
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
  const controller = new AbortController(); controller.abort();
  await expect(readRuntime({ signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' });
  expect(fetch).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});

test('manual refresh uses the same bounded reader and successful reads clear the deadline', async () => {
  const fetch = vi.fn(async () => response(runtime)); vi.stubGlobal('fetch', fetch);
  await expect(readRuntime({ refresh: true })).resolves.toEqual(runtime);
  expect(fetch.mock.calls[0][0]).toBe('/api/v1/runtime?refresh_devices=true');
  expect(fetch.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  expect(vi.getTimerCount()).toBe(0);
});

test('GPU preflight timeout never reaches the business submission', async () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
  const submit = vi.fn();
  const request = verifyDevice('training', 'cuda').then(submit);
  const rejected = expect(request).rejects.toMatchObject({ code: 'timeout' });
  await vi.advanceTimersByTimeAsync(RUNTIME_CHECK_TIMEOUT_MS);
  await rejected;
  expect(submit).not.toHaveBeenCalled();
});

test('unknown GPU readiness never reaches submission after a completed HTTP response', async () => {
  const unknown = { available: null, reason: '暂时无法核实' };
  vi.stubGlobal('fetch', vi.fn(async () => response({ ...runtime, devices: {
    training: { framework: 'PyTorch', cpu: unknown, cuda: unknown },
    inference: { framework: 'ONNX Runtime', cpu: unknown, cuda: unknown },
  } })));
  const submit = vi.fn();
  await expect(verifyDevice('training', 'cuda').then(submit)).rejects.toMatchObject({ code: 'unverified' });
  expect(submit).not.toHaveBeenCalled();
});

test('explicit CPU checks installed dependencies without requesting a GPU probe', async () => {
  const fetch = vi.fn(async () => response(runtime)); vi.stubGlobal('fetch', fetch);
  await expect(verifyDevice('training', 'cpu')).resolves.toBeUndefined();
  await expect(verifyDevice('inference', 'cpu')).resolves.toBeUndefined();
  expect(fetch.mock.calls.map(call => call[0])).toEqual(['/api/v1/runtime', '/api/v1/runtime']);
  expect(vi.getTimerCount()).toBe(0);
});

test('CPU dependency reads also have a shorter deadline and never silently submit', async () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
  const submit = vi.fn();
  const rejected = expect(verifyDevice('inference', 'cpu').then(submit)).rejects.toMatchObject({ code: 'timeout' });
  await vi.advanceTimersByTimeAsync(10000);
  await rejected;
  expect(submit).not.toHaveBeenCalled();
});

test('CPU submission rejects missing required framework dependencies', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ ...runtime, packages: {} })));
  await expect(verifyDevice('training', 'cpu')).rejects.toMatchObject({ code: 'unavailable' });
  await expect(verifyDevice('inference', 'cpu')).rejects.toMatchObject({ code: 'unavailable' });
});
