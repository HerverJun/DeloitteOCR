/** Keep task context when switching areas, but never replay an AI continuation. */
export function navigationHref(path: string, search: string, hash = '', fromPath = '/') {
  const params = new URLSearchParams(search)
  const task = params.get('task_return') || (fromPath === '/' || fromPath === '/agent' ? params.get('session') : null)
  params.delete('continue')
  if (path === '/') {
    params.delete('session')
    params.delete('task_return')
  } else if (path === '/agent') {
    params.delete('task_return')
    params.delete('session')
    if (task) params.set('session', task)
  } else {
    params.delete('session')
    if (task) params.set('task_return', task)
  }
  return `${path}${params.size ? `?${params}` : ''}${hash}`
}
