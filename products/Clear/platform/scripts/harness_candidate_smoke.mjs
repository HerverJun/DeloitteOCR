/** Exercise packaged Harness using deterministic callbacks and denied networking. */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { mkdir, writeFile, realpath } from 'node:fs/promises'
import { resolve, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const [candidateArgument, outputArgument] = process.argv.slice(2)
const candidate = await realpath(candidateArgument)
const output = await realpath(outputArgument)
if (!/^[Ff]:[\\/]/.test(candidate) || !/^[Ff]:[\\/]/.test(output) || output.startsWith(candidate + '\\')) throw new Error('Smoke paths must be separate F directories')
assert.equal(process.versions.node, '24.22.0')
const hostFile = resolve(candidate, 'platform/agent_harness/host.mjs')
const guardFile = fileURLToPath(new URL('./harness_offline_guard.mjs', import.meta.url))
const fixture = resolve(output, 'runtime')
for (const part of ['sessions', 'temp', 'cache', 'cache/appdata', 'cache/localappdata']) await mkdir(resolve(fixture, part), { recursive: true })
const environment = { ...process.env, TEMP: resolve(fixture, 'temp'), TMP: resolve(fixture, 'temp'), TMPDIR: resolve(fixture, 'temp'),
  APPDATA: resolve(fixture, 'cache/appdata'), LOCALAPPDATA: resolve(fixture, 'cache/localappdata'), XDG_CACHE_HOME: resolve(fixture, 'cache'),
  DSH_HOME: fixture, DSH_RESOURCE_CACHE: resolve(fixture, 'cache'), DSH_PRIMARY_RUNTIME: '', CLEAR_HARNESS_SMOKE_OUTPUT: output }
let modelCalls = 0, toolCalls = 0, finished = false
const tools = [{ name: 'acceptance_read', description: 'Local deterministic acceptance read',
  parameters: { type: 'object', properties: {}, additionalProperties: false } }]
const response = (content, calls = []) => ({ role: 'assistant', protocol: 'openai_chat_completions', content, calls,
  private: {}, blocks: null, usage: { input_tokens: 100, output_tokens: 10 } })
const callbacks = async (method, params) => {
  if (method === 'clear/event') return {}
  if (method === 'clear/recovery') return { results: {} }
  if (method === 'clear/prepare') return finished ? { stop: true } : { system: 'OFFLINE_ACCEPTANCE_SYSTEM', facts: 'Pinned local callback evidence', tools, compact: false }
  if (method === 'clear/tool') {
    assert.equal(params.tool, 'acceptance_read'); toolCalls++
    return { status: 'success', summary: 'Local receipt persisted', data: { verified: true }, result_refs: [], artifact_refs: [], job_refs: [] }
  }
  if (method === 'clear/model') {
    assert.equal(params.purpose, 'conversation'); modelCalls++
    if (modelCalls === 1) return response('', [{ call_id: 'offline_read', tool: 'acceptance_read', arguments: {} }])
    assert.ok(params.messages.some(message => message.role === 'tool' && message.call_id === 'offline_read'))
    finished = true
    return response('Offline packaged runtime verified')
  }
  throw new Error('Unexpected acceptance callback: ' + method)
}

class Host {
  constructor(callback) {
    this.pending = new Map(); this.serial = 0; this.stderr = ''
    this.child = spawn(process.execPath, ['--import', pathToFileURL(guardFile).href, hostFile],
      { cwd: dirname(hostFile), env: environment, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
    this.closed = new Promise(resolve => this.child.on('close', (code, signal) => resolve({ code, signal })))
    this.child.stderr.on('data', value => { this.stderr = (this.stderr + value).slice(-16_384) })
    this.child.on('exit', code => { for (const pending of this.pending.values()) pending.reject(new Error('Harness child exited: ' + code + '\n' + this.stderr)); this.pending.clear() })
    createInterface({ input: this.child.stdout }).on('line', line => {
      const frame = JSON.parse(line)
      if (frame.method) {
        Promise.resolve(callback(frame.method, frame.params)).then(result => this.send({ id: frame.id, result }),
          error => this.send({ id: frame.id, error: { code: error.code || 'ACCEPTANCE_FAILURE', message: error.message } }))
      } else {
        const waiting = this.pending.get(frame.id)
        if (!waiting) return
        this.pending.delete(frame.id)
        if (frame.error) waiting.reject(new Error(frame.error.message + '\n' + this.stderr)); else waiting.resolve(frame.result)
      }
    })
  }
  send(frame) { this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', version: 1, ...frame }) + '\n') }
  async call(method, params = {}) {
    const id = 'smoke' + ++this.serial
    let timeout
    try {
      return await new Promise((resolve, reject) => {
        this.pending.set(id, { resolve, reject })
        timeout = setTimeout(() => { this.child.kill(); reject(new Error('Harness acceptance RPC timeout')) }, 30_000)
        this.send({ id, method, params })
      })
    } finally { clearTimeout(timeout); this.pending.delete(id) }
  }
  initialize(importOnly = false) { return this.call('initialize', { version: 1, root: resolve(fixture, 'sessions'), cwd: fixture,
    session_id: 'offlineAcceptance', model: 'offline-local', protocol: 'openai_chat_completions',
    profile: { effective_context_window: 1_000_000, output_reserve: 4096, safety_margin_tokens: 2048 },
    system: 'OFFLINE_ACCEPTANCE_SYSTEM', history: [], tools, import_only: importOnly }) }
  async close() {
    try { await this.call('shutdown') } finally { this.child.stdin.end() }
    const exit = await this.closed
    assert.equal(exit.code, 0, this.stderr)
  }
}

let host, reopened
try {
  host = new Host(callbacks)
  const initialized = await host.initialize()
  assert.equal(initialized.harness_version, '0.2.1-alpha.4')
  await host.call('prompt', { id: 'offlineUser', content: 'Read the local receipt and finish' })
  const history = await host.call('history_events', { after: 0, limit: 500 })
  assert.equal(modelCalls, 2); assert.equal(toolCalls, 1)
  assert.ok(history.items.some(event => event.type === 'tool/result'))
  await host.close(); host = null
  reopened = new Host(async () => { throw new Error('Read-only history resume must not execute callbacks') })
  await reopened.initialize(true)
  const persisted = await reopened.call('history_events', { after: 0, limit: 500 })
  assert.deepEqual(persisted.items.slice(0, history.items.length), history.items)
  await reopened.close(); reopened = null
  const receipt = { format: 'clear-harness-offline-smoke-v1', passed: true, node: process.versions.node,
    harness_version: initialized.harness_version, model_calls: modelCalls, tool_calls: toolCalls,
    persisted_events: history.items.length, network: 'denied', business_data_sent: false,
    child_filesystem: 'JavaScript file-write guard: output only; actual fsync preserved',
    native_addons: ['locked @koromix/koffi-win32-x64 for Windows file locking'], shared_program_modified: false }
  await writeFile(resolve(output, 'offline-smoke.json'), JSON.stringify(receipt, null, 2) + '\n', 'utf8')
  process.stdout.write(JSON.stringify(receipt) + '\n')
} finally {
  if (host) host.child.kill()
  if (reopened) reopened.child.kill()
}
