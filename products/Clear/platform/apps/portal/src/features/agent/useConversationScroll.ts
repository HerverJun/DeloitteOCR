import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { useReducedMotion } from './RunStatus'
import { timelineLayoutEvent, timelineMotion } from './conversationMotion'

type ReadingPosition = { top: number; following: boolean; anchor?: string; offset?: number; fallbackAnchor?: string; fallbackOffset?: number }
export const nearBottom = (element: Pick<HTMLElement, 'scrollHeight' | 'clientHeight' | 'scrollTop'>) => element.scrollHeight - element.clientHeight - element.scrollTop <= 80
function findAnchor(element: HTMLElement, id: string | undefined) {
  return id ? [...element.querySelectorAll<HTMLElement>('[data-reading-anchor], [data-timeline-id]')].find(item => (item.dataset.readingAnchor || item.dataset.timelineId) === id && item.getBoundingClientRect().height > 0) : undefined
}
function readingPosition(element: HTMLElement, following: boolean): ReadingPosition {
  const { top, bottom } = element.getBoundingClientRect()
  const intersects = (item: HTMLElement) => { const bounds = item.getBoundingClientRect(); return bounds.height > 0 && bounds.bottom > top && bounds.top < bottom }
  const anchor = [...element.querySelectorAll<HTMLElement>('[data-reading-anchor]')].find(intersects) || [...element.querySelectorAll<HTMLElement>('[data-timeline-id]')].find(intersects)
  const fallback = anchor?.closest<HTMLElement>('[data-timeline-id]')
  return { top: element.scrollTop, following, anchor: anchor?.dataset.readingAnchor || anchor?.dataset.timelineId, offset: anchor ? anchor.getBoundingClientRect().top - top : undefined,
    fallbackAnchor: fallback?.dataset.timelineId, fallbackOffset: fallback ? fallback.getBoundingClientRect().top - top : undefined }
}
/** live becomes true only after history is loaded; existing row IDs never replay their entrance. */
export function useConversationScroll(scope: string, itemIds: string[], stamp: number, live = false) {
  const scroller = useRef<HTMLDivElement>(null), content = useRef<HTMLDivElement>(null), positions = useRef(new Map<string, ReadingPosition>())
  const follow = useRef(true), previousIds = useRef(new Set<string>()), [following, setFollowing] = useState(true), [unread, setUnread] = useState(0)
  const pendingRestore = useRef<ReadingPosition | undefined>(undefined)
  const layoutFrame = useRef<number | undefined>(undefined), jumpFrame = useRef<number | undefined>(undefined), jumping = useRef(false), readingIntent = useRef(false)
  const programmaticTop = useRef<number | undefined>(undefined)
  const motionReady = useRef(false), entrances = useRef(new Set<Animation>())
  const populated = useRef(itemIds.length > 0); populated.current = itemIds.length > 0
  const reduced = useReducedMotion()
  const writeScrollTop = useCallback((element: HTMLElement, top: number) => {
    element.scrollTop = top; programmaticTop.current = element.scrollTop
  }, [])
  const save = useCallback(() => {
    const element = scroller.current
    if (!element) return
    positions.current.set(scope, readingPosition(element, follow.current))
  }, [scope])
  const onScroll = useCallback(() => {
    if (!scroller.current || pendingRestore.current || jumping.current) return
    const element = scroller.current, atEnd = element.scrollHeight - element.clientHeight - element.scrollTop <= 2
    // Browser scroll callbacks can arrive after the next animation tick. Preserve the original
    // reading offset instead of saving geometry that already belongs to a later layout frame.
    if (programmaticTop.current !== undefined && Math.abs(element.scrollTop - programmaticTop.current) < .5) return
    programmaticTop.current = undefined
    // An upward reading gesture must not be undone by the near-bottom tolerance.
    follow.current = readingIntent.current ? atEnd : nearBottom(element)
    if (atEnd) readingIntent.current = false
    setFollowing(follow.current)
    if (follow.current) setUnread(0)
    save()
  }, [save])
  const synchronizeLayout = useCallback(() => {
    const element = scroller.current
    if (!element || pendingRestore.current || jumping.current) return
    if (follow.current) writeScrollTop(element, populated.current ? element.scrollHeight : 0)
    else {
      const saved = positions.current.get(scope), primary = findAnchor(element, saved?.anchor), anchor = primary || findAnchor(element, saved?.fallbackAnchor), offset = primary ? saved?.offset : saved?.fallbackOffset
      if (anchor && offset !== undefined) writeScrollTop(element, element.scrollTop + anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - offset)
    }
  }, [scope, writeScrollTop])
  const scheduleLayout = useCallback(() => {
    if (layoutFrame.current !== undefined) return
    if (typeof window.requestAnimationFrame !== 'function') { synchronizeLayout(); return }
    layoutFrame.current = window.requestAnimationFrame(() => { layoutFrame.current = undefined; synchronizeLayout() })
  }, [synchronizeLayout])
  useLayoutEffect(() => {
    const saved = positions.current.get(scope), element = scroller.current
    if (jumpFrame.current !== undefined) window.cancelAnimationFrame?.(jumpFrame.current)
    jumping.current = false; jumpFrame.current = undefined; readingIntent.current = false; motionReady.current = false; programmaticTop.current = undefined
    entrances.current.forEach(animation => animation.cancel()); entrances.current.clear()
    pendingRestore.current = saved && !saved.following ? saved : undefined
    follow.current = saved?.following ?? true; setFollowing(follow.current); setUnread(0); previousIds.current = new Set(itemIds)
    if (element) {
      const anchor = findAnchor(element, saved?.anchor)
      writeScrollTop(element, follow.current ? element.scrollHeight : saved?.top || 0)
      if (anchor && saved?.offset !== undefined) writeScrollTop(element, element.scrollTop + anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - saved.offset)
    }
  }, [scope])
  useLayoutEffect(() => {
    const element = scroller.current
    if (!element) return
    if (pendingRestore.current && itemIds.length) {
      const saved = pendingRestore.current
      const anchor = findAnchor(element, saved.anchor)
      writeScrollTop(element, saved.top)
      if (anchor && saved.offset !== undefined) writeScrollTop(element, element.scrollTop + anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - saved.offset)
      pendingRestore.current = undefined
    }
    const additions = itemIds.filter(id => !previousIds.current.has(id))
    if (live && motionReady.current && !reduced && additions.length && content.current) {
      const newIds = new Set(additions)
      for (const row of content.current.querySelectorAll<HTMLElement>('[data-timeline-id]')) {
        if (!newIds.has(row.dataset.timelineId || '') || typeof row.animate !== 'function') continue
        const animation = row.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'translateY(0)' }], { duration: timelineMotion.entrance, easing: timelineMotion.easing })
        entrances.current.add(animation)
        void animation.finished.then(() => entrances.current.delete(animation)).catch(() => entrances.current.delete(animation))
      }
    }
    motionReady.current = live
    previousIds.current = new Set(itemIds)
    if (follow.current) { if (!jumping.current) writeScrollTop(element, populated.current ? element.scrollHeight : 0); setUnread(0) }
    else if (additions.length) setUnread(value => value + additions.length)
    scheduleLayout()
  }, [stamp, itemIds, live, reduced, scheduleLayout, writeScrollTop])
  useLayoutEffect(() => {
    const element = scroller.current, body = content.current
    if (!element || !body) return
    const stopFollowing = () => {
      if (jumpFrame.current !== undefined) window.cancelAnimationFrame?.(jumpFrame.current)
      jumpFrame.current = undefined; jumping.current = false; readingIntent.current = true; programmaticTop.current = undefined
      follow.current = false; setFollowing(false); save()
    }
    const readWithKey = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.altKey || event.metaKey || !['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) return
      if (event.target instanceof HTMLElement && event.target.closest('input, textarea, select, [contenteditable="true"]')) return
      stopFollowing()
    }
    element.addEventListener('wheel', stopFollowing, { passive: true }); element.addEventListener('touchstart', stopFollowing, { passive: true }); element.addEventListener('keydown', readWithKey); element.addEventListener('pointerdown', stopFollowing, { passive: true })
    body.addEventListener(timelineLayoutEvent, scheduleLayout)
    const observer = window.ResizeObserver ? new ResizeObserver(scheduleLayout) : undefined
    observer?.observe(body)
    return () => {
      element.removeEventListener('wheel', stopFollowing); element.removeEventListener('touchstart', stopFollowing); element.removeEventListener('keydown', readWithKey); element.removeEventListener('pointerdown', stopFollowing)
      body.removeEventListener(timelineLayoutEvent, scheduleLayout); observer?.disconnect()
      if (layoutFrame.current !== undefined) window.cancelAnimationFrame?.(layoutFrame.current)
      if (jumpFrame.current !== undefined) window.cancelAnimationFrame?.(jumpFrame.current)
      layoutFrame.current = undefined; jumpFrame.current = undefined; jumping.current = false
    }
  }, [scope, save, scheduleLayout])
  useLayoutEffect(() => {
    if (!reduced) return
    entrances.current.forEach(animation => animation.cancel()); entrances.current.clear()
    if (jumping.current) {
      if (jumpFrame.current !== undefined) window.cancelAnimationFrame?.(jumpFrame.current)
      jumpFrame.current = undefined; jumping.current = false
      if (scroller.current && follow.current) writeScrollTop(scroller.current, scroller.current.scrollHeight)
    }
  }, [reduced, writeScrollTop])
  useLayoutEffect(() => () => { entrances.current.forEach(animation => animation.cancel()); entrances.current.clear() }, [])
  const jumpToLatest = useCallback(() => {
    follow.current = true; readingIntent.current = false; setFollowing(true); setUnread(0)
    const element = scroller.current
    if (!element) return
    if (jumpFrame.current !== undefined) window.cancelAnimationFrame?.(jumpFrame.current)
    if (reduced || typeof window.requestAnimationFrame !== 'function') { writeScrollTop(element, populated.current ? element.scrollHeight : 0); jumping.current = false; save(); return }
    const from = element.scrollTop, began = performance.now()
    jumping.current = true
    const step = (now: number) => {
      const progress = Math.min(1, Math.max(0, (now - began) / timelineMotion.entrance)), eased = 1 - (1 - progress) ** 3
      writeScrollTop(element, from + (Math.max(0, element.scrollHeight - element.clientHeight) - from) * eased)
      if (progress < 1) jumpFrame.current = window.requestAnimationFrame(step)
      else { jumpFrame.current = undefined; jumping.current = false; save() }
    }
    jumpFrame.current = window.requestAnimationFrame(step)
  }, [reduced, save, writeScrollTop])
  return { scroller, content, following, unread, onScroll, jumpToLatest }
}
