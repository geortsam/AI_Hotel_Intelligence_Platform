import { useCallback, useLayoutEffect, useState } from 'react'

/**
 * The rendered width of an element, in whole CSS pixels, or `null` before it is known.
 *
 * Exists for the hand-drawn charts. They used to draw in a fixed 720-unit viewBox and let the
 * SVG scale to its column, which scaled their 11px axis labels with it: in Analytics' three
 * columns the labels came out near 4.6px, unreadable (UI-0 baseline, 2026-11-28). Drawing in a
 * viewBox as wide as the element itself keeps one user unit equal to one pixel, so text and
 * strokes stay the size the stylesheet says at every width.
 *
 * A callback ref rather than a ref object, because a chart renders its empty state without
 * the measured element and may gain it later; the measurement follows the element whenever it
 * appears. Measured in a layout effect, so the first paint already uses the real width.
 * `ResizeObserver` is feature-detected: jsdom has neither layout nor observer, so there the
 * width stays `null` and callers draw at their fallback, as every existing test expects.
 */
export function useElementWidth<T extends HTMLElement>(): [(element: T | null) => void, number | null] {
  const [element, setElement] = useState<T | null>(null)
  const [width, setWidth] = useState<number | null>(null)
  const ref = useCallback((next: T | null) => {
    setElement(next)
  }, [])

  useLayoutEffect(() => {
    if (element === null) {
      return
    }
    const measure = () => {
      const next = Math.round(element.clientWidth)
      // jsdom reports 0 for everything; 0 is "unknown", never a width to draw at.
      setWidth((previous) => (next > 0 && next !== previous ? next : previous))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') {
      return
    }
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => {
      observer.disconnect()
    }
  }, [element])

  return [ref, width]
}
