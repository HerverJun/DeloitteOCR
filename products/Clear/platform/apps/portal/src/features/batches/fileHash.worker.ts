import { hashFileSlices } from './fileHashSlices'

self.onmessage = async (event: MessageEvent<Blob>) => {
  try {
    const sha256 = await hashFileSlices(event.data, (done, total) => self.postMessage({ done, total }))
    self.postMessage({ sha256 })
  } catch (error) { self.postMessage({ error: error instanceof Error ? error.message : '文件校验失败' }) }
}
