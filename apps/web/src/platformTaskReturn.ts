type NavigationScope = { app_id: string; instance_id: string; workspace_id: string }
const id = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/

/** The trusted base URL and scope come from the validated native ticket/identity. */
export function resolveTaskReturn(base: string | null, scope: NavigationScope): string | null {
  if (!base) return null
  const key = `clear-task-return:${scope.app_id}:${scope.instance_id}`
  let task: string | undefined
  try {
    const hash = new URLSearchParams(location.hash.slice(1))
    const supplied = hash.has('platform_task_return') || hash.has('platform_task_workspace')
    if (supplied) {
      const tasks = hash.getAll('platform_task_return'), workspaces = hash.getAll('platform_task_workspace')
      hash.delete('platform_task_return'); hash.delete('platform_task_workspace')
      history.replaceState(history.state, '', location.pathname + location.search + (hash.size ? `#${hash}` : ''))
      if (tasks.length === 1 && workspaces.length === 1 && id.test(tasks[0]) && workspaces[0] === scope.workspace_id) task = tasks[0]
      try { if (task) sessionStorage.setItem(key, JSON.stringify({ task, workspace: scope.workspace_id })); else sessionStorage.removeItem(key) } catch { /* Current valid return remains usable. */ }
    } else {
      try {
        const saved = JSON.parse(sessionStorage.getItem(key) || 'null')
        if (saved?.workspace === scope.workspace_id && typeof saved.task === 'string' && id.test(saved.task)) task = saved.task
      } catch { /* Optional tab storage is unavailable. */ }
    }
    if (!task) return null
    const url = new URL(base)
    url.pathname = '/agent'; url.search = new URLSearchParams({ workspace: scope.workspace_id, session: task }).toString(); url.hash = ''
    return url.href
  } catch { return null }
}
