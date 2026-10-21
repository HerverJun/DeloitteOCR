/** Offline acceptance guard: fail before any socket, listener or HTTP call. */
import net from 'node:net'
import tls from 'node:tls'
import http from 'node:http'
import https from 'node:https'
import fs from 'node:fs'
import { resolve, dirname, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { syncBuiltinESMExports } from 'node:module'

const blocked = () => { throw Object.assign(new Error('Offline smoke blocked a network operation'), { code: 'OFFLINE_NETWORK_BLOCKED' }) }
net.connect = net.createConnection = blocked
net.Socket.prototype.connect = blocked
net.Server.prototype.listen = blocked
tls.connect = blocked
http.request = http.get = https.request = https.get = blocked
globalThis.fetch = blocked

// Node 24's Permission Model disables fsync even for allowed descriptors.
// Keep Harness' actual durable writes/fsync intact; guard write path entry
// points instead. The declared Koffi addon is used for Windows file locks.
const realpath = fs.realpathSync.native
const output = realpath(process.env.CLEAR_HARNESS_SMOKE_OUTPUT || '').toLowerCase()
if (!/^[f]:[\\/]/.test(output)) throw new Error('Offline write root must be an existing F directory')
function writable(value) {
  if (typeof value === 'number' || value && typeof value.fd === 'number') return
  const input = value instanceof URL ? fileURLToPath(value) : Buffer.isBuffer(value) ? value.toString() : value
  if (typeof input !== 'string') throw new Error('Invalid offline file path')
  let path = resolve(input), current = path, suffix = []
  while (true) {
    try { path = resolve(realpath(current), ...suffix.reverse()); break }
    catch (error) {
      if (error.code !== 'ENOENT' || dirname(current) === current) throw error
      suffix.push(basename(current)); current = dirname(current)
    }
  }
  const normalized = path.toLowerCase()
  if (normalized !== output && !normalized.startsWith(output + '\\')) {
    throw Object.assign(new Error('Offline smoke blocked a write outside its output root'), { code: 'OFFLINE_WRITE_BLOCKED' })
  }
}
const writeFlags = flags => typeof flags === 'string' ? /[wax+]/.test(flags) :
  typeof flags === 'number' && !!(flags & (fs.constants.O_WRONLY | fs.constants.O_RDWR | fs.constants.O_CREAT | fs.constants.O_TRUNC | fs.constants.O_APPEND))
for (const owner of [fs, fs.promises]) {
  for (const name of ['open', 'openSync']) if (typeof owner[name] === 'function') {
    const original = owner[name]
    owner[name] = function (path, flags, ...rest) { if (writeFlags(flags)) writable(path); return original.call(this, path, flags, ...rest) }
  }
  for (const name of ['mkdir', 'mkdirSync', 'writeFile', 'writeFileSync', 'appendFile', 'appendFileSync',
    'createWriteStream', 'mkdtemp', 'mkdtempSync', 'unlink', 'unlinkSync', 'rmdir', 'rmdirSync', 'rm', 'rmSync', 'chmod', 'chmodSync', 'utimes', 'utimesSync',
    'truncate', 'truncateSync']) if (typeof owner[name] === 'function') {
    const original = owner[name]
    owner[name] = function (path, ...rest) { writable(path); return original.call(this, path, ...rest) }
  }
  for (const name of ['rename', 'renameSync']) if (typeof owner[name] === 'function') {
    const original = owner[name]
    owner[name] = function (from, to, ...rest) { writable(from); writable(to); return original.call(this, from, to, ...rest) }
  }
  for (const name of ['copyFile', 'copyFileSync', 'cp', 'cpSync', 'link', 'linkSync']) if (typeof owner[name] === 'function') {
    const original = owner[name]
    owner[name] = function (from, to, ...rest) { writable(to); return original.call(this, from, to, ...rest) }
  }
  for (const name of ['symlink', 'symlinkSync']) if (typeof owner[name] === 'function') owner[name] =
    () => { throw Object.assign(new Error('Offline smoke does not create symbolic links'), { code: 'OFFLINE_WRITE_BLOCKED' }) }
}
syncBuiltinESMExports()
