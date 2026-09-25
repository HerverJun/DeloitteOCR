import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const marker = process.argv.indexOf('--run-root')
assert(marker >= 0 && process.argv[marker + 1], 'Expected --run-root')
const RUN = path.resolve(process.argv[marker + 1])
const PYTHON = path.join(RUN, 'workspaces/platform-venv/Scripts/python.exe')
const OUTPUT = path.join(RUN, 'portal/acceptance.json')
const PNG = path.join(RUN, 'portal/screenshots')
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'

const sha = bytes => createHash('sha256').update(bytes).digest('hex')
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))

async function startPlatform() {
  const child = spawn(PYTHON, ['-u', 'tests/e2e/platform_server.py', '--run-root', RUN], {
    cwd: ROOT, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' },
  })
  let stderr = ''
  child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk })
  const info = await new Promise((resolve, reject) => {
    let buffer = ''
    const timer = setTimeout(() => reject(new Error(`Platform startup timed out: ${stderr}`)), 30000)
    child.once('exit', code => { clearTimeout(timer); reject(new Error(`Platform exited ${code}: ${stderr}`)) })
    child.stdout.setEncoding('utf8').on('data', chunk => {
      buffer += chunk
      const end = buffer.indexOf('\n')
      if (end < 0) return
      clearTimeout(timer)
      try { resolve(JSON.parse(buffer.slice(0, end))) } catch (error) { reject(error) }
    })
  })
  assert(Number.isInteger(info.port) && info.port > 0 && info.port <= 65535)
  return { child, info, stderr: () => stderr }
}

async function json(url, options = {}) {
  const response = await fetch(url, options)
  if (!response.ok) throw new Error(`${options.method || 'GET'} ${url}: ${response.status} ${await response.text()}`)
  return response.json()
}

async function screenshot(page, name) {
  const filename = path.join(PNG, `${name}.png`)
  await page.waitForTimeout(200) // Allow the painted frame to follow the visible DOM assertion.
  const session = await page.context().newCDPSession(page)
  let bytes
  try {
    const captured = await session.send('Page.captureScreenshot', { format: 'png',
      captureBeyondViewport: false, fromSurface: true })
    bytes = Buffer.from(captured.data, 'base64')
    await writeFile(filename, bytes)
  } finally {
    await session.detach()
  }
  assert(bytes.length > 1000)
  return { path: path.relative(RUN, filename).replaceAll('\\', '/'), sha256: sha(bytes), bytes: bytes.length }
}

async function waitGone(pid, timeout = 10000) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    try { process.kill(pid, 0) } catch { return true }
    await pause(100)
  }
  return false
}

async function main() {
  await mkdir(PNG, { recursive: true })
  const record = { date: '2026-09-25', status: 'running', screenshots: [],
    launches: {}, returns: {}, console_errors: [], page_errors: [], download: null, cleanup: {} }
  let platform, browser, base
  const opened = []
  const track = page => {
    page.on('pageerror', error => record.page_errors.push(error.message))
    page.on('console', message => {
      if (message.type() === 'error') record.console_errors.push({ url: page.url(), text: message.text() })
    })
  }
  try {
    platform = await startPlatform()
    record.fixture = path.relative(RUN, platform.info.fixture).replaceAll('\\', '/')
    record.bundled_files = platform.info.bundled_files
    base = `http://127.0.0.1:${platform.info.port}`
    await json(`${base}/api/platform/v1/health`)
    browser = await chromium.launch({ executablePath: CHROME, headless: true,
      args: ['--disable-gpu', '--disable-gpu-compositing', '--no-first-run'] })
    record.browser = { engine: 'Chromium', version: browser.version(), executable: CHROME,
      flags: ['--disable-gpu', '--disable-gpu-compositing'] }
    const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1280, height: 820 } })
    context.setDefaultTimeout(12000)
    context.setDefaultNavigationTimeout(15000)
    context.on('page', track)
    const page = await context.newPage()
    await page.goto(`${base}/apps`, { waitUntil: 'networkidle' })
    assert.equal(await page.locator('.application-card').count(), 2)
    for (const appId of ['classification', 'ocr']) {
      const card = page.locator('.application-card').filter({ hasText: appId })
      await card.getByRole('button', { name: '启动并打开' }).waitFor()
      const popupPromise = page.waitForEvent('popup')
      await card.getByRole('button', { name: '启动并打开' }).click()
      const popup = await popupPromise
      opened.push(popup)
      await popup.waitForURL(/^http:\/\/127\.0\.0\.1:[0-9]+\//, { timeout: 30000 })
      const receipt = await json(`${base}/api/platform/v1/apps/${appId}/launches/current`)
      assert.equal(popup.url(), receipt.base_url)
      const identity = await json(`${receipt.base_url}api/platform/v1/identity`)
      assert.equal(identity.app_id, appId)
      assert.equal(identity.instance_id, receipt.instance_id)
      assert.equal(identity.nonce, receipt.handshake_nonce)
      assert.equal(receipt.ownership, 'owned')
      record.launches[appId] = { base_url: receipt.base_url, pid: receipt.pid,
        instance_id: receipt.instance_id, identity_version: identity.version, ownership: receipt.ownership }
    }
    record.screenshots.push(await screenshot(page, 'portal-apps'))
    console.log('T017 phase: both native apps launched and portal captured')

    const workspaces = []
    for (const name of ['验收工作空间 A', '验收工作空间 B']) {
      workspaces.push(await json(`${base}/api/platform/v1/workspaces`, { method: 'POST',
        headers: { Origin: base, 'Content-Type': 'application/json' },
        body: JSON.stringify({ schema_version: '1.0', name }) }))
    }
    const first = workspaces[0].workspace_id, second = workspaces[1].workspace_id
    await page.goto(`${base}/apps?workspace=${first}`, { waitUntil: 'networkidle' })
    await page.getByRole('combobox', { name: '当前工作空间' }).click()
    await page.getByText('验收工作空间 B', { exact: true }).last().click()
    await page.waitForURL(new RegExp(`workspace=${second}`))
    await page.reload({ waitUntil: 'networkidle' })
    assert(page.url().includes(`workspace=${second}`))
    record.workspace_switch = { first, second, refreshed_url: page.url() }
    console.log('T017 phase: workspace switched and refreshed')

    // The trusted local adapter submits the same bounded context that a
    // resolved platform bridge would supply. Native services mint/consume it.
    const classification = record.launches.classification
    const created = await json(`${classification.base_url}api/v1/projects`, { method: 'POST',
      headers: { Origin: classification.base_url.slice(0, -1), 'Content-Type': 'application/json',
                 'X-Platform-Request': '1' },
      body: JSON.stringify({ name: '浏览器验收项目', labels: [
        { label_id: 'first', display_name: 'First' }, { label_id: 'second', display_name: 'Second' }] }) })
    const classTicket = await json(`${classification.base_url}api/v1/platform/navigation/issue`, {
      method: 'POST', headers: { Origin: classification.base_url.slice(0, -1),
        'Content-Type': 'application/json', 'X-Platform-Request': '1',
        'X-Workbench-Launch-Nonce': (await json(`${classification.base_url}api/platform/v1/identity`)).nonce },
      body: JSON.stringify({ app_id: 'classification', instance_id: classification.instance_id,
        link_id: 'linkClassification', workspace_id: second, module_project_id: created.id,
        asset_id: null, return_route: '/apps/classification', platform_origin: base }) })
    const classPage = await context.newPage()
    opened.push(classPage)
    await classPage.goto(`${classification.base_url}#platform_ticket=${classTicket.ticket}`, { waitUntil: 'networkidle' })
    await classPage.getByRole('link', { name: /返回平台/ }).waitFor({ timeout: 15000 })
    assert(!classPage.url().includes('platform_ticket='))
    record.screenshots.push(await screenshot(classPage, 'classification-native'))
    let returnPromise = context.waitForEvent('page')
    await classPage.getByRole('link', { name: /返回平台/ }).click()
    const classReturn = await returnPromise
    opened.push(classReturn)
    await classReturn.waitForURL(`${base}/apps/classification?workspace=${second}`)
    record.returns.classification = classReturn.url()
    assert(opened[0].url().startsWith(classification.base_url))
    console.log('T017 phase: classification ticket returned')

    const ocr = record.launches.ocr
    const ocrIdentity = await json(`${ocr.base_url}api/platform/v1/identity`)
    const ocrTicket = await json(`${ocr.base_url}api/platform/navigation/issue`, { method: 'POST',
      headers: { Origin: ocr.base_url.slice(0, -1), 'Content-Type': 'application/json',
                 'X-Workbench-Launch-Nonce': ocrIdentity.nonce },
      body: JSON.stringify({ app_id: 'ocr', instance_id: ocr.instance_id,
        link_id: 'linkOcr', workspace_id: second, return_route: '/apps/ocr', platform_origin: base }) })
    const token = (await readFile(path.join(platform.info.fixture, 'bundles/ocr/token.txt'), 'utf8')).trim()
    const ocrPage = await context.newPage()
    opened.push(ocrPage)
    await ocrPage.goto(`${ocr.base_url}#token=${token}&platform_ticket=${ocrTicket.ticket}`, { waitUntil: 'networkidle' })
    await ocrPage.getByRole('link', { name: /返回平台/ }).waitFor({ timeout: 15000 })
    assert(!ocrPage.url().includes('platform_ticket=') && !ocrPage.url().includes('token='))
    record.screenshots.push(await screenshot(ocrPage, 'ocr-native'))
    returnPromise = context.waitForEvent('page')
    await ocrPage.getByRole('link', { name: /返回平台/ }).click()
    const ocrReturn = await returnPromise
    opened.push(ocrReturn)
    await ocrReturn.waitForURL(`${base}/apps/ocr?workspace=${second}`)
    record.returns.ocr = ocrReturn.url()
    assert(opened[1].url().startsWith(ocr.base_url))
    console.log('T017 phase: OCR ticket returned')
    const replay = await fetch(`${ocr.base_url}api/platform/navigation/tickets/${ocrTicket.ticket}`,
                               { headers: { Authorization: `Bearer ${token}` } })
    assert.equal(replay.status, 404)
    record.ticket_replay_rejected = true

    const downloadPromise = page.waitForEvent('download')
    await page.evaluate(() => { const a = document.createElement('a'); a.href = '/__acceptance/download';
      document.body.append(a); a.click(); a.remove() })
    const download = await downloadPromise
    const downloadBytes = await readFile(await download.path())
    assert.equal(downloadBytes.toString(), 'T017,real-http-browser\n')
    record.download = { name: download.suggestedFilename(), sha256: sha(downloadBytes), bytes: downloadBytes.length }

    const stopped = await json(`${base}/__acceptance/stop/ocr`)
    assert.equal(stopped.stopped, true)
    await page.reload({ waitUntil: 'networkidle' })
    await page.locator('.application-card').filter({ hasText: 'ocr' })
      .getByText(/暂不可用|尚未运行/).waitFor()
    record.loss = { app_id: 'ocr', recovered_card: true }
    console.log('T017 phase: native loss reflected in portal')
    assert.deepEqual(record.page_errors, [], `Unhandled page errors: ${record.page_errors}`)
    record.unhandled_console_errors = record.console_errors.filter(item =>
      !/Failed to load resource: the server responded with a status of (401|404)/.test(item.text))
    assert.deepEqual(record.unhandled_console_errors, [], 'Unhandled console errors')
    record.status = 'passed'
    console.log(`T017 browser acceptance passed: ${record.screenshots.length} screenshots, two native returns`)
  } catch (error) {
    record.status = 'failed'
    record.error = error.stack || String(error)
    throw error
  } finally {
    if (browser) {
      try { await browser.close(); record.cleanup.browser_closed = true }
      catch (error) { record.cleanup.browser_close_error = String(error) }
    }
    if (base) {
      try { await fetch(`${base}/__acceptance/shutdown`) } catch (error) { record.cleanup.shutdown_error = String(error) }
    }
    if (platform) {
      const stopped = await waitGone(platform.child.pid)
      if (!stopped) { platform.child.kill(); record.cleanup.forced_platform_stop = true }
      record.cleanup.platform_exited = await waitGone(platform.child.pid)
      try {
        await fetch(`${base}/api/platform/v1/health`, { signal: AbortSignal.timeout(500) })
        record.cleanup.platform_listener_closed = false
      } catch { record.cleanup.platform_listener_closed = true }
      record.cleanup.native_pids_exited = {}
      for (const [appId, receipt] of Object.entries(record.launches)) {
        record.cleanup.native_pids_exited[appId] = await waitGone(receipt.pid)
      }
      record.server_stderr = platform.stderr().slice(-3000)
    }
    if (record.status === 'passed' && (record.cleanup.browser_closed !== true ||
        record.cleanup.platform_exited !== true || record.cleanup.platform_listener_closed !== true ||
        Object.values(record.cleanup.native_pids_exited).some(value => value !== true))) {
      record.status = 'failed'
      record.error = 'Owned browser or service process remained after acceptance'
    }
    await writeFile(OUTPUT, JSON.stringify(record, null, 2) + '\n', 'utf8')
    if (record.status === 'failed' && record.error === 'Owned browser or service process remained after acceptance') {
      throw new Error(record.error)
    }
  }
  console.log('1 passed')
}

main().catch(error => { console.error(error.stack || error); process.exitCode = 1 })
