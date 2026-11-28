import { useLayoutEffect, useRef, useState, type RefObject } from 'react'

/**
 * Whether an element's content is wider than the element itself, kept current as either
 * resizes.
 *
 * ## Why a scroll box needs to know
 *
 * A table that is wider than its box scrolls sideways inside the box. A mouse or a trackpad
 * can scroll it, but a keyboard can scroll only an element that can take focus, and a `<div>`
 * takes focus only with a `tabindex`. A `tabindex` on a box that does *not* scroll puts a tab
 * stop with nothing to do on the keyboard path. So the box becomes focusable, and is named
 * as a region, exactly while it overflows. The rest of the time it is an ordinary container.
 * WCAG 2.1.1 (Keyboard) is the requirement; axe's `scrollable-region-focusable` is the
 * rule that checks it.
 *
 * ## How it measures
 *
 * The element is overflowing when its `scrollWidth` exceeds its `clientWidth`. A
 * `ResizeObserver` re-measures whenever the element or its first child (the table) changes
 * size. That covers a viewport resize, a font swap and new rows alike.
 *
 * `ResizeObserver` is feature-detected. jsdom has no layout and no observer, so there the
 * element is measured once and reads as not overflowing: the fuller, plainer structure is
 * the safe default, as with `useIsCompact`.
 */
export function useHorizontalOverflow<T extends HTMLElement>(): [RefObject<T>, boolean] {
  const ref = useRef<T>(null)
  const [overflowing, setOverflowing] = useState(false)

  useLayoutEffect(() => {
    const element = ref.current
    if (element === null) {
      return
    }
    // Whole pixels on both sides: `scrollWidth` and `clientWidth` are integers already, so a
    // sub-pixel layout never reads as an overflow.
    const measure = () => {
      setOverflowing(element.scrollWidth > element.clientWidth)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') {
      return
    }
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    if (element.firstElementChild) {
      observer.observe(element.firstElementChild)
    }
    return () => {
      observer.disconnect()
    }
  }, [])

  return [ref, overflowing]
}
