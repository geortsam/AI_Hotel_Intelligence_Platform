# Frontend

React 18 + TypeScript 5.7 + Vite 6. The application foundation: routing, centralised
configuration and the API client. Feature areas arrive in later stages.

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
                SectionHeader, Skeleton
  config/       the only module that reads import.meta.env
  pages/        route targets
  router/       route table, path constants, navigation structure
  services/     API client (transport only, no endpoints yet)
  styles/       tokens.css (design tokens), base.css (element defaults)
  test/         vitest setup
  types/        shared API envelope types, mirroring the backend
```

### Styling

`src/styles/tokens.css` holds every colour, size, weight and duration as a CSS custom
property, and is the only place a raw colour or pixel value may appear. Component styles
live in a `.module.css` beside the component that uses it, so a style can be found from the
markup that renders it and deleted with it.

Breakpoints are `< 768px` (mobile), `>= 768px` (tablet) and `>= 1024px` (desktop, where the
sidebar becomes permanent). They cannot be custom properties -- a media query cannot read
`var()` -- so they are recorded at the foot of `tokens.css` and quoted consistently.

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

It defines no endpoints. Domain calls belong with the features that need them.
