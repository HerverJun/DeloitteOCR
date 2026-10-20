import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ToolActivity, groupToolSteps } from '../src/features/agent/ToolActivity'
import { useConversationScroll } from '../src/features/agent/useConversationScroll'
import { AnimatedDisclosure } from '../src/features/agent/conversationMotion'
import type { ActivityGroup, ToolStep } from '../src/features/agent/conversationProjection'

const originalAnimate = Object.getOwnPropertyDescriptor(Element.prototype, 'animate')
let frames: Map<number, FrameRequestCallback>, frameId: number, observeResize: () => void
beforeEach(() => {
  frames = new Map(); frameId = 0; observeResize = () => {}
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => { frames.set(++frameId, callback); return frameId }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id: number) => frames.delete(id)))
  vi.stubGlobal('ResizeObserver', class { constructor(callback: () => void) { observeResize = callback } observe() {} disconnect() {} unobserve() {} })
})
afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals()
  if (originalAnimate) Object.defineProperty(Element.prototype, 'animate', originalAnimate)
  else Reflect.deleteProperty(Element.prototype, 'animate')
})
function flushFrame() {
  const callbacks = [...frames.values()]; frames.clear()
  act(() => { callbacks.forEach(callback => callback(performance.now() + 250)) })
}
const tool = (id: string, phase = 'finished', status = 'success'): ToolStep => ({ id, callId: id, operationId: `op-${id}`, tool: 'data_compare', phase, status, summary: `步骤 ${id}`, created: '2026-10-16T00:00:00Z', seq: 1, jobRefs: [], events: [] })
const activity = (tools: ToolStep[]): ActivityGroup => ({ id: 'activity:run', type: 'activity', seq: 1, runId: 'run', generation: 1, created: '2026-10-16T00:00:00Z', tools, jobs: [], diagnostics: [] })

test('only consecutive successful operations fold, while exceptions keep their original position and visibility', () => {
  const tools = [tool('a'), tool('b'), tool('c', 'pending', 'pending'), tool('d', 'blocked', 'error'), tool('e', 'finished', 'needs_verification'), tool('f')]
  expect(groupToolSteps(tools).map(run => [run.complete, run.tools.map(step => step.id)])).toEqual([[true, ['a', 'b']], [false, ['c']], [false, ['d']], [false, ['e']], [true, ['f']]])
  render(<MemoryRouter><ToolActivity item={activity(tools)} /></MemoryRouter>)
  expect(screen.getByRole('button', { name: /已完成 2 项/ })).toHaveAttribute('aria-expanded', 'false')
  expect(screen.getByText('步骤 c')).toBeVisible(); expect(screen.getByText('步骤 d')).toBeVisible(); expect(screen.getByText('步骤 e')).toBeVisible()
  expect(document.querySelector('[data-call-id="a"]')).not.toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: /已完成 2 项/ }))
  expect(document.querySelector('[data-call-id="a"]')).toBeVisible()
  expect([...document.querySelectorAll('[data-call-id]')].map(row => row.getAttribute('data-call-id'))).toEqual(['a', 'b', 'c', 'd', 'e', 'f'])
})

test('a manually expanded group stays open when the next running operation completes and joins it', () => {
  const view = render(<MemoryRouter><ToolActivity item={activity([tool('a'), tool('b', 'started', 'running')])} /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: /已完成 1 项/ }))
  view.rerender(<MemoryRouter><ToolActivity item={activity([tool('a'), tool('b')])} /></MemoryRouter>)
  expect(screen.getByRole('button', { name: /已完成 2 项/ })).toHaveAttribute('aria-expanded', 'true')
  expect(document.querySelector('[data-call-id="b"]')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: /已完成 2 项/ }))
  expect(document.querySelector('[data-call-id="b"]')).not.toBeVisible()
})

test('live completion reclaims the running row height gradually and rapid group changes continue from its visual height', async () => {
  let animatedList: HTMLElement | undefined, visualHeight: number | undefined
  const bounds = HTMLElement.prototype.getBoundingClientRect
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (!this.classList.contains('agent-tool-runs')) return bounds.call(this)
    const height = animatedList === this && visualHeight !== undefined ? visualHeight : [...this.children].reduce((sum, child) => sum + (child.classList.contains('agent-tool-completed-run') ? 40 : 120), 0)
    return { top: 0, bottom: height, height, width: 840 } as DOMRect
  })
  const transitions: { resolve: () => void; cancel: ReturnType<typeof vi.fn> }[] = []
  const animate = vi.fn(function (this: HTMLElement) {
    let resolve!: () => void
    const finished = new Promise<void>(done => { resolve = done }), cancel = vi.fn(() => { visualHeight = undefined })
    animatedList = this; visualHeight = 90; transitions.push({ resolve, cancel })
    return { finished, cancel }
  })
  Object.defineProperty(Element.prototype, 'animate', { configurable: true, value: animate })
  const view = render(<MemoryRouter><ToolActivity item={activity([tool('a', 'started', 'running')])} /></MemoryRouter>)
  expect(animate).not.toHaveBeenCalled()
  view.rerender(<MemoryRouter><ToolActivity item={activity([tool('a')])} /></MemoryRouter>)
  expect(animate).toHaveBeenCalledWith([{ height: '120px' }, { height: '40px' }], expect.objectContaining({ duration: 180 }))
  view.rerender(<MemoryRouter><ToolActivity item={activity([tool('a'), tool('b', 'started', 'running')])} /></MemoryRouter>)
  expect(transitions[0].cancel).toHaveBeenCalled()
  expect(animate).toHaveBeenLastCalledWith([{ height: '90px' }, { height: '160px' }], expect.objectContaining({ duration: 180 }))
  await act(async () => transitions[0].resolve())
  expect(document.querySelector<HTMLElement>('.agent-tool-runs')?.style.overflow).toBe('hidden')
  visualHeight = undefined; await act(async () => transitions[1].resolve())
  expect(document.querySelector<HTMLElement>('.agent-tool-runs')?.style.overflow).toBe('')
  view.unmount(); animate.mockClear()
  render(<MemoryRouter><ToolActivity item={activity([tool('a'), tool('b')])} /></MemoryRouter>)
  expect(animate).not.toHaveBeenCalled()
})

test('opening an active operation diagnostic keeps that operation expanded after completion', () => {
  const view = render(<MemoryRouter><ToolActivity item={activity([tool('a', 'started', 'running')])} /></MemoryRouter>)
  const details = document.querySelector('.agent-tool-details') as HTMLDetailsElement
  details.open = true; fireEvent(details, new Event('toggle', { bubbles: true }))
  view.rerender(<MemoryRouter><ToolActivity item={activity([tool('a')])} /></MemoryRouter>)
  expect(screen.getByRole('button', { name: /已完成 1 项/ })).toHaveAttribute('aria-expanded', 'true')
  expect(document.querySelector('.agent-tool-details')).toHaveAttribute('open')
})

test('disclosure transitions reverse safely and a stale closing animation cannot hide reopened content', async () => {
  const transitions: { resolve: () => void; cancel: ReturnType<typeof vi.fn> }[] = []
  const animate = vi.fn(() => {
    let resolve!: () => void
    const finished = new Promise<void>(done => { resolve = done }), cancel = vi.fn()
    transitions.push({ resolve, cancel }); return { finished, cancel }
  })
  Object.defineProperty(Element.prototype, 'animate', { configurable: true, value: animate })
  const view = render(<AnimatedDisclosure id="steps" open={false}><button>步骤详情</button></AnimatedDisclosure>)
  expect(document.getElementById('steps')).not.toBeVisible(); expect(animate).not.toHaveBeenCalled()
  view.rerender(<AnimatedDisclosure id="steps" open><button>步骤详情</button></AnimatedDisclosure>)
  expect(document.getElementById('steps')).toBeVisible()
  view.rerender(<AnimatedDisclosure id="steps" open={false}><button>步骤详情</button></AnimatedDisclosure>)
  expect(document.getElementById('steps')).toHaveAttribute('aria-hidden', 'true')
  expect(document.getElementById('steps')).toHaveAttribute('inert')
  view.rerender(<AnimatedDisclosure id="steps" open><button>步骤详情</button></AnimatedDisclosure>)
  expect(transitions[0].cancel).toHaveBeenCalled(); expect(transitions[1].cancel).toHaveBeenCalled()
  await act(async () => transitions[1].resolve())
  expect(document.getElementById('steps')).toBeVisible()
  await act(async () => transitions[2].resolve())
  expect(document.getElementById('steps')).toBeVisible(); expect(document.getElementById('steps')).toHaveAttribute('aria-hidden', 'false')
})

function Reading({ scope = 'first', ids, stamp, live = false }: { scope?: string; ids: string[]; stamp: number; live?: boolean }) {
  const scroll = useConversationScroll(scope, ids, stamp, live)
  return <><div data-testid="scroll" ref={scroll.scroller} onScroll={scroll.onScroll} tabIndex={0}><div ref={scroll.content}>{ids.map(id => <div data-timeline-id={id} key={id}><p data-reading-anchor={`message:${id}`}>{id}</p></div>)}</div></div><span data-testid="follow">{String(scroll.following)}</span><span data-testid="unread">{scroll.unread}</span><button onClick={scroll.jumpToLatest}>回到最新</button></>
}
function dimensions(scroller: HTMLElement) {
  Object.defineProperties(scroller, { scrollHeight: { configurable: true, value: 1000 }, clientHeight: { configurable: true, value: 200 } })
}

test('reading gestures cancel follow immediately, including inside the near-bottom tolerance and a pending jump', () => {
  const view = render(<Reading ids={['one']} stamp={1} />), scroller = screen.getByTestId('scroll')
  dimensions(scroller); scroller.scrollTop = 800; fireEvent.scroll(scroller)
  fireEvent.wheel(scroller, { deltaY: -10 }); expect(screen.getByTestId('follow')).toHaveTextContent('false')
  scroller.scrollTop = 790; fireEvent.scroll(scroller)
  view.rerender(<Reading ids={['one', 'two']} stamp={2} />)
  flushFrame()
  expect(scroller.scrollTop).toBe(790); expect(screen.getByTestId('unread')).toHaveTextContent('1')
  fireEvent.click(screen.getByRole('button', { name: '回到最新' })); expect(screen.getByTestId('follow')).toHaveTextContent('true')
  fireEvent.keyDown(scroller, { key: 'PageUp' }); flushFrame()
  expect(scroller.scrollTop).toBe(790); expect(screen.getByTestId('follow')).toHaveTextContent('false')
  fireEvent.touchStart(scroller); expect(screen.getByTestId('follow')).toHaveTextContent('false')
})

test('a disclosure resize preserves the current reading anchor on the next shared frame', () => {
  render(<Reading ids={['one']} stamp={1} />)
  flushFrame()
  const scroller = screen.getByTestId('scroll'), row = document.querySelector('[data-reading-anchor]') as HTMLElement
  dimensions(scroller)
  vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue({ top: 0, bottom: 200, height: 200 } as DOMRect)
  let shift = 0
  vi.spyOn(row, 'getBoundingClientRect').mockImplementation(() => ({ top: 20 - (scroller.scrollTop - 180) + shift, bottom: 120 - (scroller.scrollTop - 180) + shift, height: 100 } as DOMRect))
  scroller.scrollTop = 180; fireEvent.scroll(scroller)
  shift = -60; act(() => observeResize())
  expect(scroller.scrollTop).toBe(180)
  flushFrame()
  expect(scroller.scrollTop).toBe(120); expect(row.getBoundingClientRect().top).toBe(20)
  // The browser delivers the scroll callback after the next animation tick has changed layout.
  shift = -80; fireEvent.scroll(scroller); act(() => observeResize()); flushFrame()
  expect(scroller.scrollTop).toBe(100); expect(row.getBoundingClientRect().top).toBe(20)
  // An actual scrollbar/reading gesture can establish a new anchor offset.
  fireEvent.pointerDown(scroller); scroller.scrollTop = 90; fireEvent.scroll(scroller)
  shift = -100; act(() => observeResize()); flushFrame()
  expect(scroller.scrollTop).toBe(70); expect(row.getBoundingClientRect().top).toBe(30)
})

test('only new live row IDs enter; loading history, changing sessions and token updates do not replay motion', () => {
  const animate = vi.fn(function (this: HTMLElement) { return { cancel: vi.fn(), finished: new Promise<void>(() => {}), node: this.dataset.timelineId } })
  Object.defineProperty(Element.prototype, 'animate', { configurable: true, value: animate })
  const view = render(<Reading ids={['history']} stamp={1} />)
  view.rerender(<Reading ids={['history']} stamp={1} live />)
  expect(animate).not.toHaveBeenCalled()
  view.rerender(<Reading ids={['history', 'live']} stamp={2} live />)
  expect(animate).toHaveBeenCalledTimes(1)
  expect(animate.mock.results[0].value.node).toBe('live')
  expect(animate.mock.calls[0][0]).toEqual([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'translateY(0)' }])
  view.rerender(<Reading ids={['history', 'live']} stamp={3} live />)
  view.rerender(<Reading scope="second" ids={['other-history']} stamp={4} live />)
  expect(animate).toHaveBeenCalledTimes(1)
})

test('reduced motion renders new live content and expands tools directly', () => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  const animate = vi.fn(() => ({ cancel: vi.fn(), finished: new Promise<void>(() => {}) }))
  Object.defineProperty(Element.prototype, 'animate', { configurable: true, value: animate })
  const view = render(<Reading ids={['history']} stamp={1} live />)
  view.rerender(<Reading ids={['history', 'live']} stamp={2} live />)
  render(<MemoryRouter><ToolActivity item={activity([tool('a')])} /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: /已完成 1 项/ }))
  expect(document.querySelector('[data-call-id="a"]')).toBeVisible()
  expect(animate).not.toHaveBeenCalled()
})
