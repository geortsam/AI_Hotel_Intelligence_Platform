import { useEffect, useState } from 'react'

/**
 * Whether the viewport is narrow enough to need a different structure, not just a narrower one.
 *
 * ## Why this is JavaScript rather than CSS
 *
 * Almost everything responsive in this application is CSS, and should be. This is the
 * exception, because the bookings list does not merely *restyle* below 768px -- it renders a
 * different accessible structure. A seven-column table cannot be made readable on a 375px
 * screen by narrowing it, and the two CSS-only alternatives both cost something real:
 *
 * * rendering both structures and hiding one leaves the hidden copy in the DOM, where a
 *   screen reader may still reach it and every row exists twice;
 * * restacking `<td>`s with `content: attr(data-label)` keeps one DOM but breaks the table
 *   semantics exactly where they are needed, and generated content is announced
 *   inconsistently.
 *
 * So the decision is made once, here, and one structure is rendered.
 *
 * ## The breakpoint
 *
 * 767px is the "mobile" boundary recorded in `tokens.css`, quoted rather than reinvented.
 *
 * `matchMedia` is feature-detected. It exists in every browser this targets, but jsdom does
 * not evaluate media queries, so the default has to be the one that is safe when nothing can
 * be measured -- the table, which is the fuller structure and the one whose semantics the
 * tests assert. A test that wants the compact path stubs `matchMedia` explicitly.
 */
export function useIsCompact(maxWidth = 767): boolean {
  const query = `(max-width: ${maxWidth}px)`

  const [isCompact, setIsCompact] = useState(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return false
    }
    return window.matchMedia(query).matches
  })

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return
    }
    const list = window.matchMedia(query)
    const onChange = (event: MediaQueryListEvent) => {
      setIsCompact(event.matches)
    }
    // Read once on mount as well: the query can have changed between the initial state above
    // and this effect running, and on a rotate that difference is the whole layout.
    setIsCompact(list.matches)
    list.addEventListener('change', onChange)
    return () => {
      list.removeEventListener('change', onChange)
    }
  }, [query])

  return isCompact
}
