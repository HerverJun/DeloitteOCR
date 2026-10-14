import { Blob as NodeBlob } from 'node:buffer'
import { createHash } from 'node:crypto'
import { expect, test, vi } from 'vitest'
import { Sha256 } from '../src/features/batches/sha256'
import { AUDIO_CHUNK_BYTES, hashFileSlices } from '../src/features/batches/fileHashSlices'

test.each([
  ['', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
  ['abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
  ['abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq', '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1'],
])('FIPS SHA256 standard vector %s', (value, expected) => {
  const data = new TextEncoder().encode(value)
  const state = new Sha256()
  for (let index = 0; index < data.length; index += 7) state.update(data.subarray(index, index + 7))
  expect(state.hex()).toBe(expected)
})

test('million a standard vector and arbitrary byte slices agree with the native SHA256 implementation', () => {
  const million = new Uint8Array(1_000_000).fill(97)
  expect(new Sha256().update(million).hex()).toBe('cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0')
  for (const length of [1, 55, 56, 63, 64, 65, 127, 128, 129, 4097]) {
    const data = Uint8Array.from({ length }, (_, index) => (index * 37 + 13) & 255)
    const state = new Sha256()
    for (let index = 0; index < length; index += 13) state.update(data.subarray(index, index + 13))
    expect(state.hex()).toBe(createHash('sha256').update(data).digest('hex'))
    expect(() => state.update(data)).toThrow()
  }
})

test('file hashing reads only bounded slices and reports progress without reading a whole file buffer', async () => {
  const data = new Uint8Array(AUDIO_CHUNK_BYTES + 73).fill(19)
  const blob = new NodeBlob([data])
  const wholeRead = vi.fn(() => { throw new Error('whole file arrayBuffer is forbidden') })
  Object.defineProperty(blob, 'arrayBuffer', { value: wholeRead })
  const slice = vi.spyOn(blob, 'slice')
  const progress = vi.fn()
  const actual = await hashFileSlices(blob as unknown as Blob, progress)
  expect(actual).toBe(createHash('sha256').update(data).digest('hex'))
  expect(wholeRead).not.toHaveBeenCalled()
  expect(slice.mock.calls).toEqual([[0, AUDIO_CHUNK_BYTES], [AUDIO_CHUNK_BYTES, data.length]])
  expect(progress.mock.calls).toEqual([[AUDIO_CHUNK_BYTES, data.length], [data.length, data.length]])
}, 15000)

test('aborted hashing stops before reading another slice', async () => {
  const blob = new NodeBlob([new Uint8Array(AUDIO_CHUNK_BYTES + 1)])
  const controller = new AbortController()
  const slice = vi.spyOn(blob, 'slice')
  await expect(hashFileSlices(blob as unknown as Blob, () => controller.abort(), controller.signal)).rejects.toThrow()
  expect(slice).toHaveBeenCalledTimes(1)
}, 15000)
