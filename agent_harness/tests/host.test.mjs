import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const taskRoot = 'F:/OCR/output/agent-harness-upgrade-20261015'
const fixtureRoot = resolve(taskRoot, 'evidence/host-tests')
const hostPath = fileURLToPath(new URL('../host.mjs', import.meta.url))
const tool = name => ({ name, description: `Declared ${name}`, parameters: { type: 'object', properties: {}, additionalProperties: false } })
const result = value => ({ status: 'success', summary: 'Verified result', data: { value }, result_refs: [], artifact_refs: [], job_refs: [] })
const answer = (content = '', calls = []) => ({ role: 'assistant', content, calls, usage: { input_tokens: 100, output_tokens: 20 }, protocol: 'openai_chat_completions', private: {}, blocks: null })
const profile = { effective_context_window: 1000000, output_reserve: 4096, safety_margin_tokens: 2048 }

class Host {
  constructor(child, callback) {
    this.child = child; this.callback = callback; this.serial = 0; this.pending = new Map(); this.events = []; this.stderr = ''
    child.stdin.on('error', () => {})
    child.stderr.on('data', value => { this.stderr += value })
    createInterface({ input: child.stdout }).on('line', line => { void this.receive(JSON.parse(line)) })
    child.on('exit', code => { for (const request of this.pending.values()) request.reject(new Error(`Host exit ${code}: ${this.stderr}`)); this.pending.clear() })
  }
  static async open(callback, root) {
    await mkdir(fixtureRoot, { recursive: true })
    root ||= await mkdtemp(resolve(fixtureRoot, 'case-'))
    const env = { ...process.env, TEMP: resolve(taskRoot, 'temp'), TMP: resolve(taskRoot, 'temp'), TMPDIR: resolve(taskRoot, 'temp'),
      APPDATA: resolve(taskRoot, 'cache/appdata'), LOCALAPPDATA: resolve(taskRoot, 'cache/localappdata'), XDG_CACHE_HOME: resolve(taskRoot, 'cache'),
      DSH_HOME: root, DSH_RESOURCE_CACHE: resolve(taskRoot, 'cache/dsh'), DSH_PRIMARY_RUNTIME: '' }
    const child = spawn(process.execPath, [hostPath], { cwd: resolve(hostPath, '..'), env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
    const host = new Host(child, callback); host.root = root
    return host
  }
  async receive(frame) {
    if (frame.method) {
      try {
        if (frame.method === 'clear/event') this.events.push(frame.params.event)
        const value = frame.method === 'clear/event' ? {} : await this.callback(frame.method, frame.params)
        this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', version: 1, id: frame.id, result: value }) + '\n')
      } catch (error) {
        this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', version: 1, id: frame.id, error: { code: error.code || 'TEST_FAILURE', message: error.message,
          ...(error.response_diagnostics ? { response_diagnostics: error.response_diagnostics } : {}) } }) + '\n')
      }
    } else {
      const request = this.pending.get(frame.id)
      if (!request) return
      this.pending.delete(frame.id)
      if (frame.error) request.reject(Object.assign(new Error(frame.error.message + '\n' + this.stderr), { code: frame.error.code })); else request.resolve(frame.result)
    }
  }
  call(method, params = {}) {
    const id = 'test-' + ++this.serial
    const waiting = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }))
    this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', version: 1, id, method, params }) + '\n')
    return waiting
  }
  initialize(extra = {}) { return this.call('initialize', { version: 1, root: resolve(this.root, 'sessions'), cwd: this.root, session_id: 'stableSession',
    model: 'engineering-controller', protocol: 'openai_chat_completions', profile, system: 'INITIAL_SYSTEM', history: [], ...extra }) }
  async allEvents() {
    const events = []; let after = 0
    while (true) {
      const page = await this.call('history_events', { after, limit: 7 })
      events.push(...page.items)
      if (!page.items.length || page.next_cursor >= page.total) break
      assert.ok(page.next_cursor > after)
      after = page.next_cursor
    }
    return events
  }
  async close() { try { await this.call('shutdown') } finally { this.child.stdin.end() } }
}

test('first assembly, activated schemas and reclaimed schemas match persisted request headers and current system', { timeout: 30000 }, async () => {
  const schemas = [tool('catalog_load'), tool('business_inspect')]
  const requests = [], preparations = []
  let completed = false
  const host = await Host.open(async (method, params) => {
    if (method === 'clear/recovery') return { results: {} }
    if (method === 'clear/prepare') {
      if (completed) return { stop: true }
      preparations.push(params)
      const index = requests.length
      return { system: `CURRENT_SYSTEM_${index}`, facts: `FACTS_${index}`, tools: index === 1 ? schemas : [schemas[0]], compact: false }
    }
    if (method === 'clear/tool') return result(params.tool)
    if (method === 'clear/model') {
      assert.equal(params.purpose, 'conversation')
      const index = requests.length
      assert.equal(params.system, `CURRENT_SYSTEM_${index}`)
      assert.deepEqual(params.tools, index === 1 ? schemas : [schemas[0]])
      assert.ok(params.messages.some(message => message.role === 'user' && message.content.includes(`FACTS_${index}`)))
      requests.push(params)
      if (index < 2) return answer('', [{ call_id: `call_${index}`, tool: index ? 'business_inspect' : 'catalog_load', arguments: {} }])
      completed = true
      return answer('Completed with verified tool results')
    }
    throw new Error(`Unexpected callback ${method}`)
  })
  try {
    await host.initialize({ tools: [schemas[0]] })
    await host.call('prompt', { id: 'fresh-user', content: 'Use existing tools to finish the task' })
    assert.equal(requests.length, 3)
    assert.ok(preparations[0].incoming.some(message => message.id === 'fresh-user'))
    const events = await host.allEvents()
    const headers = events.filter(event => event.type === 'request/header')
    assert.deepEqual(headers.map(event => event.data.header.tools), [[schemas[0]], schemas, [schemas[0]]])
    const systems = events.filter(event => event.type === 'system/message').map(event => event.data.message.content[0].text)
    for (let index = 0; index < 3; index++) assert.ok(systems.includes(`CURRENT_SYSTEM_${index}`))
    assert.equal(events.filter(event => event.type === 'tool/result').length, 2)
    assert.ok(events.some(event => event.type === 'turn/start' && event.data.turn === 2))
    await writeFile(resolve(host.root, 'assembly-verified.json'), JSON.stringify({ requests, headers, count: events.length }, null, 2))
  } finally { await host.close() }
})

test('import-only materializes closed protected history without model tools or callbacks and resumes at the next turn', { timeout: 30000 }, async () => {
  const callback = async method => { throw new Error(`Import must not call ${method}`) }
  const host = await Host.open(callback)
  const history = [{ id: 'legacy-user', role: 'user', content: 'Keep row identifiers and source hashes', migration_source: { original_event_seq: 17, sha256: 'b'.repeat(64) } },
    { id: 'legacy-model', role: 'assistant', content: '', calls: [{ call_id: 'legacy-call', tool: 'data_export', arguments: {} }], migration_source: { run_id: 'originalRun' } },
    { id: 'legacy-tool', role: 'tool', call_id: 'legacy-call', result: result('originalArtifact'), migration_source: { run_id: 'originalRun', replayed: false } }]
  let imported
  try {
    await host.initialize({ import_only: true, history })
    imported = await host.allEvents()
    assert.equal(host.events.length, 0)
    assert.equal(imported.find(event => event.type === 'system/message').data.message.content[0].text, 'INITIAL_SYSTEM')
    assert.equal(imported.find(event => event.type === 'tool/result').data.meta.migration_source.run_id, 'originalRun')
    assert.equal(imported.find(event => event.type === 'user/message').data.source.migration_source.original_event_seq, 17)
    assert.deepEqual(imported.filter(event => event.type === 'turn/end').map(event => event.data), [{ turn: 1, reason: { kind: 'completed' } }])
    await assert.rejects(host.call('prompt', { content: 'Do not run' }), /Import-only/)
  } finally { await host.close() }
  let modelCalls = 0
  const resumed = await Host.open(async (method, params) => {
    if (method === 'clear/recovery') return { results: {} }
    if (method === 'clear/prepare') return { system: 'RESUMED_SYSTEM', facts: '', tools: [], compact: false }
    if (method === 'clear/model') { modelCalls++; assert.ok(params.messages.some(message => message.id === 'legacy-user')); return answer('Resumed') }
    throw new Error(`Unexpected callback ${method}`)
  }, host.root)
  try {
    await resumed.initialize()
    await resumed.call('prompt', { id: 'new-user', content: 'Continue the original task' })
    const events = await resumed.allEvents()
    assert.deepEqual(events.slice(0, imported.length), imported)
    assert.equal(modelCalls, 1)
    assert.deepEqual(events.filter(event => event.type === 'turn/start').map(event => event.data.turn), [1, 2])
  } finally { await resumed.close() }
})

test('imported and live model protocols survive restart without being relabelled by the current connection', { timeout: 30000 }, async () => {
  const signedBlocks = [{ type: 'thinking', thinking: 'Prior provider reasoning', signature: 'original-signature' }, { type: 'text', text: 'Prior answer' }]
  const imported = await Host.open(async method => { throw new Error(`Import must not call ${method}`) })
  try {
    await imported.initialize({ import_only: true, history: [
      { id: 'known-protocol', role: 'assistant', content: 'Prior answer', calls: [], protocol: 'anthropic_messages', blocks: signedBlocks, private: { original: true } },
      { id: 'unknown-protocol', role: 'assistant', content: 'Legacy source with no protocol', calls: [] },
    ] })
    const history = (await imported.call('history')).messages
    assert.equal(history.find(message => message.id === 'known-protocol').protocol, 'anthropic_messages')
    assert.equal(history.find(message => message.id === 'unknown-protocol').protocol, 'openai_chat_completions')
    const event = (await imported.allEvents()).find(event => event.type === 'assistant/message' && event.data.message.id === 'known-protocol')
    assert.equal(event.data.message.source.replayState.response.protocol, 'anthropic_messages')
    assert.deepEqual(event.data.message.source.replayState.response.blocks, signedBlocks)
  } finally { await imported.close() }
  const resumed = await Host.open(async (method, params) => {
    if (method === 'clear/recovery') return { results: {} }
    if (method === 'clear/prepare') return { system: 'PROTOCOL_SYSTEM', facts: '', tools: [], compact: false }
    if (method === 'clear/model') {
      const prior = params.messages.find(message => message.id === 'known-protocol')
      assert.equal(prior.protocol, 'anthropic_messages')
      assert.deepEqual(prior.blocks, signedBlocks)
      assert.deepEqual(prior.private, { original: true })
      return answer('New OpenAI answer')
    }
    throw new Error(`Unexpected callback ${method}`)
  }, imported.root)
  try {
    await resumed.initialize()
    await resumed.call('prompt', { content: 'Check the historical protocol before dispatch' })
    const live = (await resumed.allEvents()).find(event => event.type === 'assistant/message' && event.data.message.id !== 'known-protocol' && event.data.message.id !== 'unknown-protocol')
    assert.equal(live.data.message.source.replayState.response.protocol, 'openai_chat_completions')
  } finally { await resumed.close() }
  const reopened = await Host.open(async method => { assert.equal(method, 'clear/recovery'); return { results: {} } }, imported.root)
  try {
    await reopened.initialize({ protocol: 'anthropic_messages' })
    const history = (await reopened.call('history')).messages
    assert.equal(history.find(message => message.content === 'New OpenAI answer').protocol, 'openai_chat_completions')
    assert.equal(history.find(message => message.id === 'known-protocol').protocol, 'anthropic_messages')
    assert.equal(history.find(message => message.id === 'unknown-protocol').protocol, 'anthropic_messages')
  } finally { await reopened.close() }
})

test('two successive restarts replace aborted then pending receipts inside legal recovery turns without invoking model or tool', { timeout: 30000 }, async () => {
  const history = [{ id: 'prior-model', role: 'assistant', content: '', calls: [{ call_id: 'submitted-call', tool: 'business_inspect', arguments: {} }] },
    { id: 'aborted-result', role: 'tool', call_id: 'submitted-call', result: { status: 'error', summary: 'Process disposed while waiting' } }]
  const initial = await Host.open(async method => { throw new Error(`Unexpected import callback ${method}`) })
  try { await initial.initialize({ history, import_only: true }) } finally { await initial.close() }
  let prefix
  for (const receipt of [{ status: 'pending', summary: 'Original job still running', job_refs: [{ object_id: 'stable-job' }] }, result('verified-original-job')]) {
    const resumed = await Host.open(async (method, params) => {
      assert.equal(method, 'clear/recovery')
      assert.ok(params.call_ids.includes('submitted-call'))
      return { results: { 'submitted-call': receipt } }
    }, initial.root)
    try {
      await resumed.initialize()
      const history = await resumed.call('history')
      assert.deepEqual(history.messages.find(message => message.role === 'tool').result, receipt)
      const events = await resumed.allEvents()
      if (prefix) assert.deepEqual(events.slice(0, prefix.length), prefix)
      assert.ok(events.at(-1).type === 'session/end-seed')
      prefix = events
    } finally { await resumed.close() }
  }
})

test('received response journal survives a crash before assistant commit and is replayed once with conservative missing-operation outcomes', { timeout: 30000 }, async () => {
  const received = Promise.withResolvers(), neverRespond = Promise.withResolvers()
  const requestId = 'journal-received-request'
  const response = { ...answer('', [{ call_id: 'journal-finished', tool: 'business_inspect', arguments: {} }, { call_id: 'journal-unsubmitted', tool: 'business_inspect', arguments: {} }]), request_id: requestId, protocol: 'anthropic_messages' }
  const crashed = await Host.open(async (method, params) => {
    if (method === 'clear/prepare') return { system: 'JOURNAL_SYSTEM', facts: '', tools: [tool('business_inspect')], compact: false }
    if (method === 'clear/model') { received.resolve(params); return neverRespond.promise }
    throw new Error(`Unexpected callback ${method}`)
  })
  await crashed.initialize()
  const prompting = crashed.call('prompt', { id: 'crash-user', content: 'Continue once response is journaled' })
  await received.promise
  crashed.child.kill()
  await assert.rejects(prompting, /Host exit/)
  let prefix
  for (let index = 0; index < 2; index++) {
    const recovered = await Host.open(async (method, params) => {
      assert.equal(method, 'clear/recovery')
      assert.equal(params.committed_model_requests.includes(requestId), index > 0)
      return { request_id: requestId, response, results: { 'journal-finished': result('journaled-original-operation') } }
    }, crashed.root)
    try {
      await recovered.initialize()
      const events = await recovered.allEvents()
      const responses = events.filter(event => event.type === 'assistant/message' && event.data.message.source.replayState?.response?.request_id === requestId)
      assert.equal(responses.length, 1)
      assert.equal(responses[0].data.message.source.replayState.response.protocol, 'anthropic_messages')
      assert.equal(events.filter(event => event.type === 'tool/call' && event.data.callId === 'journal-finished').length, 1)
      const projected = await recovered.call('history')
      assert.equal(projected.messages.find(message => message.role === 'assistant').protocol, 'anthropic_messages')
      assert.deepEqual(projected.messages.find(message => message.call_id === 'journal-finished').result, result('journaled-original-operation'))
      const unsubmitted = events.find(event => event.type === 'tool/result' && event.data.message.toolCallId === 'journal-unsubmitted')
      assert.equal(unsubmitted.data.error.name, 'ToolNotStartedError')
      if (prefix) assert.deepEqual(events.slice(0, prefix.length), prefix)
      prefix = events
    } finally { await recovered.close() }
  }
})

test('three compactions preserve one consolidated checkpoint, tool pairing and durable original events across resume', { timeout: 30000 }, async () => {
  const reference = 'artifactOriginal:sha256:' + 'a'.repeat(64)
  const summary = { goal: 'Keep original goal', constraints: ['Keep identifiers'], plan: [], completed: [], pending_jobs: [], artifacts: [reference], next_step: 'Continue', references: [reference] }
  const history = [{ id: 'goal', role: 'user', content: 'Original user goal: keep identifiers. ' + '资料原文'.repeat(3000) }]
  for (let index = 0; index < 8; index++) history.push({ id: `original-${index}`, role: 'assistant', content: 'Observed evidence '.repeat(300), calls: [{ call_id: `original-call-${index}`, tool: 'business_inspect', arguments: {} }] },
    { id: `original-result-${index}`, role: 'tool', call_id: `original-call-${index}`, result: result(reference + ' evidence '.repeat(500)) })
  const summaries = []
  const host = await Host.open(async (method, params) => {
    if (method === 'clear/recovery') return { results: {} }
    if (method === 'clear/prepare') return { system: 'COMPACTION_SYSTEM', facts: '', tools: [tool('business_inspect')], compact: false }
    if (method === 'clear/model') {
      if (params.purpose === 'compaction') { summaries.push(params); return answer(JSON.stringify(summary)) }
      return answer('Recent observations '.repeat(250))
    }
    throw new Error(`Unexpected callback ${method}`)
  })
  let events
  try {
    await host.initialize({ history })
    for (let index = 0; index < 3; index++) {
      await host.call('prompt', { id: `continued-${index}`, content: `Continue ${index} with new evidence. ` + 'More source evidence '.repeat(1500) })
      await host.call('compact')
    }
    assert.equal(summaries.length, 3)
    assert.ok(summaries[1].messages.some(message => message.content?.includes('<compacted-summary>')))
    assert.ok(summaries[2].messages.some(message => message.content?.includes('<compacted-summary>')))
    const projected = await host.call('history')
    const checkpoints = projected.messages.filter(message => message.content?.includes('<compacted-summary>'))
    assert.equal(checkpoints.length, 1)
    assert.ok(checkpoints[0].content.includes(reference))
    events = await host.allEvents()
    assert.equal(events.filter(event => event.type === 'compaction/summary').length, 3)
    assert.equal(events.filter(event => event.type === 'tool/result').length, 8)
    assert.ok(events.some(event => event.type === 'user/message' && event.data.id === 'goal'))
    await writeFile(resolve(host.root, 'compactions-verified.json'), JSON.stringify({ summaries: summaries.length, original_results: 8, checkpoints: 1, event_count: events.length }, null, 2))
  } finally { await host.close() }
  const reopened = await Host.open(async method => { if (method === 'clear/recovery') return { results: {} }; throw new Error(`Unexpected callback ${method}`) }, host.root)
  try { await reopened.initialize(); assert.deepEqual((await reopened.allEvents()).slice(0, events.length), events) } finally { await reopened.close() }
})

test('pre-step compaction selects balanced physical boundaries and rebinds the final assembly after each of three summaries', { timeout: 30000 }, async () => {
  const summary = { goal: 'Original goal', constraints: ['Keep sources'], plan: [], completed: [], pending_jobs: [], artifacts: [], next_step: 'Continue', references: [] }
  const history = [{ id: 'source-goal', role: 'user', content: 'Keep sources. ' + 'Old source evidence '.repeat(4000) }]
  for (let index = 0; index < 8; index++) history.push({ id: `pair-model-${index}`, role: 'assistant', content: '', calls: [{ call_id: `pair-${index}`, tool: 'business_inspect', arguments: {} }] },
    { id: `pair-result-${index}`, role: 'tool', call_id: `pair-${index}`, result: result('Measured evidence '.repeat(1000)) })
  let host, modelCalls = 0, compactions = 0
  host = await Host.open(async (method, params) => {
    if (method === 'clear/prepare') return { system: `PREPARED_AFTER_SUMMARIES_${compactions}`, facts: `CURRENT_FACTS_${modelCalls}`, tools: [tool('business_inspect')], compact: compactions < modelCalls + 1 }
    if (method === 'clear/model') {
      if (params.purpose === 'compaction') { compactions++; return answer(JSON.stringify(summary)) }
      assert.equal(params.system, `PREPARED_AFTER_SUMMARIES_${compactions}`)
      assert.deepEqual(params.tools, [tool('business_inspect')])
      modelCalls++
      return answer('Recent verified source evidence '.repeat(250))
    }
    throw new Error(`Unexpected callback ${method}`)
  })
  try {
    await host.initialize({ history })
    for (let index = 0; index < 3; index++) await host.call('prompt', { id: `fresh-${index}`, content: 'Continue with source evidence '.repeat(1000) })
    assert.equal(modelCalls, 3)
    assert.equal(compactions, 3)
    const events = await host.allEvents()
    assert.equal(events.filter(event => event.type === 'compaction/summary').length, 3)
    assert.equal((await host.call('history')).messages.filter(message => message.content?.includes('<compacted-summary>')).length, 1)
  } finally { await host.close() }
})

test('a nonshrinking compaction fails before the next conversation request and leaves original evidence readable', { timeout: 30000 }, async () => {
  let conversationRequests = 0
  const host = await Host.open(async (method, params) => {
    if (method === 'clear/prepare') return { system: 'UNCHANGED_EVIDENCE', facts: '', tools: [], compact: true, over_limit: true }
    if (method === 'clear/model') {
      if (params.purpose !== 'compaction') conversationRequests++
      return answer(JSON.stringify({ goal: 'Expansion '.repeat(12000), constraints: [], plan: [], completed: [], pending_jobs: [], artifacts: [], next_step: 'Continue', references: [] }))
    }
    throw new Error(`Unexpected callback ${method}`)
  })
  try {
    await host.initialize({ history: Array.from({ length: 8 }, (_, index) => ({ id: `evidence-${index}`, role: 'user', content: `Immutable source ${index}. ` + 'Source record '.repeat(500) })) })
    await assert.rejects(host.call('prompt', { id: 'oversized', content: 'Continue the same task' }), /summary is not smaller/)
    assert.equal(conversationRequests, 0)
    const history = await host.call('history')
    assert.ok(history.messages.some(message => message.id === 'evidence-0'))
    const events = await host.allEvents()
    assert.ok(events.some(event => event.type === 'compaction/end' && event.data.error))
    assert.equal(events.filter(event => event.type === 'compaction/summary').length, 0)
  } finally { await host.close() }
})

test('live pending receipt reconciliation preserves content-only identity and read handles do not close the active writer', { timeout: 30000 }, async () => {
  let prepared = false, modelCalls = 0, toolCalls = 0, host
  const verified = result('original-job-verified')
  host = await Host.open(async (method, params) => {
    if (method === 'clear/prepare') {
      if (!prepared) { prepared = true; return { resolved_results: { 'pending-call': verified } } }
      return { system: 'LIVE_RESOLVED', facts: '', tools: [tool('business_inspect')], compact: false }
    }
    if (method === 'clear/tool') { toolCalls++; assert.equal(params.call_id, 'new-live-call'); return result('new-inspection') }
    if (method === 'clear/model') {
      assert.deepEqual(params.messages.find(message => message.call_id === 'pending-call').result, verified)
      // Reading and closing an independently owned read channel must leave the
      // live writer able to record the next assistant/tool/request sequence.
      assert.ok((await host.call('history_events', { after: 0, limit: 7 })).items.length)
      if (modelCalls++ === 0) return answer('', [{ call_id: 'new-live-call', tool: 'business_inspect', arguments: {} }])
      return answer('Verified original job and new inspection')
    }
    throw new Error(`Unexpected callback ${method}`)
  })
  try {
    await host.initialize({ history: [{ id: 'pending-model', role: 'assistant', content: '', calls: [{ call_id: 'pending-call', tool: 'business_inspect', arguments: {} }] },
      { id: 'pending-result', role: 'tool', call_id: 'pending-call', result: { status: 'pending', summary: 'Original native job waiting' } }] })
    await host.call('prompt', { id: 'resolve-user', content: 'Read original result then perform the next inspection' })
    assert.equal(modelCalls, 2); assert.equal(toolCalls, 1)
    const events = await host.allEvents()
    const replacements = events.filter(event => event.type === 'tool/result' && event.surfaceOp !== 'append')
    assert.equal(replacements.length, 1)
    assert.equal(replacements[0].data.message.id, 'pending-result')
    assert.equal(replacements[0].data.turn, 1)
    assert.equal(replacements[0].data.step, 1)
  } finally { await host.close() }
})

test('idle manual compaction checks summary field types without conversation or tool execution', { timeout: 30000 }, async () => {
  let summaries = 0
  const host = await Host.open(async (method, params) => {
    assert.equal(method, 'clear/model')
    assert.equal(params.purpose, 'compaction')
    summaries++
    return answer(JSON.stringify({ goal: null, constraints: [], plan: [], completed: [], pending_jobs: [], artifacts: [], next_step: 'Continue', references: [] }))
  })
  try {
    await host.initialize({ history: Array.from({ length: 7 }, (_, index) => ({ id: `idle-source-${index}`, role: 'user', content: 'Original task evidence '.repeat(1000) })) })
    await assert.rejects(host.call('compact'), /manual compaction could not produce a smaller summary/)
    assert.equal(summaries, 2)
    assert.ok((await host.call('history')).messages.some(message => message.id === 'idle-source-0'))
    const events = await host.allEvents()
    assert.equal(events.filter(event => event.type === 'compaction/summary').length, 0)
    assert.ok(events.some(event => event.type === 'compaction/end' && event.data.turn === null && event.data.error))
    assert.ok(events.some(event => event.type === 'compaction/end' && event.data.error?.includes('检查点字段必须是非空文本：goal')))
  } finally { await host.close() }
})

test('a single schema repair corrects an array next_step without inventing another operation or dropping source references', { timeout: 30000 }, async () => {
  let requests = 0
  const source = 'artifact-existing:sha256:' + 'b'.repeat(64)
  const checkpoint = { goal: 'Original task', constraints: [], plan: [], completed: [], pending_jobs: [], artifacts: [source], next_step: ['Read the existing artifact'], references: [source] }
  const host = await Host.open(async (method, params) => {
    assert.equal(method, 'clear/model'); assert.equal(params.purpose, 'compaction')
    const instruction = params.messages.at(-1).content
    assert.ok(instruction.includes('"next_step":{"type":"string"}'))
    requests++
    if (requests === 1) return answer(JSON.stringify(checkpoint))
    assert.ok(instruction.includes(source))
    assert.ok(instruction.includes('仅允许这一次修正'))
    return answer(JSON.stringify({ ...checkpoint, next_step: 'Read the existing artifact' }))
  })
  try {
    await host.initialize({ history: Array.from({ length: 8 }, (_, index) => ({ id: `repair-source-${index}`, role: 'user', content: 'Original source evidence '.repeat(1000) })) })
    assert.equal((await host.call('compact')).compacted, true)
    assert.equal(requests, 2)
    const events = await host.allEvents()
    assert.equal(events.filter(event => event.type === 'compaction/summary').length, 1)
    assert.ok((await host.call('history')).messages.some(message => message.content?.includes(source)))
  } finally { await host.close() }
})

test('confirmed summary length permits one shorter repair at the same 2048 budget and adds an exact immutable history index', { timeout: 30000 }, async () => {
  for (const mode of ['finish', 'rpc']) {
    let requests = 0
    const checkpoint = { goal: 'Finish the original goal', constraints: ['Keep all leading zeroes'], plan: ['Finish remaining files'],
      completed: [{ operation_id: 'original-operation' }], pending_jobs: ['original-pending-job'], artifacts: ['original-artifact'], next_step: 'Read the original pending job', references: [] }
    const host = await Host.open(async (method, params) => {
      assert.equal(method, 'clear/model'); assert.equal(params.purpose, 'compaction')
      assert.equal(params.max_tokens, 2048); assert.deepEqual(params.tools, [])
      requests++
      const instruction = params.messages.at(-1).content
      assert.ok(instruction.includes('输出硬预算为2048 tokens'))
      if (requests === 1) {
        if (mode === 'finish') return { ...answer('{"goal":"unfinished'), stop_reason: 'length' }
        throw Object.assign(new Error('Confirmed output limit'), { code: 'truncated_response', response_diagnostics: { finish_reason: 'length', observed_tool_calls: 0 } })
      }
      assert.ok(instruction.includes('不能调大预算'))
      return answer(JSON.stringify(checkpoint))
    })
    try {
      await host.initialize({ profile: { ...profile, output_reserve: 2048 }, history: Array.from({ length: 7 }, (_, index) =>
        ({ id: `length-source-${index}`, role: 'user', content: 'Goal and constraint with original references '.repeat(1000) })) })
      const before = await host.allEvents()
      assert.equal((await host.call('compact')).compacted, true)
      assert.equal(requests, 2)
      const events = await host.allEvents(), summary = events.find(event => event.type === 'compaction/summary')
      const value = JSON.parse(summary.data.summary[0].text)
      assert.equal(value.goal, checkpoint.goal); assert.deepEqual(value.constraints, checkpoint.constraints)
      assert.deepEqual(value.pending_jobs, checkpoint.pending_jobs)
      const anchor = value.references.find(ref => ref.tool === 'session_read_history')
      assert.equal(anchor.first_harness_seq, summary.data.shadowedRange.start)
      assert.equal(anchor.last_harness_seq, summary.data.shadowedRange.end)
      assert.deepEqual(anchor.arguments, { after_harness: anchor.first_harness_seq - 1, limit: 20 })
      assert.deepEqual(events.slice(0, before.length), before)
      assert.equal(summary.data.rawOutput[0].text, JSON.stringify(checkpoint))
    } finally { await host.close() }
  }
})

test('an EOF never triggers summary repair and two confirmed length stops preserve original history without a checkpoint', { timeout: 30000 }, async () => {
  for (const reason of ['eof', 'max_tokens']) {
    let requests = 0
    const host = await Host.open(async (method, params) => {
      assert.equal(method, 'clear/model'); assert.equal(params.max_tokens, 2048)
      requests++
      throw Object.assign(new Error('Incomplete response'), { code: 'truncated_response', response_diagnostics: { finish_reason: reason } })
    })
    try {
      await host.initialize({ profile: { ...profile, output_reserve: 2048 }, history: Array.from({ length: 7 }, (_, index) =>
        ({ id: `unmodified-source-${index}`, role: 'user', content: 'Preserve original task and constraints '.repeat(1000) })) })
      await assert.rejects(host.call('compact'), /manual compaction could not produce a smaller summary/)
      assert.equal(requests, reason === 'eof' ? 1 : 2)
      assert.ok((await host.call('history')).messages.some(message => message.id === 'unmodified-source-0'))
      assert.equal((await host.allEvents()).filter(event => event.type === 'compaction/summary').length, 0)
    } finally { await host.close() }
  }
})

test('tool-result previews change only the sent projection while immutable original rows and references remain in the durable log', { timeout: 30000 }, async () => {
  const original = { ...result('source-result'), data: { rows: Array.from({ length: 100 }, (_, index) => ({ source_row: index, value: 'Long original evidence '.repeat(100) })) }, result_refs: [{ kind: 'table', object_id: 'original-table', sha256: 'c'.repeat(64) }] }
  const preview = { ...original, data: { rows: original.data.rows.slice(0, 1), context_preview_only: true, original_count: 100 } }
  let previewed = false
  const host = await Host.open(async (method, params) => {
    if (method === 'clear/prepare') {
      if (!previewed) { previewed = true; return { preview_results: { 'preview-call': preview } } }
      return { system: 'PREVIEW_BOUND', facts: '', tools: [], compact: false }
    }
    assert.equal(method, 'clear/model')
    const receipt = params.messages.find(message => message.call_id === 'preview-call').result
    assert.equal(receipt.data.rows.length, 1)
    assert.deepEqual(receipt.result_refs, original.result_refs)
    return answer('Read the retained source reference as needed')
  })
  try {
    await host.initialize({ history: [{ id: 'preview-model', role: 'assistant', content: '', calls: [{ call_id: 'preview-call', tool: 'business_inspect', arguments: {} }] },
      { id: 'preview-original', role: 'tool', call_id: 'preview-call', result: original }] })
    await host.call('prompt', { id: 'preview-user', content: 'Continue with bounded previews' })
    const events = await host.allEvents()
    const receipts = events.filter(event => event.type === 'tool/result')
    assert.equal(JSON.parse(receipts[0].data.message.content[0].text).data.rows.length, 100)
    assert.equal(JSON.parse(receipts[1].data.message.content[0].text).data.rows.length, 1)
    assert.equal(receipts[0].data.message.id, receipts[1].data.message.id)
  } finally { await host.close() }
})
