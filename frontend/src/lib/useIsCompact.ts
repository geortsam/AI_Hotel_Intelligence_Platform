import { useEffect, useState } from 'react'

/**
 * Whether the viewport is narrow enough to need a different structure, not just a narrower one.
 *
 * ## Why this is JavaScript rather than CSS
 *
 * Almost everything responsive in this application is CSS, and should be. This is the
 * exception, because the bookings list does not merely *restyle* below 1024px -- it renders a
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
 * 1023px: below the "desktop" boundary recorded in `tokens.css`, where the sidebar is a drawer.
 * It was 767px until UI-5, and the UI-0 baseline showed what that cost at 768px: every wide
 * table (most set a 880-960px minimum width) ran past its card into a 720px content area,
 * cutting columns and, on the platform catalogues, the row actions. Between 768 and 1023px
 * the card structure fits and the table does not, so the card structure is what renders.
 *
 * `matchMedia` is feature-detected. It exists in every browser this targets, but jsdom does
 * not evaluate media queries, so the default has to be the one that is safe when nothing can
 * be measured -- the table, which is the fuller structure and the one whose semantics the
 * tests assert. A test that wants the compact path stubs `matchMedia` explicitly.
 */
export function useIsCompact(maxWidth = 1023): boolean {
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
