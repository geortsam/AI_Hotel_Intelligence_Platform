# UI redesign — "Midnight concierge"

> **Status: done (2026-11-20 to 2026-11-22).** UI-0 to UI-10, plus your two decisions (fonts, the
> Forecasting title) and a reproducibility fix. Frontend only: no API, schema, authentication or
> backend behaviour changed, and no fingerprinted file was touched, so no retrieval re-pin was
> needed. Each stage below links its commits; the CI runs are in
> [roadmap-v2.0-execution.md](roadmap-v2.0-execution.md) §4. Current screenshots are in
> [`screenshots/`](screenshots/), embedded in the [README](../README.md#screenshots).

## 1. Direction

A dark-first operations console: a deep navy ground, raised surfaces a step lighter, one blue for
action, and brass used sparingly as the brand accent (the mark and the active navigation rail).
Dense tables and figures stay at 13–14px; the serif display face is reserved for page titles and
headline figures. Motion is short and quiet, and stops entirely under reduced motion.

## 2. Tokens — `frontend/src/styles/tokens.css`

Every colour, size and duration is a custom property. That file is the only place a raw colour or
length may appear. Contrast against the background, the surface and the raised surface is recorded
beside each text colour, as measured in the browser.

| Group | Values |
|---|---|
| Surfaces | `--color-bg` `#0b0f17`, `--color-surface` `#111723`, `--color-surface-raised` `#172031`; borders `#1f2a3c` and strong `#63738b` (the plan's `#5a6a82` failed 3:1 on the raised surface) |
| Text | `--color-text` `#e8ecf1` (16.2:1 on bg), `--color-text-muted` `#9aa7b8` (7.9:1) |
| Action and brand | `--color-primary` `#3b63f3`, `--color-accent` `#c8a24a` (brass), `--color-focus` `#7aa5ff` |
| Charts | `--chart-1`…`6` (blue, teal, brass, violet, coral, slate); `--chart-grid`, `--chart-axis`; `--chart-forecast` and `--chart-band` (primary at 18%) for forecasts and their interval |
| Type scale | 12 / 13 / 14 / 16 / 20 / 24 / 30 / 36px (`--text-xs` … `--text-4xl`) |
| Shape | radius 4 / 8 / 12px |
| Motion | durations 80 / 120 / 180 / 280 / 400ms; easings standard, enter, exit |
| Layout | sidebar 248px, header 56px, content up to 1440px |

## 3. Typography and fonts

- **Inter** (variable weight) is the interface face. **Source Serif 4** (variable weight and optical size) is used only through `--font-display`: page titles (`h1`), KPI and summary figures, and the sign-in brand.
- Both are self-hosted: `frontend/src/styles/fonts.css` declares them, and `frontend/src/assets/fonts/` holds the files. The CSP allows only `font-src 'self'`, so a font CDN was never an option.
  - The files are woff2 subsets (latin, latin-ext, greek), each with its `unicode-range`. An English page fetches only the two latin files, about 170 KB.
  - Vite hashes them into `/assets/`, which nginx serves `immutable` and does not gzip.
  - `font-display: swap`.
  - SIL OFL 1.1 licences sit beside the files.
  - Source: `@fontsource-variable/inter@5.3.0` and `@fontsource-variable/source-serif-4@5.3.1`.
- Figures use tabular numerals: `td`, `dd` and `.tnum` in `base.css`. In every table, numeric columns are right-aligned, headers included. A specificity bug had left every numeric cell computing `left` until UI-5.

## 4. Shared component architecture

Shared anatomy lives in `frontend/src/components/ui/`, and feature modules take it with CSS Modules'
`composes`. These refactors were verified by SHA-256 digests of every element's computed style
under `<main>`, before and after. The digests were identical.

| Module | Holds | Users |
|---|---|---|
| `Form.module.css` | form column, field grid, label, control, hint, error, actions | 8 form modules |
| `DataTable.module.css` | scroll box, table and header rules, cell vocabulary, the card layout | 12 list modules |
| `VisuallyHidden.module.css` | `srOnly` | 12 modules |
| `Rail.module.css` | `--rail` per Badge tone, for status rails | rooms (table and cards), insights |
| `Button` | variants and sizes; `loading` (busy, disabled, width held) | everywhere |

**Rule: one `composes` path per shared module.** If a module reaches a shared module by two
routes, Vite may emit that rule at a different place from build to build, and the bundle stops
being reproducible. That happened from `6ac7d44` until `52fde7b` (§6).

## 5. Responsive behaviour and accessibility

- **Breakpoints:** 600 / 768 / 1024 / 1280, documented in `tokens.css`.
  - Below 600px: phone layouts.
  - Below 1024px: every list renders as cards (`useIsCompact`, default `max-width: 1023px`), and the sidebar is a drawer.
  - From 1280px: KPI rows four across, and the sticky side panels on booking detail and availability.
  - Details: [frontend/README.md](../frontend/README.md#styling).
- **No sideways scrolling:**
  - The shell's content column is `minmax(0, 1fr)`, so the header's min-content can't widen the page.
  - `<main>` uses `overflow-x: clip` rather than `hidden`, so sticky panels work.
  - UI-10's audit, re-run after the fonts: 17 routes × 375, 768 and 1440px, with no sideways scroll, no escaping element and exactly one `h1` per page.
- **Semantics:**
  - One `h1` per page, and the header title is no longer a heading.
  - Tables carry visually hidden captions.
  - Native selects and radios keep the platform's keyboard behaviour. Selects have one chevron, and forced-colours mode restores the system arrow.
  - Rails and chips only repeat a status already written as text.
- **Automated accessibility checks (A4):** axe-core 4.14.0 runs in the Vitest suite on every route and state: tables and cards, open forms, loading, empty and failure states, and the shell with its landmarks. The helper is `frontend/src/test/axe.ts`, and colour contrast is excluded because jsdom renders nothing. The first run found and fixed genuine defects:
  - skipped heading levels on eight pages;
  - KPI term/value pairs broken on the dashboard and analytics;
  - a prohibited `aria-label` that hid the review rating from some screen readers.
  - Details are in [project-completion-audit.md](project-completion-audit.md) §4.3.
- **Reduced motion:** a global rule in `base.css` cuts animation and transition durations to 0.01ms and disables view transitions. The animations (KPI rise, chart draw, page cross-fade, card fade) use opacity and transform only, so nothing reflows.

## 6. Stages

| Stage | Outcome | Commits |
|---|---|---|
| UI-0 | Read-only baseline: 57 screenshots at 375 / 768 / 1440px and an issue list (kept outside the repository) | — |
| UI-1 | Token foundation; no markup changes | `f7f8c41` |
| UI-2 | Shell: one `h1` per page, translucent header, brass brand mark (8.0:1) and active rail, page cross-fade | `1e60aa1`, `80270a5` |
| UI-3 | Shared form anatomy, composed by eight form modules; digests identical | `d60924b` … `20a922c` |
| UI-4 | One visually-hidden rule; `Button loading` | `52ed3a1`, `3b31953` |
| UI-5 | Cards below 1024px; the DataTable module; numbers right-aligned; the period selector as a 2 × 2 grid on phones; the shell column fix (Administration had been 380px at 375px); one select chevron | `c6f3670` … `ff3eaee` |
| UI-6 | KPI grid in 1 / 2 / 4 columns; eyebrow labels, display figures, delta chips; page titles 24px; trend chart with tokens, area and draw-in; KPI entrance | `1e1b2e3` … `868205f` |
| UI-7 | Forecast chart: the interval in the forecast's own colour; tab underlines on the rule; insight severity rails; copilot as an 820px reading column | `25acc8c` … `9642a65` |
| UI-8 | `<main>` clipped instead of hidden; booking detail with a sticky Operations panel; availability's form beside its answer; room status rails; guest and room records as 880px columns | `e337e46` … `b61fd59` |
| UI-9 | Split-screen sign-in with the brass mark; a not-found state with a mark and a way onward; review figures in the display face | `1175aa3`, `9704e68`, `b1d1b0d` |
| UI-10 | Final audit (51 checks) and report | — |
| Decisions | Self-hosted Inter and Source Serif 4; the `/intelligence` page titled "Forecasting" | `bf8bcef`, `a08bb95` |
| Fix | The bundle made reproducible again: DataTable's caption states the clip rule itself instead of composing `srOnly`, so no module reaches `VisuallyHidden` by two paths. Local builds went from 4 distinct outputs in 4 to 1 in 6, and CI's image-reproducibility job passed | `52fde7b` |

**Deliberately not done:**
- **A shared HotelGate / StateMessage component:** per-page copy is asserted by tests.
- **Financial totals as tiles:** a six-column comparison reads better as a table.
- **A sliding tab indicator:** it would need JavaScript measurement; the underline eases instead.
- **A settings-page section nav:** two or three sections per page.

The tasks that remain are counted in [project-completion-audit.md](project-completion-audit.md). The
open accessibility-automation work is A4, and the responsive-audit regression test is A9.
