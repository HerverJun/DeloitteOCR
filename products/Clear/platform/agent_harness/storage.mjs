/** Apply the launcher's standard-library path policy before any Harness write. */
import { existsSync, readFileSync, realpathSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'

const validation = "import sys;sys.path.insert(0,sys.argv[1]);from packages.runtime import require_path;print(require_path(sys.argv[2],'Harness path',role=sys.argv[3]))"

export function requireStoragePathSync(value, role = 'write', environment = process.env) {
  if (!['read', 'write'].includes(role)) throw new Error('Unknown Harness storage role')
  const absolute = resolve(value)
  if (!/^[a-z]:\\/i.test(absolute)) throw new Error('Harness path requires an ordinary local Windows drive')
  if (environment.CLEAR_INSTALL_CONFIG) {
    // Do not duplicate the Python rules for fixed disks, configured roots,
    // reparses and trusted Paddle mappings. The small stdlib runtime is the
    // same policy used by the portal and all isolated inference workers.
    const config = JSON.parse(readFileSync(environment.CLEAR_INSTALL_CONFIG, 'utf8').replace(/^\uFEFF/, ''))
    if (config.schema_version !== 1) throw new Error('Unsupported Harness installation configuration')
    const base = resolve(dirname(environment.CLEAR_INSTALL_CONFIG), config.paths.program)
    const resource = name => {
      if (!config.resources?.[name]) throw new Error('Missing Harness installation resource: ' + name)
      return resolve(base, config.resources[name])
    }
    const runtime = resource('platform_python'), platform = resource('platform_source')
    if (environment.CLEAR_PLATFORM_PYTHON && realpathSync(environment.CLEAR_PLATFORM_PYTHON).toLowerCase() !== realpathSync(runtime).toLowerCase()) {
      throw new Error('Harness Python differs from its registered installation runtime')
    }
    const child = spawnSync(runtime, ['-B', '-I', '-X', 'utf8', '-c', validation, platform, absolute, role], {
      env: { ...environment, PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1' },
      encoding: 'utf8', windowsHide: true, timeout: 15000, maxBuffer: 1024 * 1024,
    })
    if (child.error || child.status !== 0) throw new Error('Harness path rejected by installation policy: ' + (child.error?.message || child.stderr?.trim()))
    const checked = child.stdout.trim()
    if (!/^[a-z]:\\/i.test(checked)) throw new Error('Invalid shared Harness path-policy response')
    return checked
  }
  if (!/^f:\\/i.test(absolute)) throw new Error('Harness path must be on local F drive in unmanaged development')
  let ancestor = absolute
  while (!existsSync(ancestor)) {
    const parent = dirname(ancestor)
    if (parent === ancestor) throw new Error('Harness path has no local existing ancestor')
    ancestor = parent
  }
  if (!/^f:\\/i.test(realpathSync(ancestor))) throw new Error('Harness path resolves outside F drive')
  return absolute
}
