import { Sha256 } from './sha256'

export const AUDIO_CHUNK_BYTES = 8 * 1024 * 1024
export function readBlob(blob: Blob): Promise<ArrayBuffer> {
  if (blob.arrayBuffer) return blob.arrayBuffer()
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as ArrayBuffer)
    reader.onerror = () => reject(new Error('文件读取失败'))
    reader.readAsArrayBuffer(blob)
  })
}

export async function hashFileSlices(file: Blob, progress?: (done: number, total: number) => void, signal?: AbortSignal) {
  const state = new Sha256()
  for (let offset = 0; offset < file.size; offset += AUDIO_CHUNK_BYTES) {
    signal?.throwIfAborted()
    state.update(new Uint8Array(await readBlob(file.slice(offset, Math.min(offset + AUDIO_CHUNK_BYTES, file.size)))))
    progress?.(Math.min(offset + AUDIO_CHUNK_BYTES, file.size), file.size)
  }
  signal?.throwIfAborted()
  return state.hex()
}
