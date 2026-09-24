/** Isolated AgentPanel browser regressions; no real controller or OCR calls. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';
import { chromium, expect } from '@playwright/test';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = path.resolve(process.argv[2] || path.join(root, '../audit/ocr-agent-20260921-langgraph/frontend-final-20260923-behavior.json'));
const checks = [], errors = [];
const receipt = { started_at: new Date().toISOString(), checks, errors, fixture: 'Real AgentPanel/React and local Vite, intercepted synthetic HTTP, external headless Edge; no screenshots', limitations: ['No real backend/controller qualification', 'This supplements, and does not repeat or replace, browser QA09'] };
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0, open: false }, plugins: [{ name: 'agent-panel-test-fixture', configureServer(vite) {
  vite.middlewares.use(async (req, res, next) => {
    if (req.url?.split('?')[0] !== '/__agent_panel_test.html') return next();
    const html = `<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module">
      import React from 'react';
      import {createRoot} from 'react-dom/client';
      import {AgentPanel} from '/src/AgentPanel.tsx';
      createRoot(document.getElementById('root')).render(React.createElement(AgentPanel, {projectId:'project', selectedImageIds:[], documents:[], engines:[], onNavigate:async()=>{}}));
    </script></body></html>`;
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(await vite.transformIndexHtml(req.url, html));
  });
} }] });
let browser;
const run = (id = 'run-a', patch = {}) => ({ id, session_id: id === 'run-a' ? 'session-a' : 'session-b', project_id: 'project', goal: 'synthetic', status: 'running', outcome: null, generation: 1, last_event_seq: 10, coverage: {}, limits: {}, usage: {}, ...patch });
const session = id => ({ id, project_id: 'project', title: id, status: 'active', updated: '2026-09-23T00:00:00Z' });
async function fixture(options = {}) {
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const state = { a: run(), b: run('run-b', { status: 'waiting_jobs' }), requests: [], streamCount: 0, nextEvent: null, cancelRoute: null, externalRequests: [], pageErrors: [] };
  page.on('pageerror', error => state.pageErrors.push(String(error)));
  page.on('request', request => { if (!request.url().startsWith(base)) state.externalRequests.push(request.url()); });
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), endpoint = url.pathname.slice(4);
    state.requests.push({ endpoint, method: request.method(), body: request.postDataJSON() });
    const json = value => route.fulfill({ status: 200, json: value });
    if (endpoint === '/agent/status') return json({ enabled: true, available: options.available !== false });
    if (endpoint === '/projects/project/agent/sessions') return json({ sessions: [{ ...session('session-a'), status: options.archived ? 'archived' : 'active' }, session('session-b')] });
    if (endpoint === '/agent/connection') return json({ configured: true, available: options.connected !== false, revision: 1, model: 'Synthetic', base_url: 'http://127.0.0.1/fixture' });
    if (endpoint === '/multimodal/models') return json({ models: [] });
    if (endpoint === '/projects/project/agent/controller-authorization') return json({ authorized: options.authorized !== false, revision: 1 });
    if (endpoint === '/projects/project/agent/selection') return json({ selection_token: 'synthetic-selection' });
    if (endpoint === '/agent/sessions/session-a/messages') return json({ run: state.a });
    if (endpoint.endsWith('/cancel')) { state.cancelRoute = route; return; }
    if (/\/agent\/sessions\/session-[ab]$/.test(endpoint)) {
      const selected = endpoint.endsWith('session-a') ? state.a : state.b;
      return json({ runs: [selected], inbox: [], last_seq: selected.last_event_seq });
    }
    if (endpoint.endsWith('/events')) return json({ events: options.events || [], next_seq: 10 });
    if (endpoint.endsWith('/stream')) {
      state.streamCount++;
      if (options.expired) return route.fulfill({ status: 401, json: { detail: 'synthetic expired' } });
      const event = state.nextEvent;
      state.nextEvent = null;
      return route.fulfill({ contentType: 'text/event-stream', body: event ? `data: ${JSON.stringify(event)}\n\n` : ': heartbeat\n\n' });
    }
    return route.fulfill({ status: 404, json: { detail: 'Unexpected fixture request: ' + endpoint } });
  });
  try {
    await page.goto(base + '/__agent_panel_test.html#token=synthetic-local-test');
    await page.getByRole('button', { name: '文档助手', exact: true }).click();
    const panel = page.getByRole('complementary', { name: '文档助手' });
    await expect(panel.locator('.agent-run-state')).toContainText('正在处理');
    return { context, page, panel, state };
  } catch (error) {
    await context.close();
    throw Error(String(error) + '; page errors: ' + state.pageErrors.join('; '));
  }
}
async function check(name, work) {
  let current;
  try { current = await fixture(work.options); await work.run(current); assert.deepEqual(current.state.pageErrors, []); checks.push({ name, passed: true }); console.log('PASS ' + name); }
  catch (error) { errors.push({ name, error: String(error) }); checks.push({ name, passed: false }); console.error('FAIL ' + name + ': ' + error); }
  finally { await current?.context.close(); }
}
let base;
try {
  await server.listen();
  base = server.resolvedUrls.local[0].replace(/\/$/, '');
  browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--disable-gpu', '--disable-gpu-compositing'] });
  const delayed = async ({ panel, state }) => {
    await panel.getByRole('button', { name: '停止助手', exact: true }).click();
    await expect.poll(() => !!state.cancelRoute).toBe(true);
  };
  const release = async (state, value) => { await state.cancelRoute.fulfill({ status: 200, json: value }); };
  await check('Late control response cannot overwrite another session run', { run: async f => {
    await delayed(f);
    await f.panel.getByLabel('历史会话', { exact: true }).selectOption('session-b');
    await expect(f.panel.locator('.agent-run-state')).toContainText('等待后台任务');
    await release(f.state, run('run-a', { status: 'cancelled', last_event_seq: 11 }));
    await expect(f.panel.getByRole('button', { name: '停止助手', exact: true })).toBeEnabled();
    await expect(f.panel.locator('.agent-run-state')).toContainText('等待后台任务');
  } });
  for (const scenario of [
    { name: 'Late control response cannot overwrite newer generation', current: { status: 'waiting_user', generation: 2, last_event_seq: 20 }, late: { status: 'cancelled', generation: 1, last_event_seq: 999 }, label: '等待你的回复' },
    { name: 'Late control response cannot overwrite newer event sequence', current: { status: 'waiting_user', generation: 1, last_event_seq: 20 }, late: { status: 'cancelled', generation: 1, last_event_seq: 11 }, label: '等待你的回复' },
  ]) await check(scenario.name, { run: async f => {
    await delayed(f);
    await f.panel.getByLabel('历史会话', { exact: true }).selectOption('session-b');
    await expect(f.panel.locator('.agent-run-state')).toContainText('等待后台任务');
    f.state.a = run('run-a', scenario.current);
    await f.panel.getByLabel('历史会话', { exact: true }).selectOption('session-a');
    await expect(f.panel.locator('.agent-run-state')).toContainText(scenario.label);
    await release(f.state, run('run-a', scenario.late));
    await expect(f.panel.getByRole('button', { name: '停止助手', exact: true })).toBeEnabled();
    await expect(f.panel.locator('.agent-run-state')).toContainText(scenario.label);
  } });
  await check('Current newer control response is applied', { run: async f => {
    await delayed(f);
    await release(f.state, run('run-a', { status: 'cancelled', last_event_seq: 11 }));
    await expect(f.panel.locator('.agent-run-state')).toContainText('已停止');
  } });
  await check('Late control response cannot populate a newly selected empty session', { run: async f => {
    await delayed(f);
    await f.panel.getByLabel('历史会话', { exact: true }).selectOption('');
    await expect(f.panel.locator('.agent-run-state')).toHaveCount(0);
    await release(f.state, run('run-a', { status: 'cancelled', last_event_seq: 11 }));
    await expect(f.panel.getByRole('button', { name: '新建', exact: true })).toBeEnabled();
    await expect(f.panel.locator('.agent-run-state')).toHaveCount(0);
  } });
  await check('E51 Enter sends the typed instruction', { run: async f => {
    await f.panel.getByLabel('本轮目标', { exact: true }).fill('keyboard send');
    await f.panel.getByLabel('本轮目标', { exact: true }).press('Enter');
    await expect.poll(() => f.state.requests.filter(r => r.endpoint.endsWith('/messages')).length, { timeout: 2000 }).toBe(1);
  } });
  await check('E51 Shift+Enter inserts a newline without sending', { run: async f => {
    const input = f.panel.getByLabel('本轮目标', { exact: true });
    await input.fill('first'); await input.press('Shift+Enter'); await input.press('x');
    await expect(input).toHaveValue('first\nx');
    assert.equal(f.state.requests.filter(r => r.endpoint.endsWith('/messages')).length, 0);
  } });
  for (const ime of [{ isComposing: true, keyCode: 13 }, { isComposing: false, keyCode: 229 }]) {
    await check('E51 IME Enter is preserved without sending: ' + JSON.stringify(ime), { run: async f => {
      const input = f.panel.getByLabel('本轮目标', { exact: true });
      await input.fill('输入中的中文');
      const unconsumed = await input.evaluate((element, composition) => element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, ...composition })), ime);
      assert.equal(unconsumed, true);
      assert.equal(f.state.requests.filter(r => r.endpoint.endsWith('/messages')).length, 0);
    } });
  }
  for (const blocked of [{ name: 'empty text', options: {} }, { name: 'missing permission', options: { authorized: false } }, { name: 'archived session', options: { archived: true } }, { name: 'unavailable controller', options: { connected: false } }, { name: 'unavailable runtime', options: { available: false } }]) {
    await check('E51 Enter respects send restrictions: ' + blocked.name, { options: blocked.options, run: async f => {
      const input = f.panel.getByLabel('本轮目标', { exact: true });
      if (blocked.name !== 'empty text') await input.fill('blocked instruction');
      await expect(f.panel.getByRole('button', { name: '追加指令', exact: true })).toBeDisabled();
      await input.press('Enter');
      assert.equal(f.state.requests.filter(r => r.endpoint.endsWith('/selection') || r.endpoint.endsWith('/messages')).length, 0);
      await expect(input).toHaveValue(blocked.name === 'empty text' ? '' : 'blocked instruction');
    } });
  }
  await check('E51 Repeated Enter in one event batch sends exactly one instruction', { run: async f => {
    const input = f.panel.getByLabel('本轮目标', { exact: true });
    await input.fill('one instruction');
    await input.evaluate(element => { for (let i = 0; i < 3; i++) element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })); });
    await expect(input).toHaveValue('');
    assert.equal(f.state.requests.filter(r => r.endpoint.endsWith('/messages')).length, 1);
    assert.equal(f.state.requests.filter(r => r.endpoint.endsWith('/selection')).length, 1);
  } });
  await check('E51 Escape closes the panel without cancelling its active run', { run: async f => {
    await f.panel.getByLabel('本轮目标', { exact: true }).focus();
    await f.page.keyboard.press('Escape');
    await expect(f.panel).toHaveCount(0);
    assert.equal(f.state.requests.filter(r => r.endpoint.endsWith('/cancel')).length, 0);
    await f.page.getByRole('button', { name: '文档助手', exact: true }).click();
    await expect(f.panel.locator('.agent-run-state')).toContainText('正在处理');
  } });
  await check('E51 Background progress stays in polite live region and preserves typing focus', { run: async f => {
    const input = f.panel.getByLabel('本轮目标', { exact: true });
    await input.fill('keep focus'); await input.focus();
    f.state.a = run('run-a', { last_event_seq: 11 });
    f.state.nextEvent = { session_id: 'session-a', run_id: 'run-a', generation: 1, seq: 11, type: 'job_progress', created: '2026-09-23T00:00:00Z', payload: { summary: 'synthetic background progress' } };
    await expect(f.panel.getByText('synthetic background progress', { exact: true })).toBeVisible({ timeout: 5000 });
    await expect(f.panel.locator('.agent-transcript')).toHaveAttribute('aria-live', 'polite');
    await expect(input).toBeFocused();
    await expect(input).toHaveValue('keep focus');
  } });
  const hostile = '<script>window.__agent_xss=true</script><img src="https://untrusted.invalid/probe" onerror="window.__agent_xss=true"> [run](javascript:window.__agent_xss=true) ![external](https://untrusted.invalid/image)';
  await check('E55 Untrusted model HTML, script URL and images remain inert text without external requests', { options: { events: [{ session_id: 'session-a', run_id: 'run-a', generation: 1, seq: 1, type: 'message', created: '2026-09-23T00:00:00Z', payload: { role: 'assistant', content: hostile } }] }, run: async f => {
    await expect(f.panel.locator('.agent-transcript')).toContainText(hostile);
    await expect(f.panel.locator('.agent-transcript script, .agent-transcript img, .agent-transcript a')).toHaveCount(0);
    assert.equal(await f.page.evaluate(() => window.__agent_xss), undefined);
    assert.deepEqual(f.state.externalRequests, []);
  } });
  await check('E12 401 expires authorization and stops SSE reconnection', { options: { expired: true }, run: async f => {
    await expect(f.panel.getByText('会话鉴权已失效，请从托盘重新打开工作台。', { exact: true })).toBeVisible();
    assert.equal(f.state.streamCount, 1);
    await f.page.waitForTimeout(3200);
    assert.equal(f.state.streamCount, 1);
    assert.equal(new URL(f.page.url()).hash, '');
    assert.ok(f.state.requests.every(r => !JSON.stringify(r).includes('synthetic-local-test')));
  } });
} finally {
  await browser?.close();
  await server.close();
  receipt.completed_at = new Date().toISOString();
  receipt.passed = errors.length === 0;
  receipt.shutdown = { browser_closed: true, vite_closed: true };
  await fs.mkdir(path.dirname(output), { recursive: true });
  await fs.writeFile(output, JSON.stringify(receipt, null, 2) + '\n');
}
if (errors.length) process.exitCode = 1;
