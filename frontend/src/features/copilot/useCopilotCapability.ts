import { useEffect, useState } from 'react'

import { copilotService } from '@/services/copilot/copilotService'

/**
 * Whether this deployment has the copilot switched on, as the server reports it.
 *
 * ## The server's answer, never a guess
 *
 * `copilot_enabled` is read from `GET /api/v1/`, which reports the server's own `llm_enabled`
 * switch. Nothing here infers it — not from an environment variable, not from a build flag,
 * not from an earlier refusal. Until the answer arrives the state is `checking`, and no
 * question is accepted: a question sent in that gap would be charged against the allowance
 * only to be refused.
 *
 * ## An unreadable answer is `unknown`, not `disabled`
 *
 * A network failure or a malformed body says nothing about the switch. Reporting it as
 * "switched off" would fake a state the server never stated. `unknown` keeps the question box
 * offered; the server still refuses a question with `LLM_DISABLED` if the copilot is off, and
 * the screen already handles that refusal.
 *
 * Read once per mount. The switch is deployment configuration: it does not change with the
 * hotel, and a change to it needs a restart of the server anyway.
 */

export type CopilotCapability = 'checking' | 'enabled' | 'disabled' | 'unknown'

function isCapability(body: unknown): body is { copilot_enabled: boolean } {
  return (
    typeof body === 'object' &&
    body !== null &&
    typeof (body as { copilot_enabled?: unknown }).copilot_enabled === 'boolean'
  )
}

export function useCopilotCapability(): CopilotCapability {
  const [capability, setCapability] = useState<CopilotCapability>('checking')

  useEffect(() => {
    const request = new AbortController()
    let cancelled = false

    copilotService
      .capability(request.signal)
      .then((body: unknown) => {
        if (cancelled) {
          return
        }
        if (!isCapability(body)) {
          setCapability('unknown')
          return
        }
        setCapability(body.copilot_enabled ? 'enabled' : 'disabled')
      })
      .catch(() => {
        if (cancelled || request.signal.aborted) {
          return
        }
        setCapability('unknown')
      })

    return () => {
      cancelled = true
      request.abort()
    }
  }, [])

  return capability
}
