import { afterEach, expect, test, vi } from 'vitest'
import { randomUUID } from '../../../packages/browser/secureRandom'

afterEach(() => vi.unstubAllGlobals())

test('LAN HTTP uses cryptographic bytes and RFC4122 version/variant bits', () => {
  const getRandomValues = vi.fn((bytes: Uint8Array) => bytes.fill(255))
  vi.stubGlobal('crypto', { getRandomValues })
  const weak = vi.spyOn(Math, 'random').mockImplementation(() => { throw new Error('Weak RNG forbidden') })
  try {
    expect(randomUUID()).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff')
    expect(getRandomValues).toHaveBeenCalledOnce()
    expect(getRandomValues.mock.calls[0][0]).toHaveLength(16)
  } finally { weak.mockRestore() }
})

test('secure-context native UUID keeps its receiver and existing format', () => {
  const source = { randomUUID() { expect(this).toBe(source); return '12345678-1234-4234-8234-123456789abc' } }
  vi.stubGlobal('crypto', source)
  expect(randomUUID()).toBe('12345678-1234-4234-8234-123456789abc')
})

test('a browser without a secure RNG fails explicitly instead of generating weak IDs', () => {
  vi.stubGlobal('crypto', {})
  expect(() => randomUUID()).toThrow('安全随机数')
})
