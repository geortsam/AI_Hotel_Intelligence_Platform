/**
 * The one place the application reads its environment.
 *
 * Every `import.meta.env` access lives here. Nothing else in `src/` touches it, so the set
 * of variables the frontend depends on is this file's exports and cannot drift into a
 * component nobody remembers to grep for.
 *
 * **Only `VITE_`-prefixed variables exist in the bundle at all.** Vite substitutes them at
 * build time, which means every value here is baked into the shipped JavaScript and is
 * readable by anyone who opens the browser devtools. Nothing secret may ever be read here:
 * no API keys, no tokens, no credentials. A public base URL is the appropriate kind of
 * value for this file, and it is currently the only one.
 */

/** Backend origin, e.g. `https://api.example.com`. Empty means "same origin as this page". */
const rawBaseUrl: string = import.meta.env.VITE_API_BASE_URL ?? ''

/**
 * The backend's API mount point, taken from the backend's own `api_v1_prefix` setting.
 *
 * Hard-coded rather than configurable because it is a property of the API this frontend is
 * written against, not of the deployment: a build that talked to a different prefix would
 * be talking to a different API.
 */
export const API_PREFIX = '/api/v1'

/**
 * Where API requests are sent, with any trailing slash removed.
 *
 * An empty value is deliberate and is the development default: requests go to the page's
 * own origin and Vite's dev-server proxy forwards `/api` to the backend, so the browser
 * makes same-origin requests and no CORS preflight is involved. A deployed build sets
 * `VITE_API_BASE_URL` to the backend origin.
 */
export const API_BASE_URL: string = rawBaseUrl.replace(/\/+$/, '')

/** True in `vite build` output, false under `vite dev`. */
export const IS_PRODUCTION: boolean = import.meta.env.PROD
