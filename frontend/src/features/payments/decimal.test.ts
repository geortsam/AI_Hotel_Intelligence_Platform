import { describe, expect, it } from 'vitest'

import {
  compareDecimalStrings,
  isPositiveMoneyString,
  toDecimalString,
} from '@/lib/decimal'
import { movedNoMoney, POSTABLE_STATUSES, statusPresentation } from '@/features/payments/vocabulary'
import type { PaymentStatus } from '@/types/payment'

/**
 * The money-string rules, and the payment vocabulary, pinned.
 *
 * These are pure and therefore decidable exactly, which matters more here than elsewhere: a
 * rounding slip in a component shows up as a wrong pixel, and a rounding slip in a money
 * string shows up as a wrong charge.
 */

describe('validating a money figure', () => {
  it('accepts what the column can hold', () => {
    for (const value of ['1', '1.5', '1.50', '0.01', '120.00', '999999999999.99']) {
      expect(isPositiveMoneyString(value)).toBe(true)
    }
  })

  it('rejects zero, which the backend refuses', () => {
    // ck_payments_amount_positive: amount > 0 for BOTH kinds. Verified live -- "0.00" is a 422.
    for (const value of ['0', '0.0', '0.00']) {
      expect(isPositiveMoneyString(value)).toBe(false)
    }
  })

  it('rejects a negative amount rather than sending it to be refused', () => {
    // Direction lives in `kind`, never in the sign, so a minus is meaningless here.
    for (const value of ['-1', '-0.01', '-120.00']) {
      expect(isPositiveMoneyString(value)).toBe(false)
    }
  })

  it('rejects more precision than NUMERIC(14,2) keeps', () => {
    // Silently rounding 1.555 to 1.56 would post a figure the operator did not type.
    expect(isPositiveMoneyString('1.555')).toBe(false)
    expect(isPositiveMoneyString('0.001')).toBe(false)
  })

  it('rejects shapes that are not a number at all', () => {
    for (const value of ['', ' ', '.', '.5', '1.', 'abc', '1e3', '1,50', '+1', '1 2']) {
      expect(isPositiveMoneyString(value)).toBe(false)
    }
  })

  it('rejects more integer digits than the column has', () => {
    expect(isPositiveMoneyString('999999999999.99')).toBe(true) // 12 digits
    expect(isPositiveMoneyString('1000000000000.00')).toBe(false) // 13
  })
})

describe('preparing the figure for the wire', () => {
  it('trims surrounding whitespace and changes nothing else', () => {
    expect(toDecimalString('  120.00 ')).toBe('120.00')
  })

  it('does not pad, round or re-scale', () => {
    // The server stores at the column's scale. A client that rewrote `5` as `5.00` would be
    // normalising a figure it does not own.
    expect(toDecimalString('5')).toBe('5')
    expect(toDecimalString('5.5')).toBe('5.5')
    expect(toDecimalString('0.10')).toBe('0.10')
  })
})

describe('comparing two money strings', () => {
  it('orders by value, not by text length', () => {
    expect(compareDecimalStrings('9', '10')).toBeLessThan(0)
    expect(compareDecimalStrings('100', '99.99')).toBeGreaterThan(0)
  })

  it('treats different scales of the same value as equal', () => {
    expect(compareDecimalStrings('5', '5.00')).toBe(0)
    expect(compareDecimalStrings('5.5', '5.50')).toBe(0)
  })

  it('compares the fraction correctly', () => {
    expect(compareDecimalStrings('1.09', '1.1')).toBeLessThan(0)
    expect(compareDecimalStrings('0.30', '0.3')).toBe(0)
  })

  it('survives the case that motivates the whole module', () => {
    // 0.1 + 0.2 !== 0.3 in binary floating point. Nothing here adds, and nothing converts.
    expect(compareDecimalStrings('0.30', '0.30')).toBe(0)
    expect(compareDecimalStrings('0.1', '0.2')).toBeLessThan(0)
  })

  it('ignores leading zeros', () => {
    expect(compareDecimalStrings('007', '7')).toBe(0)
  })
})

describe('the payment vocabulary', () => {
  const ALL: readonly PaymentStatus[] = [
    'pending',
    'authorized',
    'captured',
    'failed',
    'refunded',
    'partially_refunded',
    'cancelled',
  ]

  it('names every status the backend can send', () => {
    for (const status of ALL) {
      expect(statusPresentation(status).label).not.toBe('')
      expect(statusPresentation(status).description).toContain('—')
    }
  })

  it('treats exactly failed and cancelled as having moved no money', () => {
    // VOIDED_PAYMENT_STATUSES on the backend. Pinned so a change there fails a test here
    // rather than silently offering a refund the server will refuse.
    expect(ALL.filter(movedNoMoney)).toEqual(['failed', 'cancelled'])
  })

  it('offers only statuses a person can actually witness', () => {
    // `refunded` and `partially_refunded` describe what other postings did to this one, and
    // `authorized` is a gateway's word about a hold this platform does not place.
    expect(POSTABLE_STATUSES).toEqual(['pending', 'captured', 'failed', 'cancelled'])
    for (const status of POSTABLE_STATUSES) {
      expect(ALL).toContain(status)
    }
  })
})
