import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, symlinkSync, rmSync } from 'node:fs'
import { resolve, join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { requireStoragePathSync } from '../storage.mjs'

test('managed Harness shares native policy for separated DATA, immutable app and escaping junctions', () => {
  const temporary = process.env.TEMP
  assert.match(temporary, /^f:[\\/]/i, 'This checkout must only write tests on F')
  const root = mkdtempSync(join(temporary, 'harness-storage-'))
  try {
    const platform = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
    const runtime = process.env.CLEAR_TEST_PLATFORM_PYTHON
    assert.ok(runtime, 'Pass the existing F-drive platform Python explicitly')
    const paths = {
      program: resolve(platform, '..'), assets: join(root, 'assets'), runtimes: dirname(runtime), tools: join(root, 'tools'),
      data: join(root, '中文 DATA'), cache: join(root, 'cache'), temp: join(root, 'temp'),
      paddle_temp: join(root, 'paddle-temp'), secrets: join(root, 'secrets'), backup: join(root, 'backups'),
    }
    for (const path of Object.values(paths)) mkdirSync(path, { recursive: true })
    const configPath = join(root, 'clear.install.json')
    writeFileSync(configPath, JSON.stringify({ schema_version: 1, installation_id: randomUUID(), paths,
      mode: 'single', network: { transport: 'loopback-http', listen: '127.0.0.1', public_origin: 'http://127.0.0.1:8889',
        portal_port: 8889, worker_port: 8890, postgres_port: 5432 },
      resources: { platform_source: platform, platform_python: runtime }, policy: { allowed_drives: ['F:'] } }))
    const environment = { ...process.env, CLEAR_INSTALL_CONFIG: configPath, CLEAR_PLATFORM_PYTHON: runtime }
    assert.equal(requireStoragePathSync(join(paths.data, '任务', 'new'), 'write', environment), join(paths.data, '任务', 'new'))
    assert.throws(() => requireStoragePathSync(join(paths.program, 'no-write'), 'write', environment), /registered write roots/)
    const denied = join(root, 'unregistered', 'new')
    assert.throws(() => requireStoragePathSync(denied, 'write', environment), /registered write roots/)
    assert.equal(existsSync(denied), false)
    const target = join(root, 'unregistered')
    mkdirSync(target)
    symlinkSync(target, join(paths.data, 'escape'), 'junction')
    assert.throws(() => requireStoragePathSync(join(paths.data, 'escape', 'new'), 'write', environment), /registered|reparse/)
    assert.throws(() => requireStoragePathSync(join(paths.data, 'new'), 'write', { ...environment, CLEAR_INSTALL_CONFIG: join(root, 'missing.json') }), /ENOENT/)
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('unmanaged Harness retains local F-only development without creating directories', () => {
  assert.throws(() => requireStoragePathSync('C:/must-not-create', 'write', {}), /local F drive/)
  assert.throws(() => requireStoragePathSync('\\\\server\\share\\must-not-create', 'write', {}), /ordinary local Windows drive/)
})
