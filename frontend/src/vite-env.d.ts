/// <reference types="vite/client" />

/**
 * The environment variables this frontend reads, added to the ones Vite declares.
 *
 * Declaration merging, not replacement: an `interface ImportMeta` of our own would shadow
 * Vite's and take `PROD`, `DEV` and `MODE` with it. Only the project's own variables are
 * declared here, and `config/env.ts` is the only module allowed to read them.
 *
 * Optional because a variable that is not set is genuinely absent at runtime -- `.env.example`
 * ships it empty, and an unset one is `undefined`. Typing it as always-present would push a
 * lie past the compiler.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
}
