import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { useReducedMotion } from './RunStatus'

type ReadingPosition = { top: number; following: boolean; anchor?: string; offset?: number }
export const nearBottom = (element: Pick<HTMLElement, 'scrollHeight' | 'clientHeight' | 'scrollTop'>) => element.scrollHeight - element.clientHeight - element.scrollTop <= 80
export function useConversationScroll(scope: string, itemIds: string[], stamp: number) {
  const scroller = useRef<HTMLDivElement>(null), content = useRef<HTMLDivElement>(null), positions = useRef(new Map<string, ReadingPosition>())
  const follow = useRef(true), previousIds = useRef(new Set<string>()), [following, setFollowing] = useState(true), [unread, setUnread] = useState(0)
  const pendingRestore = useRef<ReadingPosition | undefined>(undefined)
  const reduced = useReducedMotion()
  const save = useCallback(() => {
    const element = scroller.current
    if (!element) return
    const top = element.getBoundingClientRect().top
    const anchor = [...element.querySelectorAll<HTMLElement>('[data-timeline-id]')].find(item => item.getBoundingClientRect().bottom > top)
    positions.current.set(scope, { top: element.scrollTop, following: follow.current, anchor: anchor?.dataset.timelineId, offset: anchor ? anchor.getBoundingClientRect().top - top : undefined })
  }, [scope])
  const onScroll = useCallback(() => {
    if (!scroller.current || pendingRestore.current) return
    follow.current = nearBottom(scroller.current); setFollowing(follow.current)
    if (follow.current) setUnread(0)
    save()
  }, [save])
  useLayoutEffect(() => {
    const saved = positions.current.get(scope), element = scroller.current
    pendingRestore.current = saved && !saved.following ? saved : undefined
    follow.current = saved?.following ?? true; setFollowing(follow.current); setUnread(0); previousIds.current = new Set(itemIds)
    if (element) {
      const anchor = saved?.anchor ? [...element.querySelectorAll<HTMLElement>('[data-timeline-id]')].find(item => item.dataset.timelineId === saved.anchor) : undefined
      element.scrollTop = follow.current ? element.scrollHeight : saved?.top || 0
      if (anchor && saved?.offset !== undefined) element.scrollTop += anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - saved.offset
    }
  }, [scope])
  useLayoutEffect(() => {
    const element = scroller.current
    if (!element) return
    if (pendingRestore.current && itemIds.length) {
      const saved = pendingRestore.current
      const anchor = [...element.querySelectorAll<HTMLElement>('[data-timeline-id]')].find(item => item.dataset.timelineId === saved.anchor)
      element.scrollTop = saved.top
      if (anchor && saved.offset !== undefined) element.scrollTop += anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - saved.offset
      pendingRestore.current = undefined
    }
    const additions = itemIds.filter(id => !previousIds.current.has(id)).length
    previousIds.current = new Set(itemIds)
    if (follow.current) { element.scrollTop = element.scrollHeight; setUnread(0) }
    else if (additions) setUnread(value => value + additions)
  }, [stamp, itemIds])
  useLayoutEffect(() => {
    if (!content.current || !window.ResizeObserver) return
    const observer = new ResizeObserver(() => { if (follow.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight })
    observer.observe(content.current); return () => observer.disconnect()
  }, [scope])
  const jumpToLatest = useCallback(() => {
    follow.current = true; setFollowing(true); setUnread(0)
    if (scroller.current) {
      if (!reduced && typeof scroller.current.scrollTo === 'function') scroller.current.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' })
      else scroller.current.scrollTop = scroller.current.scrollHeight
    }
  }, [reduced])
  return { scroller, content, following, unread, onScroll, jumpToLatest }
}
