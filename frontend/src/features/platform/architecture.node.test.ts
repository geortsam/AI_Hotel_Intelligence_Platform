import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * What this feature must NOT do, asserted against its own source.
 *
 * `platform.test.tsx` proves that a particular screen sends a particular body. It cannot
 * prove that no code path anywhere scopes a shared catalogue to a hotel, or offers a
 * capability the API lacks. Five properties this stage is asked to hold are properties of the
 * code, so they are checked in the code:
 *
 * 1. **Nothing here is hotel-scoped.** These tables have no `hotel_id`; a hotel segment or a
 *    `hotel_public_id` in any request would be a scope the schema does not model.
 * 2. **No authorization is decided in the browser.** The platform grant is not a hotel role
 *    and is not ranked against one, and the frontend is told neither.
 * 3. **No capability the API lacks is offered** -- no grant management, no audit mutation, no
 *    search, no sort, and no `is_active` on the amenity route that has no such column.
 * 4. **The audit trail is read-only in the code, not merely in the UI.**
 * 5. **The locked finance service is not extended.** Stage 5.8 owns catalogue *reading* for
 *    the ledger; this stage owns catalogue *maintenance*, in its own module.
 */

/*
 * Resolved from the runner's working directory, which vitest sets to the package root.
 * `import.meta.url` is not a `file:` URL under the jsdom environment.
 */
const ROOT = process.cwd()

const DIRECTORIES = [
  join(ROOT, 'src', 'features', 'platform'),
  join(ROOT, 'src', 'services', 'platform'),
]

const EXTRA_FILES = [
  join(ROOT, 'src', 'pages', 'PlatformPage.tsx'),
  join(ROOT, 'src', 'types', 'platform.ts'),
]

/** Every `.ts`/`.tsx` file this stage added, minus its own tests. */
function sourcesOf(): { name: string; text: string }[] {
  const files: { name: string; text: string }[] = []
  for (const directory of DIRECTORIES) {
    for (const entry of readdirSync(directory)) {
      if (!/\.tsx?$/.test(entry) || entry.includes('.test.')) {
        continue
      }
      files.push({ name: entry, text: readFileSync(join(directory, entry), 'utf8') })
    }
  }
  for (const path of EXTRA_FILES) {
    files.push({ name: path.split(/[\\/]/).pop()!, text: readFileSync(path, 'utf8') })
  }
  return files
}

/**
 * Strip comments before matching.
 *
 * These files explain at length *why* nothing here is hotel-scoped -- so a naive search finds
 * the prose promising the absence and fails the test proving it. Only executable text is
 * searched.
 */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
}

const sources = sourcesOf()

function named(name: string): string {
  return code(sources.find((file) => file.name === name)!.text)
}

/**
 * The detector, checked against known positives before it is trusted on negatives.
 *
 * A source scan is only as good as its stripper: if `code()` were too aggressive it would
 * return almost nothing and every assertion below would pass on an empty string.
 */
describe('the source scan itself works', () => {
  it('strips prose but keeps the code', () => {
    const service = sources.find((file) => file.name === 'platformService.ts')!
    const stripped = code(service.text)
    expect(service.text).toMatch(/separate module from/)
    expect(stripped).not.toMatch(/separate module from/)
    expect(stripped).toMatch(/api\.get<Page<Amenity>>/)
    expect(stripped).toMatch(/api\.delete<void>/)
  })

  it('leaves most of every file behind', () => {
    for (const file of sources) {
      expect(code(file.text).length, `${file.name} was over-stripped`).toBeGreaterThan(
        file.text.length / 5,
      )
    }
  })

  it('detects a hotel-scoped path where one really exists', () => {
    // The positive control. If this stops matching, the negative assertions below would stop
    // catching a real breach.
    const control = 'api.get(`/hotels/${hotelPublicId}/amenities`)'
    expect(code(control)).toMatch(/\/hotels\/\$\{/)
  })

  it('covers every source file this stage added', () => {
    expect(sources.map((file) => file.name).sort()).toEqual([
      'AuditTrail.tsx',
      'CatalogueForm.tsx',
      'CatalogueList.tsx',
      'PlatformPage.tsx',
      'platform.ts',
      'platformService.ts',
      'useCatalogue.ts',
      'usePlatformAudit.ts',
    ])
  })
})

describe('nothing here is hotel-scoped', () => {
  it('builds no path under a hotel', () => {
    for (const file of sources) {
      const stripped = code(file.text)
      expect(stripped, `${file.name} must not nest under a hotel`).not.toMatch(/\/hotels\//)
      expect(stripped, `${file.name} must not name a hotel id`).not.toMatch(
        /hotel_public_id|hotelPublicId/,
      )
    }
  })

  it('reads the hotel context only for a display time zone', () => {
    const page = named('PlatformPage.tsx')
    const uses = page.match(/hotelContext\.\w+/g) ?? []
    // One use, and it is the selected hotel's `timezone` -- the operator's working zone, for
    // formatting timestamps that belong to no property.
    expect(uses).toEqual(['hotelContext.selected'])
    expect(page).toMatch(/hotelContext\.selected\?\.timezone \?\? 'UTC'/)
  })

  it('sends no hotel identifier in any body', () => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not send a hotel`).not.toMatch(
        /body:[^}]*hotel/i,
      )
    }
  })
})

describe('no authorization is decided in the browser', () => {
  it('compares no role and checks no grant', () => {
    const deciding = [
      /role\s*===\s*['"]/,
      /\bisPlatformAdmin\b|\bcanWrite\b|\bcanManage\b|\bhasGrant\b|\bisAdmin\b/,
      /HotelRole|PlatformRole/,
      /\brank\b|outranks/i,
    ]
    for (const file of sources) {
      const stripped = code(file.text)
      for (const pattern of deciding) {
        expect(stripped, `${file.name} must not decide authorization`).not.toMatch(pattern)
      }
    }
  })

  it('never hides a write control behind a guess about the grant', () => {
    const page = named('PlatformPage.tsx')
    // Only in-flight state and an open form gate the controls.
    expect(page).toMatch(/disabled=\{catalogue\.pending !== null\}/)
    expect(page).not.toMatch(/\?\s*null\s*:\s*<Button[^>]*Add /)
  })

  it('speaks of the grant rather than of a hotel role in every refusal', () => {
    const page = named('PlatformPage.tsx')
    const copy = /forbidden: \{[\s\S]*?\}/g
    const blocks = page.match(copy) ?? []
    expect(blocks.length).toBeGreaterThan(0)
    for (const block of blocks) {
      expect(block).toMatch(/platform administrator grant/)
      // An owner is not a junior platform administrator, and the copy must not imply it.
      expect(block).not.toMatch(/\bowner role\b|\bmanager role\b/)
    }
  })
})

describe('no capability the API lacks is offered', () => {
  it.each([
    ['grant management', /grantAdmin|revokeAdmin|platform_admins|addPlatformAdmin/],
    ['an audit mutation', /deleteAudit|updateAudit|api\.(post|patch|delete)[^\n]*audit/i],
    ['a search parameter', /\bsearch\s*:/],
    ['a sort parameter', /\b(?:sort|sort_by|order_by)\s*:/],
    ['bulk operations', /bulk|selectAll|checkedRows/i],
    ['an import or export', /\bexport(?:Csv|ToCsv)?\s*\(|\bimportFrom/i],
  ])('contains no %s', (label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not offer ${label}`).not.toMatch(pattern)
    }
  })

  it('sends is_active only to the two routes that have it', () => {
    const service = named('platformService.ts')
    // Three list calls; exactly two mention the filter.
    expect(service.match(/is_active/g)).toHaveLength(2)
    const amenityList = /listAmenities\([\s\S]*?\n  \},/.exec(service)![0]
    expect(amenityList).not.toMatch(/is_active/)
    expect(amenityList).toMatch(/query: \{ page: query\.page, page_size: query\.pageSize \}/)
  })

  it('declares the difference rather than hiding it', () => {
    const hook = named('useCatalogue.ts')
    expect(hook).toMatch(/supportsActiveFilter: false/)
    expect(hook.match(/supportsActiveFilter: true/g)).toHaveLength(2)
    // And the page renders the control only when the route has one.
    expect(named('PlatformPage.tsx')).toMatch(/catalogue\.supportsActiveFilter \?/)
  })

  it('never asks for a page larger than the routers allow', () => {
    expect(named('platformService.ts')).toMatch(/MAX_PAGE_SIZE = 100/)
    const options = /PAGE_SIZE_OPTIONS = \[([0-9, ]+)\]/.exec(named('PlatformPage.tsx'))![1]!
    for (const size of options.split(',')) {
      expect(Number(size.trim())).toBeLessThanOrEqual(100)
    }
  })

  it('offers only the audit actions that can occur at platform scope', () => {
    const types = named('platform.ts')
    const list = /PLATFORM_AUDIT_ACTIONS[^=]*= \[([\s\S]*?)\]/.exec(types)![1]!
    expect(list).toMatch(/auth\.password_changed/)
    // A hotel-only action would return an empty page forever, which is a worse control than
    // none at all.
    expect(list).not.toMatch(/booking\.|payment\.|membership\./)
  })
})

describe('the audit trail is read-only in the code', () => {
  it('issues only a GET', () => {
    const service = named('platformService.ts')
    const auditCall = /listPlatformAuditEvents\([\s\S]*?\n  \},/.exec(service)![0]
    expect(auditCall).toMatch(/api\.get</)
    expect(auditCall).not.toMatch(/api\.(post|patch|delete)</)
  })

  it('has no write in its hook at all', () => {
    const hook = named('usePlatformAudit.ts')
    expect(hook).not.toMatch(/api\.(post|patch|delete)|create|update|remove|delete/)
  })

  it('renders the details object as text, never as markup', () => {
    const trail = named('AuditTrail.tsx')
    expect(trail).toMatch(/String\(value\)/)
    expect(trail).not.toMatch(/dangerouslySetInnerHTML|JSON\.parse/)
  })
})

describe('the locked finance service is not extended', () => {
  it('routes catalogue maintenance through this stage’s own module', () => {
    const importers = sources.filter((file) => /@\/services\/api\/client/.test(file.text))
    expect(importers.map((file) => file.name)).toEqual(['platformService.ts'])
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not reach into finance`).not.toMatch(
        /financeService/,
      )
    }
  })

  it('reuses the locked response types rather than restating them', () => {
    // The category rows are the same rows Stage 5.8 already transcribed; a second definition
    // would be a second thing to keep in step.
    expect(named('platformService.ts')).toMatch(
      /import type \{ ExpenseCategory, RevenueCategory \} from '@\/types\/finance'/,
    )
    expect(named('platform.ts')).not.toMatch(/interface (Revenue|Expense)Category\b/)
  })
})

describe('the feature is safe with untrusted text', () => {
  it.each([
    ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
    ['innerHTML', /\.innerHTML\b/],
    ['eval', /\beval\s*\(/],
    ['new Function', /new\s+Function\s*\(/],
    ['console', /\bconsole\.\w+\s*\(/],
    ['a hand-built Authorization header', /Bearer/],
    ['localStorage', /\blocalStorage\b/],
    ['sessionStorage', /\bsessionStorage\b/],
    ['a raw fetch', /\bfetch\s*\(/],
  ])('contains no %s', (label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not contain ${label}`).not.toMatch(pattern)
    }
  })

  it('uses no internal identifier', () => {
    const internal = /\b(?:amenity_id|category_id|user_id|hotel_id|\w+\.id)\b/
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not use an internal id`).not.toMatch(internal)
    }
  })
})
