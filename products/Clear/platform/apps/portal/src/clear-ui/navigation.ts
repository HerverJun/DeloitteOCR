/** Keep task context when switching areas, but never replay an AI continuation. */
export function navigationHref(path: string, search: string, hash = '', fromPath = '/') {
  const source = new URLSearchParams(search)
  const task = source.get('task_return') || (fromPath === '/' || fromPath === '/agent' ? source.get('session') : null)
  const samePage = path === fromPath
  // Result IDs, revisions, input selections and pane anchors belong to their
  // source page. Cross-area navigation carries only shared project/task scope.
  const params = samePage ? source : new URLSearchParams()
  if (!samePage && source.has('workspace')) params.set('workspace', source.get('workspace')!)
  params.delete('continue')
  if (path === '/') {
    params.delete('session')
    params.delete('task_return')
  } else if (path === '/agent') {
    params.delete('task_return')
    params.delete('session')
    if (task) params.set('session', task)
  } else {
    if (!samePage) params.delete('session')
    if (task) params.set('task_return', task)
  }
  return `${path}${params.size ? `?${params}` : ''}${samePage ? hash : ''}`
}
