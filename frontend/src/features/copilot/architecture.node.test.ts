import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * What the copilot screen must never do, asserted against its own source (Stage 7.13).
 *
 * ## Why a source test rather than a rendering test
 *
 * `copilot.test.tsx` proves that particular fixtures render safely. It cannot prove that no
 * code path ever decides a role, renders markup, writes to browser storage or builds a link
 * from model text — a path the fixtures never reach would pass unnoticed. Each of those is a
 * property of the code, so it is checked in the code, as every earlier feature does.
 *
 * ## What each rule protects
 *
 * * **No authorization decision.** The frontend is told no role (`/auth/me` and `/hotels`
 *   carry none), and the server's role-filtered tool catalogue is the only authority. A
 *   client-side check would be a second, unenforceable decision.
 * * **No markup from model or document text.** Answers are model output and excerpts are
 *   untrusted documents. `dangerouslySetInnerHTML`, a Markdown renderer or an auto-linker
 *   would turn either into an injection surface.
 * * **No browser persistence.** A one-off question is promised not to be stored; a
 *   conversation's retention rule lives on the server. `localStorage` would break both.
 * * **One HTTP seam, no streaming.** Every request goes through `services/api/client.ts`. A
 *   streamed answer would show text before the server's figure and citation checks could
 *   withhold it, so there is no `EventSource`, `WebSocket` or stream reader.
 * * **Citations come from the server.** No code parses `[S1]` out of an answer to build a
 *   request or a link; a citation opens the document the server's citation names.
 * * **The server's words never reach the screen.** Failures are fixed copy chosen by code; no
 *   file reads an error's `.message`.
 */

const ROOT = process.cwd()

const DIRECTORIES = [
  join(ROOT, 'src', 'features', 'copilot'),
  join(ROOT, 'src', 'services', 'copilot'),
  join(ROOT, 'src', 'services', 'knowledge'),
]
const EXTRA_FILES = [
  join(ROOT, 'src', 'pages', 'CopilotPage.tsx'),
  join(ROOT, 'src', 'types', 'copilot.ts'),
  join(ROOT, 'src', 'types', 'knowledge.ts'),
]

function sources(): { name: string; text: string }[] {
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

/** Comments explain the rules at length; only executable text is searched. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
}

const SOURCES = sources()
const byName = (name: string) => SOURCES.find((file) => file.name === name)!

describe('the source scan itself works', () => {
  it('covers every source file of the feature', () => {
    expect(SOURCES.map((file) => file.name).sort()).toEqual([
      'CitationList.tsx',
      'ConversationList.tsx',
      'CopilotPage.tsx',
      'ExchangeView.tsx',
      'ModeSelector.tsx',
      'QuestionForm.tsx',
      'ToolCallList.tsx',
      'copilot.ts',
      'copilotService.ts',
      'exchange.ts',
      'knowledge.ts',
      'knowledgeService.ts',
      'useCitationSource.ts',
      'useCopilot.ts',
      'vocabulary.ts',
    ])
  })

  it('strips prose but keeps the code', () => {
    const hook = byName('useCopilot.ts')
    expect(hook.text).toMatch(/Nothing here decides authorization/)
    expect(code(hook.text)).not.toMatch(/Nothing here decides authorization/)
    expect(code(hook.text)).toMatch(/copilotService\s*\.\s*listConversations/)
    for (const file of SOURCES) {
      expect(code(file.text).length, `${file.name} was over-stripped`).toBeGreaterThan(
        file.text.length / 5,
      )
    }
  })

  it('detects a forbidden pattern where one really exists', () => {
    // The positive control: the harness really does touch sessionStorage, so the storage rule
    // below would catch a copilot file that did the same.
    const harness = readFileSync(join(ROOT, 'src', 'test', 'harness.tsx'), 'utf8')
    expect(code(harness)).toMatch(/\bsessionStorage\b/)
  })
})

describe('no authorization decision is made in the browser', () => {
  it.each([
    ['HotelRole', /\bHotelRole\b/],
    ['a role predicate', /\b(isManager|isOwner|isViewer|hasRole|canUse\w*|canAsk\w*|allowedTools)\b/],
    ['a role literal', /['"](owner|manager|staff|viewer)['"]/],
    ['a min_role field', /\bmin_?role\b/i],
  ])('contains no %s', (_label, pattern) => {
    for (const file of SOURCES) {
      expect(code(file.text), `${file.name}`).not.toMatch(pattern)
    }
  })

  it('never renders the tool-label table as a catalogue', () => {
    // The table is a lookup for names a response reported. Iterating it would list tools the
    // caller may not be offered.
    for (const file of SOURCES) {
      const text = code(file.text)
      expect(text, file.name).not.toMatch(/Object\.(keys|values|entries)\s*\(\s*TOOL_LABELS/)
      expect(text, file.name).not.toMatch(/TOOL_LABELS\s*\)\s*\.\s*map/)
    }
  })

  it('names exactly the seven tools the backend registers', () => {
    // Read from the source, like every rule here: the table's keys as written.
    const table = /TOOL_LABELS[^=]*=\s*\{([\s\S]*?)\n\}/.exec(code(byName('vocabulary.ts').text))
    expect(table).not.toBeNull()
    const keys = [...table![1]!.matchAll(/^\s*(\w+):/gm)].map((match) => match[1])
    expect(keys.sort()).toEqual([
      'get_daily_series',
      'get_demand_forecast',
      'get_forecast_accuracy',
      'get_hotel_kpis',
      'get_hotel_priorities',
      'get_revenue_breakdown',
      'search_hotel_knowledge',
    ])
  })
})

describe('model and document text never becomes markup', () => {
  it.each([
    ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
    ['innerHTML', /\binnerHTML\b/],
    ['outerHTML', /\bouterHTML\b/],
    ['insertAdjacentHTML', /insertAdjacentHTML/],
    ['eval', /\beval\s*\(/],
    ['new Function', /new\s+Function\s*\(/],
    ['an anchor element', /<a[\s>]/],
    ['an href', /\bhref\b/],
    ['a DOMParser', /\bDOMParser\b/],
  ])('contains no %s', (_label, pattern) => {
    for (const file of SOURCES) {
      expect(code(file.text), file.name).not.toMatch(pattern)
    }
  })

  it('imports no Markdown, sanitising or linkifying library', () => {
    for (const file of SOURCES) {
      expect(code(file.text), file.name).not.toMatch(
        /from\s+['"](react-markdown|marked|markdown-it|remark[\w-]*|rehype[\w-]*|dompurify|linkify[\w-]*|showdown|micromark)['"]/,
      )
    }
    const manifest = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as {
      dependencies: Record<string, string>
    }
    // Stage 7.13 added no dependency.
    expect(Object.keys(manifest.dependencies).sort()).toEqual([
      'lucide-react',
      'react',
      'react-dom',
      'react-router-dom',
    ])
  })

  it('keeps line breaks with CSS, not by splitting text into elements', () => {
    const css = readFileSync(join(ROOT, 'src', 'features', 'copilot', 'ExchangeView.module.css'), 'utf8')
    expect(css).toMatch(/white-space:\s*pre-wrap/)
    for (const file of SOURCES) {
      expect(code(file.text), file.name).not.toMatch(/\.split\s*\(\s*['"]\\n['"]/)
    }
  })
})

describe('citations come only from the server', () => {
  it('never parses the answer text', () => {
    for (const file of SOURCES) {
      const text = code(file.text)
      expect(text, file.name).not.toMatch(/answer\s*\.\s*(match|matchAll|split|replace|replaceAll|search)\s*\(/)
      expect(text, file.name).not.toMatch(/\\\[S\\d|\[S\\d/)
    }
  })

  it('opens the document the server’s citation names', () => {
    const hook = code(byName('useCitationSource.ts').text)
    expect(hook).toMatch(/citation\.document_public_id/)
    expect(hook).toMatch(/citation\.chunk_public_id/)
    expect(hook).toMatch(/knowledgeService\s*\.\s*getDocument/)
  })
})

describe('nothing is kept in the browser', () => {
  it.each([
    ['localStorage', /\blocalStorage\b/],
    ['sessionStorage', /\bsessionStorage\b/],
    ['indexedDB', /\bindexedDB\b/],
    ['document.cookie', /document\s*\.\s*cookie/],
    ['caches', /\bcaches\s*\./],
  ])('uses no %s', (_label, pattern) => {
    for (const file of SOURCES) {
      expect(code(file.text), file.name).not.toMatch(pattern)
    }
  })
})

describe('one HTTP seam, and no streaming', () => {
  it.each([
    ['fetch', /\bfetch\s*\(/],
    ['XMLHttpRequest', /XMLHttpRequest/],
    ['EventSource', /\bEventSource\b/],
    ['WebSocket', /\bWebSocket\b/],
    ['a stream reader', /\bgetReader\s*\(|ReadableStream|text\/event-stream/],
    ['Bearer', /Bearer/],
    ['console', /\bconsole\./],
  ])('contains no %s', (_label, pattern) => {
    for (const file of SOURCES) {
      expect(code(file.text), file.name).not.toMatch(pattern)
    }
  })

  it('reaches the backend only through the shared client', () => {
    for (const name of ['copilotService.ts', 'knowledgeService.ts']) {
      expect(byName(name).text).toMatch(/from '@\/services\/api\/client'/)
    }
    for (const file of SOURCES) {
      if (/\bapi\s*\./.test(code(file.text))) {
        expect(['copilotService.ts', 'knowledgeService.ts']).toContain(file.name)
      }
    }
  })
})

describe('the request carries a question and nothing else', () => {
  const service = code(byName('copilotService.ts').text)

  it('sends exactly `{ question }` on every asking call', () => {
    const bodies = service.match(/body:\s*\{[^}]*\}/g) ?? []
    expect(bodies).toHaveLength(3)
    for (const body of bodies) {
      expect(body.replace(/\s+/g, ' ')).toBe('body: { question }')
    }
  })

  it('puts the hotel only in the path', () => {
    expect(service).toMatch(/\/hotels\/\$\{hotelPublicId\}\/copilot\/ask/)
    expect(service).toMatch(/\/hotels\/\$\{hotelPublicId\}\/copilot\/conversations/)
    const queries = service.match(/query:\s*\{[^}]*\}/g) ?? []
    expect(queries).toHaveLength(1)
    for (const query of queries) {
      expect(query).not.toMatch(/hotel/i)
    }
    for (const file of SOURCES) {
      // `hotelPublicId` is the only hotel identifier; nothing internal may appear.
      expect(code(file.text).replace(/hotelPublicId|hotel_public_id/g, ''), file.name).not.toMatch(
        /hotel_?[Ii]d\b/,
      )
    }
  })
})

describe('the server’s words never reach the screen', () => {
  it('reads no error message', () => {
    for (const file of SOURCES) {
      expect(code(file.text), file.name).not.toMatch(/\.message\b/)
    }
  })

  it('handles every copilot refusal code by name', () => {
    const vocabulary = code(byName('vocabulary.ts').text)
    for (const codeName of [
      'LLM_DISABLED',
      'LLM_BUDGET_EXHAUSTED',
      'LLM_RATE_LIMITED',
      'LLM_UNAVAILABLE',
      'LLM_INVALID_RESPONSE',
      'CONVERSATION_FULL',
    ]) {
      expect(vocabulary).toContain(`'${codeName}'`)
    }
  })
})

describe('the screen makes no claim the platform has not measured', () => {
  it.each([
    /\b(accurate|accuracy of|reliable|reliability|trustworthy|guaranteed|validated|verified)\b/i,
    /\bproduction[- ]ready\b/i,
    /\b(recommend|advise|you should)\b/i,
  ])('renders no %s', (pattern) => {
    for (const name of ['vocabulary.ts', 'CopilotPage.tsx', 'ExchangeView.tsx', 'ModeSelector.tsx']) {
      // Only string literals are user-visible copy; identifiers such as a tool name are not.
      const literals = (code(byName(name).text).match(/'[^'\n]*'|`[^`]*`|"[^"\n]*"|>[^<>{}]+</g) ?? []).join(' ')
      expect(literals, name).not.toMatch(pattern)
    }
  })

  it('labels every answer as generated, unconditionally', () => {
    const view = code(byName('ExchangeView.tsx').text)
    expect(view).toMatch(/<Badge tone="info">\{GENERATED_LABEL\}<\/Badge>/)
    expect(view).toMatch(/\{GENERATED_NOTE\}/)
  })
})
