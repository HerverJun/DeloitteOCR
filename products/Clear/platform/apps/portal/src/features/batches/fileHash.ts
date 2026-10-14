import { hashFileSlices, readBlob } from './fileHashSlices'
export { AUDIO_CHUNK_BYTES, hashFileSlices } from './fileHashSlices'

export function fileSha256(file: Blob, progress?: (done: number, total: number) => void, signal?: AbortSignal): Promise<string> {
  // Browsers hash in an isolated worker. The streaming fallback supports DOM
  // test environments without Worker, and still never reads the whole file.
  if (typeof Worker === 'undefined') return hashFileSlices(file, progress, signal)
  return new Promise((resolve, reject) => {
    signal?.throwIfAborted()
    const worker = new Worker(new URL('./fileHash.worker.ts', import.meta.url), { type: 'module' })
    const cleanup = () => { worker.terminate(); signal?.removeEventListener('abort', abort) }
    const abort = () => { cleanup(); reject(signal?.reason || new DOMException('已暂停', 'AbortError')) }
    signal?.addEventListener('abort', abort, { once: true })
    worker.onmessage = (event: MessageEvent<{ done?: number; total?: number; sha256?: string; error?: string }>) => {
      const value = event.data
      if (value.sha256) { cleanup(); resolve(value.sha256) }
      else if (value.error) { cleanup(); reject(new Error(value.error)) }
      else if (value.done !== undefined && value.total !== undefined) progress?.(value.done, value.total)
    }
    worker.onerror = () => { cleanup(); reject(new Error('文件校验失败，请重新选择原文件')) }
    worker.postMessage(file)
  })
}

export async function chunkSha256(blob: Blob) {
  const digest = await crypto.subtle.digest('SHA-256', await readBlob(blob))
  return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('')
}
