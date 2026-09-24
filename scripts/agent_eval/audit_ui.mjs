import { chromium, expect as baseExpect } from '../../frontend/node_modules/@playwright/test/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';

const out = path.resolve(process.argv[2]);
const seed = JSON.parse(await fs.readFile(path.join(out, 'seed.json'), 'utf8'));
const expect = baseExpect.configure({ timeout: 15000 });
const checks = [], errors = [];
const api = async (url, method = 'GET', body) => {
  const response = await fetch(seed.base + '/api' + url, { method, headers: { Authorization: 'Bearer ' + seed.token, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw Error(await response.text());
  return response.json();
};
const record = text => { checks.push(text); console.log(text); };
const browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--disable-gpu', '--disable-gpu-compositing'] });
const context = await browser.newContext({ viewport: { width: 1366, height: 900 }, acceptDownloads: true });
const page = await context.newPage();
page.on('pageerror', error => errors.push(String(error)));
const panel = page.getByRole('complementary', { name: '文档助手' });
const open = async target => { await target.getByRole('button', { name: '文档助手', exact: true }).click(); };
let currentGoal = '';
const send = async goal => {
  currentGoal = goal;
  await panel.getByLabel('本轮目标', { exact: true }).fill(goal);
  await panel.getByRole('button', { name: '发送', exact: true }).click();
  await expect(panel.locator('.agent-user').getByText(goal, { exact: true })).toBeVisible();
};
const completed = async tag => {
  if (['S01', 'S04'].includes(tag)) {
    // Both partial scenarios have the same factual heading. Scope the answer
    // after this request so S04 cannot pass on S01's already-rendered answer.
    const lastAnswer = panel.locator('.agent-user').filter({ hasText: currentGoal })
      .locator('xpath=following-sibling::div[contains(@class, "agent-assistant")]').last();
    // These scenarios execute several native PDF pages on the real CPU worker.
    // Keep UI assertions short, but allow the batch itself a bounded minute.
    await expect(lastAnswer).toContainText('本轮未完整完成。', { timeout: 60000 });
    await expect(lastAnswer).toContainText('导出产物：已保存 1 份，失败或不可用 1 份。');
    await expect(panel.locator('.agent-run-state')).toContainText('有未覆盖内容');
    await expect(panel.getByText(tag + ' 合成场景已完成', { exact: true })).toHaveCount(0);
  } else {
    await expect(panel.getByText(tag + ' 合成场景已完成', { exact: true })).toBeVisible();
  }
  await expect(panel.locator('.agent-run-state')).toContainText('已结束');
};
try {
  await page.goto(seed.base + '/#token=' + seed.token);
  await expect(page.getByLabel('当前识别结果', { exact: true })).toHaveValue(seed.result);
  await open(page);
  await panel.getByRole('button', { name: '允许此地址读取当前项目', exact: true }).click();
  await send('S02 核对小数合计');
  await completed('S02');
  await expect(panel.getByText(/计算合计：0.30 · 差额：0.01/)).toBeVisible();
  await panel.getByRole('button', { name: '定位核对单元格' }).first().click();
  await expect(page.getByRole('tab', { name: '表格 1', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('已选中：表 1 · 第 4 行 / 第 2 列', { exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(out, '01-financial.png'), animations: 'disabled' });
  record('S02: real Decimal check, visible difference, signed evidence and cell navigation; original preserved');

  await send('S05 查找应收账款');
  await completed('S05');
  await panel.getByRole('button', { name: /第 1 页 · 采用结果/ }).last().click();
  record('S05: adopted-content search and version-checked evidence navigation');

  await send('EXPORT 保存工作簿');
  await completed('EXPORT');
  const downloadPromise = page.waitForEvent('download');
  await panel.getByRole('button', { name: '下载导出文件', exact: true }).click();
  const download = await downloadPromise;
  await download.saveAs(path.join(out, 'export.xlsx'));
  expect((await fs.stat(path.join(out, 'export.xlsx'))).size).toBeGreaterThan(1000);
  record('Real adopted-result XLSX export and leased download');

  await send('INBOX 等待补充');
  await expect(panel.getByText('请选择下一步', { exact: true })).toBeVisible();
  await panel.getByLabel('本轮目标', { exact: true }).fill('追加说明，仅汇报状态');
  await panel.getByRole('button', { name: '追加指令', exact: true }).click();
  await completed('INBOX');
  await expect(panel.getByText(/已在安全边界处理/)).toBeVisible();
  record('Active inbox replaces clarification at a safe boundary without implicit permission');

  await send('S06 中断后继续');
  await expect(panel.getByText('请选择下一步', { exact: true })).toBeVisible();
  await api('/audit/restart-agent', 'POST', {});
  await expect(panel.getByText('重启后等待继续', { exact: true })).toBeVisible();
  const before = (await api('/audit/fixture')).requests.length;
  await panel.getByRole('button', { name: '继续', exact: true }).click();
  await expect(panel.getByText('等待你的回复', { exact: true })).toBeVisible();
  expect((await api('/audit/fixture')).requests.length).toBe(before);
  await panel.getByRole('button', { name: '继续核对', exact: true }).click();
  await completed('S06');
  record('S06: actual runtime close/open, explicit resume and rebound decision; resume alone issues no model call');

  await panel.getByText('范围、审校与预算', { exact: true }).click();
  await panel.getByLabel('追加文档范围（支持未渲染页面）', { exact: true }).selectOption(seed.pdfs.S01.id);
  await panel.getByLabel('文档页码，例如 1-3,8', { exact: true }).fill('1-3');
  await panel.locator('form summary').click();
  await panel.getByLabel('允许处理所选页面', { exact: true }).check();
  await send('S01 批量处理原生 PDF 并导出工作簿');
  await completed('S01');
  const batch = (await api('/audit/fixture')).stages.filter(stage => stage.document_id === seed.pdfs.S01.id);
  expect(batch).toHaveLength(3); expect(batch.every(stage => stage.status === 'succeeded')).toBe(true);
  record('S01 limited: three real native PDF pages processed; empty-table XLSX rejected, TXT fallback exported. Table XLSX verified separately');
  await panel.getByLabel('追加文档范围（支持未渲染页面）', { exact: true }).selectOption(seed.pdfs.S04.id);
  await panel.getByLabel('文档页码，例如 1-3,8', { exact: true }).fill('1-2');
  await panel.getByLabel('允许本轮按原范围重试失败任务（仍受预算限制）', { exact: true }).check();
  await send('S04 仅重试失败页并导出');
  await completed('S04');
  const retried = (await api('/audit/fixture')).stages.filter(stage => stage.document_id === seed.pdfs.S04.id);
  expect(retried).toHaveLength(2); expect(retried.every(stage => stage.status === 'succeeded')).toBe(true);
  record('S04: injected second-page failure, explicit subset retry, real native completion/TXT export; no duplicate successful-page stage');
  await panel.getByLabel('追加文档范围（支持未渲染页面）', { exact: true }).selectOption('');
  await panel.getByText('所选采用结果的审校权限', { exact: true }).click();
  await panel.getByLabel('允许使用的视觉审校模型', { exact: true }).selectOption(seed.visual_model);
  await panel.getByLabel('允许本轮向该视觉连接发送上述内容', { exact: true }).check();
  await send('S03 局部视觉审校');
  await completed('S03');
  expect((await api('/audit/fixture')).edited_text).toContain('001.00');
  await panel.getByRole('button', { name: '查看视觉建议', exact: true }).last().click();
  await expect(page.getByRole('tab', { name: '视觉审校', exact: true })).toHaveAttribute('aria-selected', 'true');
  await panel.getByRole('button', { name: '关闭助手面板', exact: true }).click();
  const review = page.getByRole('region', { name: '多模态二轮审校', exact: true });
  await expect(review.getByRole('button', { name: '采用这条修改', exact: true })).toBeEnabled();
  await page.screenshot({ path: path.join(out, '03-visual-proposal.png'), animations: 'disabled' });
  await review.getByRole('button', { name: '采用这条修改', exact: true }).click();
  await expect.poll(async () => (await api('/audit/fixture')).edited_text).toContain('001.05');
  await page.getByRole('button', { name: '撤销', exact: true }).click();
  await expect.poll(async () => (await api('/audit/fixture')).edited_text).toContain('001.00');
  await open(page);
  await send('S03-REJECT 再次审校后拒绝');
  await completed('S03-REJECT');
  await panel.getByRole('button', { name: '查看视觉建议', exact: true }).last().click();
  await panel.getByRole('button', { name: '关闭助手面板', exact: true }).click();
  await review.getByRole('button', { name: '拒绝此建议', exact: true }).click();
  await expect(review.getByText('已拒绝这条建议，识别内容未修改。', { exact: true })).toBeVisible();
  record('S03: scoped local visual protocol fixture, signed proposal navigation, explicit adoption, undo and rejection');
  await open(page);

  await page.reload(); await open(page);
  await expect(panel.getByRole('button', { name: '允许此地址读取当前项目' })).toHaveCount(0);
  await expect(panel.getByText('S06 合成场景已完成', { exact: true })).toBeVisible();
  const second = await context.newPage();
  second.on('pageerror', error => errors.push(String(error)));
  await second.goto(seed.base + '/#token=' + seed.token); await open(second);
  await expect(second.getByRole('complementary', { name: '文档助手' }).getByText('S06 合成场景已完成', { exact: true })).toBeVisible();
  await panel.getByText('会话管理', { exact: true }).click();
  await panel.getByLabel('会话名称', { exact: true }).fill('已验收的合成会话');
  await panel.getByRole('button', { name: '保存名称', exact: true }).click();
  await expect(panel.getByLabel('历史会话')).toContainText('已验收的合成会话');
  await panel.getByRole('button', { name: '归档会话', exact: true }).click();
  await panel.getByLabel('本轮目标', { exact: true }).fill('归档会话不允许发送');
  await expect(panel.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  await panel.getByRole('button', { name: '恢复会话', exact: true }).click();
  await expect(panel.getByRole('button', { name: '发送', exact: true })).toBeEnabled();
  await second.close();
  record('Reload restores history and consent; second window reads same history; rename/archive/restore controls');
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.screenshot({ path: path.join(out, '02-small-laptop.png'), animations: 'disabled' });
  const fixture = await api('/audit/fixture');
  expect(fixture.edited_text).toBe(fixture.original_text);
  const long = await api('/audit/long-history', 'POST', {});
  let streams = 0;
  const historyRequests = [];
  page.on('request', request => { if (request.url().includes(`/agent/sessions/${long.session_id}/events?`)) historyRequests.push(request.url()); });
  await page.route('**/agent/sessions/*/stream?*', route => { streams++; return streams === 1 ? route.abort('failed') : route.continue(); });
  await page.reload(); await open(page);
  await expect(panel.getByLabel('历史会话')).toHaveValue(long.session_id);
  await expect(panel.getByText('历史记录 100000', { exact: true })).toBeAttached();
  expect(await panel.locator('.agent-message').count()).toBe(500);
  await expect.poll(() => streams).toBeGreaterThanOrEqual(2);
  await expect(panel.getByText('进度已连接', { exact: true })).toBeVisible();
  await expect(panel.getByText(/进度连接暂时中断/)).toHaveCount(0);
  await panel.getByRole('button', { name: '加载更早记录', exact: true }).click();
  await expect(panel.getByText('历史记录 99001', { exact: true })).toBeAttached();
  expect(historyRequests[0]).toContain('after_seq=99500');
  await panel.getByRole('button', { name: '更多历史会话', exact: true }).click();
  await expect(panel.getByLabel('历史会话').locator('option')).toHaveCount(53);
  record('100k-event history opens latest 500, pages backward, reconnects SSE without replay; 52 sessions remain accessible');
  expect(errors).toEqual([]);
  await fs.writeFile(path.join(out, 'receipt.json'), JSON.stringify({ passed: true, checks, errors, fixture,
    limits: ['synthetic controller and local visual protocol fixture', 'S01 PDF native output has no adopted tables; batch XLSX remains untested, separate table XLSX passes', 'runtime restart is not OS process crash', 'no target-device or cloud qualification'] }, null, 2));
} catch (error) {
  await page.screenshot({ path: path.join(out, 'failure.png'), animations: 'disabled' });
  await fs.writeFile(path.join(out, 'failure.txt'), String(error) + '\n' + await page.locator('body').innerText());
  throw error;
} finally {
  await context.close(); await browser.close();
}
