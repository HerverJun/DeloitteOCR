import { chromium } from 'file:///F:/OCR/output/clear-automation-dev-20261002/worktrees/ocr/frontend/node_modules/playwright/index.mjs'
import fs from 'node:fs/promises'
import path from 'node:path'

const root = process.env.CLEAR_TASK_ROOT
if (!root || !path.resolve(root).toUpperCase().startsWith('F:\\')) throw new Error('Use an F-drive task root')
const privateLogin = JSON.parse(await fs.readFile(path.join(root, 'runs/demo-login-private.json'), 'utf8'))
const base = process.env.CLEAR_BROWSER_BASE || 'http://127.0.0.1:8889'
const profile = path.join(root, 'cache/browser-profile')
const context = await chromium.launchPersistentContext(profile, {
  executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  downloadsPath: path.join(root, 'temp/browser-downloads'), viewport: { width: 1440, height: 1000 },
  ignoreHTTPSErrors: true, args: ['--disable-gpu', '--disable-gpu-compositing', '--disable-crash-reporter', '--disk-cache-dir=' + path.join(root, 'cache/browser-disk')],
})
const errors = []
const page = await context.newPage()
page.on('pageerror', error => errors.push(error.message))
try {
  await page.goto(base + '/jobs')
  await page.waitForFunction(() => document.querySelector('input[type="password"]') || document.querySelector('.task-history'))
  if (await page.getByLabel('账号', { exact: true }).isVisible()) {
    await page.getByLabel('账号', { exact: true }).fill(privateLogin.username)
    await page.getByLabel('密码', { exact: true }).fill(privateLogin.password)
    await page.locator('form button[type="submit"]').click()
  }
  await page.getByRole('heading', { name: '任务中心', exact: true }).waitFor()
  await page.locator('.ant-table-row').first().waitFor()
  await page.screenshot({ path: path.join(root, 'evidence/task-center.png'), fullPage: true })
  await page.locator('.task-history').getByRole('button', { name: /详\s*情/ }).first().click()
  await page.getByRole('tab', { name: /执行记录/ }).waitFor()
  await page.waitForFunction(() => { const panel = document.querySelector('.ant-drawer-content-wrapper'); if (!panel) return false; const rect = panel.getBoundingClientRect(); return rect.width > 600 && rect.left >= 0 && rect.right <= innerWidth + 1 && panel.getAnimations().every(animation => animation.playState === 'finished'); })
  await page.screenshot({ path: path.join(root, 'evidence/task-detail.png'), fullPage: true })
  if (errors.length) throw new Error('Browser errors: ' + errors.join('; '))
  await fs.writeFile(path.join(root, 'evidence/browser-check.json'), JSON.stringify({ passed: true, base, screenshots: ['task-center.png', 'task-detail.png'], browserErrors: errors }, null, 2))
} finally { await context.close() }
