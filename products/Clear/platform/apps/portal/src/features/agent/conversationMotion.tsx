import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { useReducedMotion } from './RunStatus'

export const timelineLayoutEvent = 'clear:timeline-layout'
export const timelineMotion = { disclosure: 180, entrance: 240, easing: 'cubic-bezier(.2,.8,.2,1)' }

/** Keep layout observers on the same frame as a disclosure, including its final height. */
export function announceTimelineLayout(element: HTMLElement) {
  element.dispatchEvent(new CustomEvent(timelineLayoutEvent, { bubbles: true }))
}

/** Animate the space reclaimed when live tool rows become a completed summary. */
export function useActivityListMotion(identity: string, signature: string) {
  const list = useRef<HTMLOListElement>(null), animation = useRef<Animation | undefined>(undefined)
  const previous = useRef<{ identity: string; signature: string; height: number } | undefined>(undefined)
  const reduced = useReducedMotion()
  useLayoutEffect(() => {
    const element = list.current
    if (!element) { previous.current = undefined; return }
    const old = previous.current, changed = old?.identity === identity && old.signature !== signature
    if (animation.current && !changed && !reduced) return
    // Read the current visual height before cancellation so rapid updates can reverse smoothly.
    const from = animation.current ? element.getBoundingClientRect().height : old?.height ?? element.getBoundingClientRect().height
    animation.current?.cancel(); animation.current = undefined; element.style.overflow = ''
    const to = element.getBoundingClientRect().height
    previous.current = { identity, signature, height: to }
    if (!changed || reduced || typeof element.animate !== 'function' || Math.abs(from - to) < .5) return
    announceTimelineLayout(element); element.style.overflow = 'hidden'
    const current = element.animate([{ height: `${from}px` }, { height: `${to}px` }], { duration: timelineMotion.disclosure, easing: timelineMotion.easing })
    animation.current = current
    void current.finished.then(() => {
      if (animation.current !== current) return
      animation.current = undefined; element.style.overflow = ''
      previous.current = { identity, signature, height: element.getBoundingClientRect().height }
      announceTimelineLayout(element)
    }).catch(() => {})
  })
  useLayoutEffect(() => {
    const element = list.current
    if (!element || !window.ResizeObserver) return
    const observer = new ResizeObserver(() => {
      // Disclosure animations can resize this list independently; retain their settled height.
      if (!animation.current && previous.current) previous.current.height = element.getBoundingClientRect().height
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [identity, signature])
  useLayoutEffect(() => () => { animation.current?.cancel(); if (list.current) list.current.style.overflow = '' }, [])
  return list
}

export function AnimatedDisclosure({ id, open, children }: { id: string; open: boolean; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null), previousOpen = useRef(open), initialized = useRef(false), animation = useRef<Animation | undefined>(undefined)
  const reduced = useReducedMotion()
  useLayoutEffect(() => {
    const element = panel.current
    if (!element) return
    const wasHidden = element.hidden, from = wasHidden ? 0 : element.getBoundingClientRect().height
    const changed = previousOpen.current !== open
    previousOpen.current = open
    animation.current?.cancel(); animation.current = undefined
    if (!initialized.current || !changed || reduced || typeof element.animate !== 'function') {
      initialized.current = true; element.hidden = !open; element.style.height = ''
      announceTimelineLayout(element)
      return
    }
    announceTimelineLayout(element)
    element.hidden = false; element.style.height = 'auto'
    const to = open ? element.scrollHeight : 0
    element.style.height = open ? '' : '0px'
    const current = element.animate([{ height: `${from}px`, opacity: open ? .65 : 1 }, { height: `${to}px`, opacity: open ? 1 : .65 }], {
      duration: timelineMotion.disclosure, easing: timelineMotion.easing,
    })
    animation.current = current
    void current.finished.then(() => {
      if (animation.current !== current) return
      element.hidden = !open; element.style.height = ''; animation.current = undefined
      announceTimelineLayout(element)
    }).catch(() => {})
  }, [open, reduced])
  useLayoutEffect(() => () => animation.current?.cancel(), [])
  return <div ref={panel} id={id} className="agent-tool-disclosure-panel" aria-hidden={!open} inert={!open}>{children}</div>
}
