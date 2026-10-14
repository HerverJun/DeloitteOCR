/** Keep a review return address inside the originating workspace. */
export function nativeReviewReturn(value: string | null, workspaceId: string | null): string | null {
  if (!value || !workspaceId || value.length > 4096 || !value.startsWith('/')
    || value.startsWith('//') || /[\\\u0000-\u001f]/.test(value)) return null
  try {
    const url = new URL(value, 'http://127.0.0.1:1')
    if (url.origin !== 'http://127.0.0.1:1' || !['/batches', '/todos', '/audit'].includes(url.pathname)
      || url.searchParams.get('workspace') !== workspaceId) return null
    return url.pathname + url.search + url.hash
  } catch { return null }
}
