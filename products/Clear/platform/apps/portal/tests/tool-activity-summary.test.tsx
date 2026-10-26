import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ToolActivity } from '../src/features/agent/ToolActivity'
import { projectConversation } from '../src/features/agent/conversationProjection'
import type { AgentEvent } from '../src/features/agent/api'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
})
afterEach(cleanup)
const event = (seq: number, payload: Record<string, unknown>): AgentEvent => ({
  seq, kind: 'tool_activity', run_id: 'run', created: '2026-10-25T05:00:00Z',
  payload: { version: 1, generation: 1, call_id: 'read-file', operation_id: 'original-operation', tool: 'read', ...payload },
})

test('file reads show a concise purpose and filename while the original output remains in call details', () => {
  const path = 'F:\\OCR\\output\\preview-main-20261024\\workspace-user\\agent\\workspaces\\session\\inputs\\asset\\dialog-input.csv'
  const raw = `<path>${path}</path>\n<type>file</type>\n<content>\n1: 供应商编号,金额\n2: 00137,12.30\n</content>`
  const events = [event(1, { phase: 'started', status: 'running', summary: '工具开始执行' }),
    event(2, { phase: 'finished', status: 'success', summary: raw })]
  const item = projectConversation(events).items[0]
  if (item.type !== 'activity') throw new Error('Missing activity')
  render(<MemoryRouter><ToolActivity item={item} /></MemoryRouter>)
  const toggle = screen.getByRole('button', { name: /已完成 1 项/ })
  expect(toggle).toHaveTextContent('读取文件 · dialog-input.csv')
  expect(toggle).not.toHaveTextContent(path)
  expect(toggle).not.toHaveTextContent('00137')
  fireEvent.click(toggle)
  const row = document.querySelector('[data-call-id="read-file"]') as HTMLElement
  expect(within(row).getByText('读取文件 · dialog-input.csv')).toBeVisible()
  expect(within(row).getByText('已完成')).toBeVisible()
  const details = row.querySelector('details')!
  expect(details).not.toHaveAttribute('open')
  fireEvent.click(within(details).getByText('调用详情'))
  expect(details).toHaveAttribute('open')
  expect(details.querySelector('pre')?.textContent).toBe(JSON.stringify(events.map(value => value.payload), null, 2))
  expect(item.tools[0]).toMatchObject({ phase: 'finished', status: 'success', summary: raw, operationId: 'original-operation' })
})

test('a blocked read with a raw path remains visibly unexecuted and preserves its original reason', () => {
  const raw = 'Access denied: F:\\OCR\\output\\private\\input.csv'
  const item = projectConversation([event(1, { phase: 'blocked', status: 'error', summary: raw })]).items[0]
  if (item.type !== 'activity') throw new Error('Missing activity')
  render(<MemoryRouter><ToolActivity item={item} /></MemoryRouter>)
  const row = document.querySelector('[data-call-id="read-file"]') as HTMLElement
  expect(within(row).getByText('读取文件')).toBeVisible()
  expect(within(row).getByText('未执行')).toBeVisible()
  expect(screen.queryByRole('button', { name: /已完成/ })).not.toBeInTheDocument()
  expect(row.querySelector('.agent-tool-heading')).not.toHaveTextContent('F:\\OCR')
  const details = row.querySelector('details')!
  fireEvent.click(within(details).getByText('调用详情'))
  expect(details.querySelector('pre')?.textContent).toContain('Access denied:')
  expect(item.tools[0]).toMatchObject({ phase: 'blocked', status: 'error', summary: raw })
})

test('multiline business errors keep their actionable reason in the default summary', () => {
  const summary = '金额列包含无法解析的值。\n请核对第 3 行后继续。'
  const item = projectConversation([event(1, { tool: 'data_compare', phase: 'finished', status: 'error', summary })]).items[0]
  if (item.type !== 'activity') throw new Error('Missing activity')
  render(<MemoryRouter><ToolActivity item={item} /></MemoryRouter>)
  const heading = document.querySelector('.agent-tool-heading') as HTMLElement
  expect(heading).toHaveTextContent('金额列包含无法解析的值。 请核对第 3 行后继续。')
  expect(heading).not.toHaveTextContent('工具操作')
  expect(screen.queryByRole('button', { name: /已完成/ })).not.toBeInTheDocument()
  expect(item.tools[0]).toMatchObject({ phase: 'finished', status: 'error', summary })
})
