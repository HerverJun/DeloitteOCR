import { useEffect, useState } from 'react'

export type RecentVisit = { href: string; title: string; workspace: string | null; visited: number }
const eventName = 'clear-recent-visits'
const key = (owner: string) => `clear-recent-visits-v1:${owner}`
const paths = /^\/(agent|projects|assets|artifacts|jobs|findings|apps(?:\/[a-z-]+(?:\/[a-z-]+)?)?)$/
export function readRecentVisits(owner: string): RecentVisit[] {
  try {
    const values: unknown = JSON.parse(localStorage.getItem(key(owner)) || '[]')
    return Array.isArray(values) ? values.filter((item): item is RecentVisit => {
      if (!item || typeof item.href !== 'string' || typeof item.title !== 'string' || !Number.isFinite(item.visited)) return false
      const url = new URL(item.href, window.location.origin)
      return url.origin === window.location.origin && paths.test(url.pathname) && item.href.startsWith('/') && !item.href.startsWith('//')
        && (item.workspace === null || typeof item.workspace === 'string')
    }).slice(0, 12) : []
  } catch { return [] }
}
export function rememberVisit(owner: string, visit: Omit<RecentVisit, 'visited'>) {
  try {
    const url = new URL(visit.href, window.location.origin)
    if (url.origin !== window.location.origin || !paths.test(url.pathname)) return
    const params = new URLSearchParams()
    for (const field of ['workspace', 'session', 'bundle', 'inspection', 'analysis']) {
      const value = url.searchParams.get(field)
      if (value && /^[A-Za-z0-9_-]+$/.test(value)) params.set(field, value)
    }
    const href = url.pathname + (params.size ? `?${params}` : '')
    const items = [{ ...visit, href, title: visit.title.slice(0, 160), visited: Date.now() }, ...readRecentVisits(owner).filter(item => item.href !== href)].slice(0, 12)
    localStorage.setItem(key(owner), JSON.stringify(items)); window.dispatchEvent(new Event(eventName))
  } catch { /* Optional navigation history never blocks work. */ }
}
export function useRecentVisits(owner: string) {
  const [items, setItems] = useState(() => readRecentVisits(owner))
  useEffect(() => {
    const update = () => setItems(readRecentVisits(owner))
    update(); window.addEventListener(eventName, update); window.addEventListener('storage', update)
    return () => { window.removeEventListener(eventName, update); window.removeEventListener('storage', update) }
  }, [owner])
  return items
}
