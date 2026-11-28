# Frontend

React 18 + TypeScript 5.7 + Vite 6. The complete dashboard: authentication, protected routing,
centralised configuration and one API client, plus feature areas for bookings and booking
mutations, payments, financials, reviews, guests, rooms, availability, hotel and room-type
management, membership administration, platform administration and intelligence.

Routes are code-split — 13 lazy routes behind a `Suspense` boundary — so the initial payload
carries the shell and nothing else. 998 tests run under Vitest.

## Install

```bash
cd frontend
npm install
```

Node 20+ is required; verified on Node 24.21.0 with npm 11.19.0.

npm 11 prints a warning that `esbuild`'s postinstall script was not run. It is safe here —
esbuild ships its platform binary as an optional dependency, and both the dev server and
the production build were verified working without approving the script.

## Configure

Copy the template and edit it:

```bash
cp .env.example .env.local
```

`VITE_API_BASE_URL` is the backend origin, e.g. `https://api.example.com`. Give it no
trailing slash and no `/api/v1` suffix — the API client adds the prefix itself.

**Leave it empty for local development.** Requests then go to the dev server's own origin
and the proxy in `vite.config.ts` forwards `/api` to the backend (default
`http://localhost:8000`, override with `VITE_API_PROXY_TARGET`), so the browser stays
same-origin and no CORS preflight is involved.

Only `VITE_`-prefixed variables reach the browser bundle, and everything that reaches it is
readable in devtools. Never put a secret in a `VITE_` variable.

## Commands

```bash
npm run dev        # dev server on http://localhost:5173
npm run typecheck  # both TypeScript projects, no emit
npm test           # vitest, single run
npm run test:watch # vitest, watching
npm run build      # typecheck, then production build to dist/
npm run preview    # serve the built dist/ locally
```

`build` runs `typecheck` first, so a type error fails the build rather than shipping.

## Layout

```
src/
  components/
    layout/     the application shell: AppShell, Sidebar, Header
    ui/         reusable primitives: Button, Card, Badge, PageContainer,
                SectionHeader, Skeleton; shared style modules (Form, DataTable,
                Rail, VisuallyHidden)
  assets/fonts/ self-hosted Inter and Source Serif 4 (woff2) and their OFL licences
  config/       the only module that reads import.meta.env
  features/     one folder per area (bookings, finance, intelligence, ...): its
                components, hooks, styles and tests
  lib/          shared helpers: formatting, decimal arithmetic, useIsCompact
  pages/        route targets
  router/       route table, path constants, navigation structure
  services/     api/ is the transport (client, errors, preconditions); one folder per
                API area holds that area's endpoint calls
  session/      authentication and the selected hotel (AuthProvider, HotelProvider)
  styles/       fonts.css (@font-face), tokens.css (design tokens), base.css (element
                defaults)
  test/         vitest setup
  types/        shared API envelope types, mirroring the backend
```

### Styling

`src/styles/tokens.css` holds every colour, size, weight and duration as a CSS custom
property, and is the only place a raw colour or pixel value may appear. Component styles
live in a `.module.css` beside the component that uses it, so a style can be found from the
markup that renders it and deleted with it.

**Type.** Two self-hosted faces, in `src/styles/fonts.css` (imported first by `index.css`):
Inter, the interface face, and Source Serif 4, used only through `--font-display` for page
titles and headline figures. The woff2 files are in `src/assets/fonts/` -- latin, latin-ext and
greek subsets, each with the `unicode-range` that keeps a page to the scripts it renders -- beside
their SIL Open Font License files (`LICENSE-Inter.txt`, `LICENSE-SourceSerif4.txt`). Vite hashes
them into `/assets/`, which satisfies the server's `font-src 'self'`; no font CDN is used, and
the system stack shows until a face has loaded (`font-display: swap`).

**Shared style modules.** `src/components/ui/` holds the anatomy that many features share, and a
feature's module takes it with CSS Modules' `composes` rather than restating it:

| Module | What it holds | Used by |
|---|---|---|
| `Form.module.css` | the field anatomy: form column, grid, label, control, hint, error, actions | composed by 8 form modules |
| `DataTable.module.css` | the scroll box, table and header rules, cell vocabulary (link, code, timestamp, numeric, absent) and the card layout | composed by 12 list modules |
| `VisuallyHidden.module.css` | `srOnly`, text for assistive technology only | composed by 12 modules |
| `Rail.module.css` | a `--rail` colour per Badge tone; the consumer draws the edge | imported by 3 components |

**One `composes` path per shared module.** A module must not reach the same shared module by two
routes -- for example composing `srOnly` directly *and* composing a class that itself composes
`srOnly`. Vite then places that rule at a different point in a chunk from build to build, and the
bundle stops being reproducible: CI's image-reproducibility job caught exactly this (fixed in
`52fde7b`). DataTable's `caption` therefore states the clip rule itself.

**Breakpoints.** A media query cannot read `var()`, so breakpoints are numbers, documented at the
foot of `tokens.css` as 600 / 768 / 1024 / 1280 and used as:

| Width | What changes |
|---|---|
| below 600px | phone layouts: single-column grids, the period selector as a 2 x 2 grid, KPI tiles one per row |
| below 768px | tighter page padding (`PageContainer`), and three pages' own compact layouts |
| 1024px and up | the sidebar is permanent; list pages show tables instead of cards (`useIsCompact`, default `max-width: 1023px`) |
| 1280px and up | KPI rows four across; booking detail and availability gain a sticky side panel |

Below 1024px every list renders as cards, so no table has to survive a phone or a tablet. A few
older modules still use one-off widths (480, 640, 899 and 1100px, among others); moving them onto
the documented set is open work, not a second scale.

Import with the `@/` alias (`@/config/env`), which maps to `src/`. It is declared in both
`vite.config.ts` (for the bundler) and `tsconfig.json` (for the compiler); changing one
without the other breaks resolution in the tool that was missed.

### Two TypeScript projects

`tsconfig.json` covers `src/` with DOM types and no Node types, so browser code cannot
reach `process` or `node:*`. `tsconfig.node.json` covers `vite.config.ts` with Node types
and no DOM. They are separate projects rather than one config with both type sets, which
is what keeps that boundary real; `npm run typecheck` checks both.

### API client

`src/services/api/client.ts` is the only place the app issues HTTP. It owns the base URL,
the `/api/v1` prefix, JSON encoding, and the conversion of any non-2xx response into an
`ApiError` carrying the backend's own `code` and `message`. A failed request always throws
`ApiError`; `status` is `0` when no response arrived at all.

The session is two `HttpOnly` cookies the browser attaches itself (`credentials: 'include'`), so
no script -- this client included -- ever holds a credential. What the client holds is the
session's CSRF token, sent as `X-CSRF-Token` on every request that changes something; a 401 is
answered by one refresh (`POST /auth/session/refresh`) before the session is treated as ended.

The client defines no endpoints. Domain calls live in `src/services/<area>/`, one module per API
area, and the features call those.
