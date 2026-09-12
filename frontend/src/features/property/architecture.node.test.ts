import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * What this feature must NOT do, asserted against its own source.
 *
 * ## Why a source test rather than a rendering test
 *
 * `property.test.tsx` proves that a particular screen sends a particular body. It cannot
 * prove that no screen ever sends `slug` to `PATCH`, or that no code path anywhere prices a
 * stay -- the wrong call might live on a branch no fixture reaches. Four properties this
 * stage is asked to hold are properties of the code, so they are checked in the code:
 *
 * 1. **The immutable identities are never sent to an update.** `slug` on a hotel and `code`
 *    on a room type are both create-only, and both are a 422 on `PATCH` -- verified live.
 *    `is_active` is the mirror image: update-only, a 422 at creation.
 * 2. **Nothing is computed.** A room type's price is a `NUMERIC(14,2)` decimal string from
 *    the column to the screen. No occupancy rate, no availability, no revenue -- this screen
 *    manages definitions, and every figure on it is a stored column.
 * 3. **No query parameter is sent that the endpoints do not have.** Both list routes take
 *    `page` and `page_size` and nothing else, and the backend **ignores an unrecognised
 *    parameter silently** -- `?is_active=true` returned every type with a 200 -- so a filter
 *    invented here would look like it worked.
 * 4. **Hotel deletion is not wired to anything.** The endpoint exists and the service has
 *    the method; no component may reach it. See `PropertyPage` for why.
 *
 * The behavioural half is in `property.test.tsx`.
 */

/*
 * Resolved from the runner's working directory, which vitest sets to the package root.
 * `import.meta.url` is not a `file:` URL under the jsdom environment.
 */
const ROOT = process.cwd()

const DIRECTORIES = [
  join(ROOT, 'src', 'features', 'property'),
  join(ROOT, 'src', 'services', 'hotels'),
  join(ROOT, 'src', 'services', 'roomTypes'),
]

const EXTRA_FILES = [
  join(ROOT, 'src', 'pages', 'PropertyPage.tsx'),
  join(ROOT, 'src', 'types', 'roomType.ts'),
]

/** Every `.ts`/`.tsx` file this feature owns, minus its own tests. */
function propertySources(): { name: string; text: string }[] {
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
 * These files explain at length *why* they never send a slug to `PATCH` -- so a naive search
 * finds the prose promising the absence and fails the test proving it. Only executable text
 * is searched.
 */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
}

const sources = propertySources()

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
    const service = sources.find((file) => file.name === 'roomTypeService.ts')!
    const stripped = code(service.text)
    expect(service.text).toMatch(/No search, no filter, no sort/)
    expect(stripped).not.toMatch(/No search, no filter, no sort/)
    expect(stripped).toMatch(/api\.get<Page<RoomType>>/)
    expect(stripped).toMatch(/api\.delete<void>/)
  })

  it('leaves most of every file behind', () => {
    for (const file of sources) {
      expect(code(file.text).length, `${file.name} was over-stripped`).toBeGreaterThan(
        file.text.length / 5,
      )
    }
  })

  it('detects a parseFloat where one really exists', () => {
    // The positive control. If this stops matching, the negative assertions below would stop
    // catching a real breach.
    const control = 'const rate = parseFloat(row.total_amount)'
    expect(code(control)).toMatch(/\bparseFloat\s*\(/)
  })

  it('covers every source file this stage added or extended', () => {
    expect(sources.map((file) => file.name).sort()).toEqual([
      'HotelForm.tsx',
      'PropertyPage.tsx',
      'RoomTypeForm.tsx',
      'RoomTypeList.tsx',
      'hotelService.ts',
      'roomType.ts',
      'roomTypeService.ts',
      'usePropertyAdmin.ts',
    ])
  })
})

describe('the immutable identities are never sent to an update', () => {
  it('builds a hotel update without a slug', () => {
    const form = named('HotelForm.tsx')
    // The value is displayed -- read from the record, never written into the payload.
    expect(form).toMatch(/value=\{hotel\.slug\}/)
    expect(form).not.toMatch(/\bslug:/)
    // And the update-only field is present, which create refuses.
    expect(form).toMatch(/is_active: draft\.is_active/)
  })

  it('builds a room-type update without a code, and a creation without is_active', () => {
    const form = named('RoomTypeForm.tsx')
    // The two payloads, taken apart so each can be searched for what the other owns.
    const create = /onCreate\?\.\(\{([\s\S]*?)\n    \}\)/.exec(form)![1]!
    const update = /onUpdate\?\.\(\{([\s\S]*?)\n      \}\)/.exec(form)![1]!

    expect(create).toMatch(/code: draft\.code\.trim\(\)/)
    // Update-only, and a 422 at creation.
    expect(create).not.toMatch(/\bis_active\b/)

    expect(update).toMatch(/is_active: draft\.is_active/)
    // Create-only, and a 422 on PATCH.
    expect(update).not.toMatch(/\bcode\b/)
  })

  it('types the two schemas apart rather than sharing one', () => {
    const types = named('roomType.ts')
    expect(types).toMatch(/interface RoomTypeCreateRequest/)
    expect(types).toMatch(/interface RoomTypeUpdateRequest/)
    // A single shared interface would let either field reach either verb.
    expect(types).not.toMatch(/RoomTypeUpdateRequest\s+extends\s+RoomTypeCreateRequest/)
  })
})

describe('the property feature computes nothing', () => {
  it.each([
    ['parseFloat', /\bparseFloat\s*\(/],
    ['parseInt', /\bparseInt\s*\(/],
    ['toFixed', /\.toFixed\s*\(/],
    ['reduce', /\.reduce\s*\(/],
    ['compound addition', /\+=/],
    ['rounding', /\bMath\.(?:round|floor|ceil|abs)\s*\(/],
  ])('contains no %s', (label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not use ${label}`).not.toMatch(pattern)
    }
  })

  it('never converts a money string into a number', () => {
    // `Number()` is legitimate here -- occupancies and bed counts are `int` in the schema --
    // so it is bounded to those fields rather than banned. A `Number(...)` reaching a decimal
    // string is the defect this catches.
    for (const file of sources) {
      const stripped = code(file.text)
      for (const money of ['base_price', 'size_sqm', 'latitude', 'longitude']) {
        expect(stripped, `${file.name} must not numify ${money}`).not.toMatch(
          new RegExp(`Number\\s*\\([^)]*${money}`),
        )
        // Bounded to the identifier itself: the `</Field>` after a price cell is a closing
        // tag, not a division.
        expect(stripped, `${file.name} must not do arithmetic on ${money}`).not.toMatch(
          new RegExp(`${money}\\s*[*/]|[*/]\\s*(?:\\w+\\.)?${money}\\b`),
        )
      }
    }
  })

  it('confines Number() to the integer fields', () => {
    const form = named('RoomTypeForm.tsx')
    const uses = form.match(/Number\s*\(\s*([A-Za-z_.]+)/g) ?? []
    expect(uses.length).toBeGreaterThan(0)
    for (const use of uses) {
      expect(use).toMatch(/Number\s*\(\s*(?:value|draft\.(?:max_occupancy|standard_occupancy|bed_count))/)
    }
  })

  it('names no figure this screen has no business reporting', () => {
    const invented =
      /\b(?:occupancy_rate|occupancyRate|revpar|adr|available_rooms|availableRooms|revenue|forecast)\b/i
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not name ${invented}`).not.toMatch(invented)
    }
  })
})

describe('the property feature sends no parameter the endpoints lack', () => {
  it('lists room types with a page and nothing else', () => {
    const service = named('roomTypeService.ts')
    expect(service).toMatch(/query: \{ page, page_size: pageSize \}/)
    // One `query:` in the file: the list. Every other call is a path plus a body.
    expect(service.match(/query:/g)).toHaveLength(1)
  })

  it('lists hotels with a page and nothing else', () => {
    const service = named('hotelService.ts')
    expect(service).toMatch(/query: \{ page: 1, page_size: HOTEL_PAGE_SIZE \}/)
    expect(service.match(/query:/g)).toHaveLength(1)
  })

  it('names no filter, search or sort parameter anywhere', () => {
    const invented = /\b(?:search|sort_by|sort|order_by|q|filter|is_active_filter)\s*:/
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not send ${invented}`).not.toMatch(invented)
    }
  })

  it('never asks for a page larger than the routers allow', () => {
    expect(named('roomTypeService.ts')).toMatch(/MAX_PAGE_SIZE = 100/)
    const page = named('PropertyPage.tsx')
    const options = /PAGE_SIZE_OPTIONS = \[([0-9, ]+)\]/.exec(page)![1]!
    for (const size of options.split(',')) {
      expect(Number(size.trim())).toBeLessThanOrEqual(100)
    }
  })
})

describe('hotel deletion is not offered', () => {
  it('is never called outside the service that declares it', () => {
    const callers = sources.filter(
      (file) => file.name !== 'hotelService.ts' && /hotelService\.remove/.test(code(file.text)),
    )
    expect(callers.map((file) => file.name)).toEqual([])
  })

  it('is not reachable from the hook either', () => {
    const hook = named('usePropertyAdmin.ts')
    expect(hook).toMatch(/hotelService\.update/)
    expect(hook).not.toMatch(/hotelService\.(?:remove|create)/)
  })
})

describe('the property feature is safe with untrusted text', () => {
  it.each([
    ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
    ['innerHTML', /\.innerHTML\b/],
    ['eval', /\beval\s*\(/],
    ['new Function', /new\s+Function\s*\(/],
    ['a markdown or sanitiser import', /from\s+['"](?:marked|remark|markdown-it|dompurify)/i],
    ['console', /\bconsole\.\w+\s*\(/],
    ['a hand-built Authorization header', /Bearer/],
    ['localStorage', /\blocalStorage\b/],
    ['a raw fetch', /\bfetch\s*\(/],
  ])('contains no %s', (label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not contain ${label}`).not.toMatch(pattern)
    }
  })

  it('routes every request through the shared API client', () => {
    const importers = sources.filter((file) => /@\/services\/api\/client/.test(file.text))
    expect(importers.map((file) => file.name).sort()).toEqual([
      'hotelService.ts',
      'roomTypeService.ts',
    ])
  })

  it('addresses every record by its public identifier', () => {
    const internal = /\b(?:hotel_id|room_type_id|\w+\.id)\b/
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not use an internal id`).not.toMatch(internal)
    }
  })

  it('renders the backend’s own message nowhere', () => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not render error.message`).not.toMatch(
        /\{[^}]*\bError\.message\b[^}]*\}|\{\s*\w*[Ee]rror\.message\s*\}/,
      )
    }
  })
})
