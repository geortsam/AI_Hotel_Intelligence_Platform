import { describe, expect, it } from 'vitest'

import {
  canExtendStay,
  canModifyStay,
  isDestructive,
  isTerminal,
  MAX_EXTENSION_NIGHTS,
  nextStatuses,
} from '@/features/bookings/transitions'
import type { BookingStatus } from '@/types/booking'

/**
 * The lifecycle graph, pinned.
 *
 * This file exists to make drift **visible**. The frontend holds a copy of the backend's
 * transition table so it can offer only reachable actions, and a copy of a domain rule is a
 * thing that goes quietly out of date. Stating the whole table here means a backend change
 * this copy has not followed fails a test, instead of silently producing a button whose only
 * outcome is a 409.
 *
 * The expected values were read out of the **running** backend on 2026-09-10, from
 * `app.models.enums.BOOKING_STATUS_TRANSITIONS`, `MODIFIABLE_BOOKING_STATUSES`,
 * `EXTENDABLE_BOOKING_STATUSES` and `app.services.booking.MAX_EXTENSION_NIGHTS` -- not from
 * the stage brief, and not from memory.
 */

const ALL: readonly BookingStatus[] = [
  'pending',
  'confirmed',
  'checked_in',
  'checked_out',
  'cancelled',
  'no_show',
]

describe('the transition table', () => {
  it('matches the backend, state for state', () => {
    expect(nextStatuses('pending')).toEqual(['confirmed', 'cancelled'])
    expect(nextStatuses('confirmed')).toEqual(['checked_in', 'cancelled', 'no_show'])
    expect(nextStatuses('checked_in')).toEqual(['checked_out'])
    expect(nextStatuses('checked_out')).toEqual([])
    expect(nextStatuses('cancelled')).toEqual([])
    expect(nextStatuses('no_show')).toEqual([])
  })

  it('never offers a state its own self-transition', () => {
    // The backend permits `X -> X` as an idempotent no-op, which makes a retried request
    // safe. It is not an action, so it must not be a button.
    for (const status of ALL) {
      expect(nextStatuses(status)).not.toContain(status)
    }
  })

  it('refuses confirmed -> checked_out', () => {
    // A guest who checked out first checked in. Skipping the arrival would put a stay in the
    // occupancy figures the property never recorded anyone arriving for.
    expect(nextStatuses('confirmed')).not.toContain('checked_out')
  })

  it('refuses pending -> no_show', () => {
    // A no-show held a reservation and did not arrive; a pending booking holds no room and
    // was never promised anything. Its honest disposal is `cancelled`.
    expect(nextStatuses('pending')).not.toContain('no_show')
  })

  it('offers only real statuses', () => {
    for (const status of ALL) {
      for (const target of nextStatuses(status)) {
        expect(ALL).toContain(target)
      }
    }
  })
})

describe('terminal states', () => {
  it('are exactly the three the backend derives', () => {
    expect(ALL.filter(isTerminal)).toEqual(['checked_out', 'cancelled', 'no_show'])
  })

  it('are derived from the table rather than listed separately', () => {
    // A state is terminal exactly when it has nowhere to go, so the two cannot disagree.
    for (const status of ALL) {
      expect(isTerminal(status)).toBe(nextStatuses(status).length === 0)
    }
  })
})

describe('destructive targets', () => {
  it('are the disposals, and both are terminal', () => {
    expect(isDestructive('cancelled')).toBe(true)
    expect(isDestructive('no_show')).toBe(true)
    expect(isTerminal('cancelled')).toBe(true)
    expect(isTerminal('no_show')).toBe(true)
  })

  it('do not include ordinary progress through the lifecycle', () => {
    expect(isDestructive('confirmed')).toBe(false)
    expect(isDestructive('checked_in')).toBe(false)
    // checked_out is terminal but is not destructive: it is how a stay is meant to end.
    expect(isDestructive('checked_out')).toBe(false)
  })
})

describe('which stay operation applies', () => {
  it('permits a whole-stay change only for pending and confirmed', () => {
    expect(ALL.filter(canModifyStay)).toEqual(['pending', 'confirmed'])
  })

  it('permits an extension only for checked_in', () => {
    expect(ALL.filter(canExtendStay)).toEqual(['checked_in'])
  })

  it('never permits both for the same status', () => {
    // The two sets are disjoint by design: one restates a stay nobody is living in, the
    // other lengthens one in progress. A status offering both would mean the UI could send
    // the operation the backend refuses.
    for (const status of ALL) {
      expect(canModifyStay(status) && canExtendStay(status)).toBe(false)
    }
  })

  it('offers neither for a terminal booking', () => {
    for (const status of ALL.filter(isTerminal)) {
      expect(canModifyStay(status)).toBe(false)
      expect(canExtendStay(status)).toBe(false)
    }
  })
})

describe('the extension limit', () => {
  it('matches the backend constant', () => {
    expect(MAX_EXTENSION_NIGHTS).toBe(366)
  })
})
