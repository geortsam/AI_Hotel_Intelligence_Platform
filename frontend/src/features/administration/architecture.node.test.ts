import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * What this feature must NOT do, asserted against its own source.
 *
 * `administration.test.tsx` proves that a particular screen sends a particular body. It
 * cannot prove that no code path anywhere invents a capability the API lacks, or that a
 * credential never reaches somewhere it would be logged. Five properties this stage is asked
 * to hold are properties of the code, so they are checked in the code:
 *
 * 1. **No authorization is decided here.** The backend is the only authority, and the
 *    frontend is never told the caller's role -- so a role comparison in this feature would
 *    be a guess layered over the real decision.
 * 2. **No capability the API lacks is offered.** There is no user search, no invitation, no
 *    password reset, no account disable, no account delete, and no cross-hotel view of one
 *    person. Each was checked against the live API and each is absent.
 * 3. **No credential is stored, logged or put in a URL.** Passwords pass from the field to
 *    the request body and nowhere else.
 * 4. **The last-owner rule is never predicted.** Counting owners in the browser would be a
 *    guess about a number another administrator may already be changing.
 * 5. **The token seam has exactly one caller.** `adoptToken` exists for the password change
 *    and must not become a general way to write the session.
 */

/*
 * Resolved from the runner's working directory, which vitest sets to the package root.
 * `import.meta.url` is not a `file:` URL under the jsdom environment.
 */
const ROOT = process.cwd()

const DIRECTORIES = [
  join(ROOT, 'src', 'features', 'administration'),
  join(ROOT, 'src', 'services', 'members'),
]

const EXTRA_FILES = [
  join(ROOT, 'src', 'pages', 'AdministrationPage.tsx'),
  join(ROOT, 'src', 'types', 'member.ts'),
  join(ROOT, 'src', 'services', 'auth', 'authService.ts'),
]

/** Every `.ts`/`.tsx` file this feature owns, minus its own tests. */
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
 * These files explain at length *why* they do not reset anybody's password -- so a naive
 * search finds the prose promising the absence and fails the test proving it. Only
 * executable text is searched.
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
    const service = sources.find((file) => file.name === 'memberService.ts')!
    const stripped = code(service.text)
    expect(service.text).toMatch(/deliberately no flat one/)
    expect(stripped).not.toMatch(/deliberately no flat one/)
    expect(stripped).toMatch(/api\.get<Page<Member>>/)
    expect(stripped).toMatch(/api\.delete<void>/)
  })

  it('leaves most of every file behind', () => {
    for (const file of sources) {
      expect(code(file.text).length, `${file.name} was over-stripped`).toBeGreaterThan(
        file.text.length / 5,
      )
    }
  })

  it('detects a role comparison where one really exists', () => {
    // The positive control. If this stops matching, the negative assertions below would stop
    // catching a real breach.
    const control = "if (user.role === 'owner') { return true }"
    expect(code(control)).toMatch(/role\s*===\s*['"]owner['"]/)
  })

  it('covers every source file this stage added or extended', () => {
    expect(sources.map((file) => file.name).sort()).toEqual([
      'AddMemberForm.tsx',
      'AdministrationPage.tsx',
      'ChangePasswordForm.tsx',
      'MemberList.tsx',
      'authService.ts',
      'member.ts',
      'memberService.ts',
      'useMembers.ts',
    ])
  })
})

describe('no authorization is decided in the browser', () => {
  it('compares no role against a required one', () => {
    // Rendering `member.role` is the point of the screen; *branching* on it to decide what
    // may be done is the defect. The patterns below are what that branch looks like.
    const deciding = [
      /role\s*===\s*['"](?:owner|manager|staff|viewer)['"]/,
      /role\s*!==\s*['"](?:owner|manager|staff|viewer)['"]/,
      /\bcanEdit\b|\bcanManage\b|\bcanRemove\b|\bisOwner\b|\bisManager\b|\bhasRole\b/,
      /\brank\b|outranks/i,
    ]
    for (const file of sources) {
      const stripped = code(file.text)
      for (const pattern of deciding) {
        expect(stripped, `${file.name} must not decide authorization`).not.toMatch(pattern)
      }
    }
  })

  it('never hides a control behind a guess about the caller’s role', () => {
    const page = named('AdministrationPage.tsx')
    // The controls are rendered unconditionally on the data; only in-flight state and the
    // absence of a hotel gate them.
    expect(page).toMatch(/disabled=\{members\.pending !== null\}/)
    expect(page).not.toMatch(/\?\s*null\s*:\s*<Button[^>]*Add member/)
  })
})

describe('no capability the API lacks is offered', () => {
  it.each([
    ['a user search', /\/users\b|userSearch|searchUsers/],
    ['an invitation flow', /\binvite\b|invitation/i],
    ['a password reset for somebody else', /reset[-_]?password|forgot[-_]?password|sendReset/i],
    ['account creation', /\/auth\/register|registerUser|createAccount/],
    ['account deletion', /deleteAccount|deleteUser|removeAccount/],
    ['disabling an account', /setActive|disableAccount|deactivateUser/],
    ['a flat memberships route', /memberships\b/],
  ])('contains no %s', (label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not offer ${label}`).not.toMatch(pattern)
    }
  })

  it('calls exactly the four member endpoints and no others', () => {
    const service = named('memberService.ts')
    const calls = service.match(/api\.(get|post|patch|delete)</g) ?? []
    expect(calls.sort()).toEqual(['api.delete<', 'api.get<', 'api.patch<', 'api.post<'])
    // Every path is nested under its hotel; there is no flat route to call.
    for (const path of service.match(/`\/[^`]*`/g) ?? []) {
      expect(path).toMatch(/^`\/hotels\/\$\{hotelPublicId\}\/members/)
    }
  })

  it('sends a page and nothing else', () => {
    const service = named('memberService.ts')
    expect(service).toMatch(/query: \{ page, page_size: pageSize \}/)
    expect(service.match(/query:/g)).toHaveLength(1)
    const invented = /\b(?:search|sort_by|sort|order_by|q|filter|role_filter)\s*:/
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not send ${invented}`).not.toMatch(invented)
    }
  })

  it('never asks for a page larger than the router allows', () => {
    expect(named('memberService.ts')).toMatch(/MAX_PAGE_SIZE = 100/)
    const page = named('AdministrationPage.tsx')
    const options = /PAGE_SIZE_OPTIONS = \[([0-9, ]+)\]/.exec(page)![1]!
    for (const size of options.split(',')) {
      expect(Number(size.trim())).toBeLessThanOrEqual(100)
    }
  })
})

describe('the last-owner rule is the server’s', () => {
  it('counts no owners in the browser', () => {
    for (const file of sources) {
      const stripped = code(file.text)
      expect(stripped, `${file.name} must not count owners`).not.toMatch(
        /ownerCount|owner_count|countOwners/,
      )
      // A filter over the page followed by a length check is the shape this would take.
      expect(stripped, `${file.name} must not derive an owner count`).not.toMatch(
        /filter\([^)]*owner[^)]*\)\s*\.length/i,
      )
    }
  })

  it('renders the conflict instead of predicting it', () => {
    const page = named('AdministrationPage.tsx')
    expect(page).toMatch(/conflict: \{/)
    expect(page).toMatch(/would be left with no owner/)
  })
})

describe('credentials go from the field to the body and nowhere else', () => {
  it.each([
    ['localStorage', /\blocalStorage\b/],
    ['sessionStorage', /\bsessionStorage\b/],
    ['console', /\bconsole\.\w+\s*\(/],
    ['a hand-built Authorization header', /Bearer/],
    ['a raw fetch', /\bfetch\s*\(/],
    ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
    ['innerHTML', /\.innerHTML\b/],
    ['eval', /\beval\s*\(/],
    ['new Function', /new\s+Function\s*\(/],
  ])('contains no %s', (label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not contain ${label}`).not.toMatch(pattern)
    }
  })

  it('never puts a password in a query parameter', () => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not query by password`).not.toMatch(
        /query:[^}]*password/i,
      )
    }
  })

  it('sends the password only as a body, on the one route that takes one', () => {
    const service = named('authService.ts')
    expect(service).toMatch(/'\/auth\/change-password'/)
    expect(service).toMatch(/credentialInBody: true/)
    // Exactly one route carries `credentialInBody`, and it is that one.
    expect(service.match(/credentialInBody/g)).toHaveLength(1)
  })

  it('keeps the password fields masked and out of autofill history', () => {
    const form = named('ChangePasswordForm.tsx')
    expect(form.match(/type="password"/g)).toHaveLength(3)
    expect(form).toMatch(/autoComplete="current-password"/)
    expect(form.match(/autoComplete="new-password"/g)).toHaveLength(2)
  })
})

describe('the session seam has one caller', () => {
  it('adopts a token only in the password form', () => {
    const users = sources.filter((file) => /adoptToken/.test(code(file.text)))
    expect(users.map((file) => file.name)).toEqual(['ChangePasswordForm.tsx'])
    expect(named('ChangePasswordForm.tsx').match(/adoptToken\(/g)).toHaveLength(1)
  })

  it('adopts it before anything else can issue a request', () => {
    const form = named('ChangePasswordForm.tsx')
    // The old token is dead the moment the server answers, so the replacement is stored
    // before the form clears itself or reports success.
    expect(form).toMatch(
      /adoptToken\(token\.access_token\)[\s\S]{0,200}setOutcome\(\{ kind: 'done' \}\)/,
    )
  })
})

describe('no internal identifier is used', () => {
  it('addresses every member by the account’s public id', () => {
    const internal = /\b(?:user_id|hotel_id|membership_id|\w+\.id)\b/
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not use an internal id`).not.toMatch(internal)
    }
    expect(named('memberService.ts')).toMatch(/userPublicId/)
  })

  it('routes every request through the shared API client', () => {
    const importers = sources.filter((file) => /@\/services\/api\/client/.test(file.text))
    expect(importers.map((file) => file.name).sort()).toEqual([
      'authService.ts',
      'memberService.ts',
    ])
  })
})
