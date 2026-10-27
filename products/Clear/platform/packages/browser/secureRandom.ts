/** RFC 4122 v4 IDs also work on explicitly configured LAN HTTP origins. */
export function randomUUID(): string {
  const cryptography = globalThis.crypto
  if (typeof cryptography?.randomUUID === 'function') return cryptography.randomUUID()
  if (typeof cryptography?.getRandomValues !== 'function') {
    throw new Error('浏览器不支持安全随机数，请使用当前版本的浏览器')
  }
  const bytes = cryptography.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0'))
  return hex.slice(0, 4).join('') + '-' + hex.slice(4, 6).join('') + '-'
    + hex.slice(6, 8).join('') + '-' + hex.slice(8, 10).join('') + '-' + hex.slice(10).join('')
}
