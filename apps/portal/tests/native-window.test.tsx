import { afterEach, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { nativeAddress, nativeTaskReturnAddress, useNativeWindow, type NativeTarget } from '../src/features/catalog/nativeWindow'
afterEach(() => { cleanup(); vi.restoreAllMocks() })
const target: NativeTarget = { url: 'http://127.0.0.1:12345/#platform_ticket=' + 't'.repeat(43), kind: 'ticket' }
test('verified native launch carries only validated conversation and workspace IDs', () => {
  const address = nativeAddress(target)
  const hash = new URLSearchParams(new URL(nativeTaskReturnAddress(address, '?workspace=w1&task_return=s1')).hash.slice(1))
  expect(hash.get('platform_ticket')).toBe('t'.repeat(43))
  expect(hash.get('platform_task_return')).toBe('s1')
  expect(hash.get('platform_task_workspace')).toBe('w1')
  expect(nativeTaskReturnAddress(address, '?workspace=w1&task_return=https://outside.example')).toBe(address)
})
function Control({ scope, resolve }: { scope: string; resolve: (signal: AbortSignal) => Promise<NativeTarget> }) {
  const native = useNativeWindow(scope)
  return <><button onClick={() => native.open(resolve)}>open</button><span>{native.opening ? 'opening' : 'idle'}</span><p>{native.error}</p></>
}
test('blocked or throwing windows do not submit navigation requests', () => {
  const resolve = vi.fn()
  vi.spyOn(window, 'open').mockReturnValue(null)
  render(<Control scope="A" resolve={resolve} />)
  fireEvent.click(screen.getByText('open'))
  expect(resolve).not.toHaveBeenCalled()
  expect(screen.getByText(/允许弹出窗口/)).toBeTruthy()
})
test.each(['scope change', 'unmount'])('%s aborts a project handoff and ignores its late response', async mode => {
  let finish!: (value: NativeTarget) => void
  let signal!: AbortSignal
  const resolve = vi.fn((value: AbortSignal) => { signal = value; return new Promise<NativeTarget>(r => { finish = r }) })
  const popup = { opener: null, close: vi.fn(), location: { replace: vi.fn() } }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  const view = render(<Control scope="A" resolve={resolve} />)
  fireEvent.click(screen.getByText('open')); fireEvent.click(screen.getByText('open'))
  await waitFor(() => expect(resolve).toHaveBeenCalledOnce())
  if (mode === 'unmount') view.unmount()
  else view.rerender(<Control scope="B" resolve={resolve} />)
  expect(signal.aborted).toBe(true)
  expect(popup.close).toHaveBeenCalled()
  await act(async () => { finish(target); await Promise.resolve() })
  expect(popup.location.replace).not.toHaveBeenCalled()
})
test.each(['http://127.0.0.1:65536/', 'http://localhost:1234/', 'http://127.0.0.1:1234/path',
  'http://127.0.0.1:1234/?token=secret', 'http://evil.invalid:1234/'])('rejects invalid native address %s', url => {
  expect(() => nativeAddress({ url, kind: 'base' })).toThrow()
})
test('rejects a session for a different live origin', () => {
  expect(() => nativeAddress({ url: 'http://127.0.0.1:1234/#token='+'s'.repeat(48), kind: 'session', baseUrl:'http://127.0.0.1:4321/' })).toThrow()
})
