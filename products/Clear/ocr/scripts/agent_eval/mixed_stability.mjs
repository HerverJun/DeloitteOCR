import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const seed = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const { chromium, expect: baseExpect } = await import(pathToFileURL(seed.playwright).href);
const expect = baseExpect.configure({ timeout: 30000 });
const api = async (route, body) => {
  const response = await fetch(seed.base + '/api/audit/' + route, {
    method: body ? 'POST' : 'GET', headers: { Authorization: 'Bearer ' + seed.token, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw Error(await response.text());
  return response.json();
};
const clients = [];
try {
  for (let n = 0; n < 2; n++) {
    const browser = await chromium.launch({ channel: 'msedge', headless: true,
      args: ['--disable-gpu', '--disable-gpu-compositing', '--disable-background-networking'] });
    const context = await browser.newContext({ viewport: { width: 1366, height: 900 } });
    await context.route('**/*', route => route.request().url().startsWith(seed.base + '/') ? route.continue() : route.abort());
    const page = await context.newPage();
    const client = { id: n+1, browser, context, page, errors: [], streams: 0, cycles: 0, session: seed.sessions[n] };
    clients.push(client);
    page.on('pageerror', error => client.errors.push(String(error)));
    page.on('request', request => { if (request.url().includes('/stream?')) client.streams++; });
    await page.goto(seed.base + '/#token=' + seed.token);
    await page.getByRole('button', { name: '文档助手', exact: true }).click();
    client.panel = page.getByRole('complementary', { name: '文档助手' });
    await client.panel.getByLabel('历史会话', { exact: true }).selectOption(client.session);
    await expect(client.panel.getByText('进度已连接', { exact: true })).toBeVisible();
  }
  await fs.writeFile(path.join(seed.output, 'ui-ready.json'), JSON.stringify({ clients: clients.map(c => ({ id: c.id, session_id: c.session, streams: c.streams })), actual_backend: true }));
  await Promise.all(clients.map(async client => {
    while (true) {
      const control = await api('control');
      if (control.stop) break;
      if (control.active) {
        const goal = 'UI-' + client.id + '-' + client.cycles;
        const start = performance.now();
        await client.panel.getByLabel('本轮目标', { exact: true }).fill(goal);
        await client.panel.getByRole('button', { name: '发送', exact: true }).click();
        await expect(client.panel.getByText('MIXED-DONE ' + goal, { exact: true })).toBeVisible();
        await expect(client.panel.locator('.agent-run-state')).toContainText('已结束');
        await api('ui-receipt', { client: client.id, latency_ms: performance.now() - start, goal,
          session_id: client.session, page_errors: client.errors, sse_requests: client.streams });
        client.cycles++;
      }
      for (let tick = 0; tick < 10; tick++) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        if ((await api('control')).stop) return;
      }
    }
  }));
  for (const client of clients) {
    expect(client.errors).toEqual([]);
    expect(client.cycles).toBeGreaterThan(0);
    expect(client.streams).toBeGreaterThan(0);
    await client.page.screenshot({ path: path.join(seed.output, `ui-${client.id}-final.png`), animations: 'disabled' });
  }
  await fs.writeFile(path.join(seed.output, 'ui-result.json'), JSON.stringify({ passed: true,
    clients: clients.map(c => ({ id: c.id, cycles: c.cycles, streams: c.streams, errors: c.errors })) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(seed.output, 'ui-failure.txt'), String(error));
  throw error;
} finally {
  for (const client of clients) { await client.context.close(); await client.browser.close(); }
}
