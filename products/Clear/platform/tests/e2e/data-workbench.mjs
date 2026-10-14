import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const index = process.argv.indexOf('--run-root')
assert(index >= 0 && process.argv[index + 1], 'Expected --run-root')
const RUN = path.resolve(process.argv[index + 1])
const PYTHON = path.join(RUN, 'workspaces/platform-venv/Scripts/python.exe')
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
const hash = value => createHash('sha256').update(value).digest('hex')

async function startServer() {
  const child = spawn(PYTHON, ['-u', 'tests/e2e/data_e2e_server.py', '--run-root', RUN], {
    cwd: ROOT, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' },
  })
  let stderr = ''
  child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk })
  const info = await new Promise((resolve, reject) => {
    let output = ''
    const timer = setTimeout(() => reject(new Error(`Data platform startup timed out: ${stderr}`)), 45000)
    child.once('exit', code => { clearTimeout(timer); reject(new Error(`Data platform exited ${code}: ${stderr}`)) })
    child.stdout.setEncoding('utf8').on('data', chunk => {
      output += chunk
      const end = output.indexOf('\n')
      if (end < 0) return
      clearTimeout(timer)
      try { resolve(JSON.parse(output.slice(0, end))) } catch (error) { reject(error) }
    })
  })
  return { child, info }
}

async function read(origin, path) {
  const response = await fetch(origin + path)
  assert(response.ok, `GET ${path}: ${response.status}`)
  return response.json()
}

async function waitSession(origin, id, predicate) {
  for (let attempt = 0; attempt < 120; attempt++) {
    const value = await read(origin, `/api/platform/v1/data/sessions/${id}`)
    if (predicate(value)) return value
    await pause(200)
  }
  throw new Error('Data operation did not reach its expected state')
}

let server, browser
try {
  server = await startServer()
  const { origin, data_dir: dataDir } = server.info
  assert(await read(origin, '/api/platform/v1/data/health'))
  browser = await chromium.launch({ executablePath: CHROME, headless: true,
    args: ['--disable-gpu', '--disable-gpu-compositing', '--no-first-run'] })
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  await page.goto(origin + '/apps/data')
  await page.getByRole('heading', { name: '数据清洗与比对', exact: true }).waitFor()
  await page.getByLabel('新作业名称').fill('浏览器数据回归')
  await page.getByRole('button', { name: '新建作业', exact: true }).click()
  await page.waitForURL(/session=s[0-9a-f]+/)
  const id = new URL(page.url()).searchParams.get('session')
  assert(id)
  await page.getByLabel('上传左表').setInputFiles({ name: 'left.csv', mimeType: 'text/csv',
    buffer: Buffer.from('编号,金额,备注\nA,10.00,相同\nB,20.00,修改前\n') })
  await waitSession(origin, id, value => !!value.state.tables.left &&
    !value.jobs.some(job => ['queued', 'running'].includes(job.status)))
  await page.waitForFunction(() => !document.querySelector('input[aria-label="上传右表"]')?.disabled)
  await page.getByLabel('上传右表').setInputFiles({ name: 'right.csv', mimeType: 'text/csv',
    buffer: Buffer.from('编号,金额,备注\nA,10.00,相同\nB,21.00,修改后\n') })
  await waitSession(origin, id, value => !!value.state.tables.right &&
    !value.jobs.some(job => ['queued', 'running'].includes(job.status)))
  await page.reload()
  await page.getByRole('button', { name: '开始比对', exact: true }).waitFor()
  await page.getByLabel('金额容差', { exact: true }).fill('0.01')
  await page.locator('.data-pair').nth(1).getByRole('checkbox').check()
  await page.getByRole('button', { name: '开始比对', exact: true }).click()
  const compared = await waitSession(origin, id, value => !!value.state.result_id &&
    !value.jobs.some(job => ['queued', 'running'].includes(job.status)))
  assert.equal(compared.state.comparisons[0].coverage.same_pairs, 1)
  assert.equal(compared.state.comparisons[0].coverage.different_pairs, 1)
  await page.reload()
  await page.getByRole('button', { name: '开始比对', exact: true }).waitFor()
  assert.equal(await page.getByLabel('金额容差', { exact: true }).inputValue(), '0.01')
  await page.getByRole('button', { name: '生成下载文件', exact: true }).click()
  const exported = await waitSession(origin, id, value => value.state.exports.length > 0 &&
    !value.jobs.some(job => ['queued', 'running'].includes(job.status)))
  const summary = exported.state.exports[0].files.find(file => file.name === 'summary.json')
  assert(summary)
  await page.reload()
  const downloaded = page.waitForEvent('download')
  await page.getByRole('link', { name: /^summary.json/ }).first().click()
  const file = await downloaded
  const saved = path.join(dataDir, 'downloaded-summary.json')
  await file.saveAs(saved)
  assert.equal(hash(await readFile(saved)), summary.sha256)
  await page.screenshot({ path: path.join(dataDir, 'data-workbench.png'), fullPage: true })
  assert.deepEqual(errors, [])
  const record = { status: 'passed', origin, session_id: id,
    comparison_id: compared.state.result_id, export_id: exported.state.exports[0].id,
    summary_sha256: summary.sha256, errors,
    checks: ['UI creates independent data session', 'UI uploads both CSV files',
      'UI compares real rows', 'draft survives reload', 'UI exports and downloads fixed summary'] }
  await writeFile(path.join(dataDir, 'report.json'), JSON.stringify(record, null, 2))
  console.log(JSON.stringify({ ...record, evidence_dir: dataDir }))
} finally {
  if (browser) await browser.close()
  if (server) {
    server.child.kill()
    await new Promise(resolve => { if (server.child.exitCode !== null) resolve()
      else { server.child.once('exit', resolve); setTimeout(resolve, 10000) } })
  }
}
