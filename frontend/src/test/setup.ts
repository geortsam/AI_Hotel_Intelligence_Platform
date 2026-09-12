import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

/**
 * Unmount between tests.
 *
 * Without this, components from one test stay in the document and the next test's queries
 * can match them -- which shows up as a test that passes alone and fails in the suite, or
 * worse, one that passes for the wrong reason.
 */
afterEach(() => {
  cleanup()
})
