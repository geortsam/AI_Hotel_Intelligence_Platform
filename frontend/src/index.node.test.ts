import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * The two document-level facts the accessibility checks cannot see (A4).
 *
 * `src/test/axe.ts` turns `html-has-lang` and `document-title` off, because a page under test is
 * mounted into jsdom's bare document rather than into `index.html`. They are only safe to turn
 * off if the real document satisfies them -- so it is asserted here, against the file the
 * browser actually loads.
 */
const INDEX_HTML = readFileSync(join(__dirname, '..', 'index.html'), 'utf8')

describe('index.html', () => {
  it('declares the page language, so screen readers pronounce it correctly', () => {
    expect(INDEX_HTML).toMatch(/<html[^>]*\slang="en"/)
  })

  it('has a non-empty document title, so the tab and the window are named', () => {
    const title = INDEX_HTML.match(/<title>([^<]*)<\/title>/)
    expect(title).not.toBeNull()
    expect(title![1]!.trim()).not.toBe('')
  })
})
