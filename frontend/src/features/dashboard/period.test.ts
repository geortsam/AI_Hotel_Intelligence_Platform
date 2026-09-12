import { describe, expect, it } from 'vitest'

import {
  describeRange,
  PERIOD_OPTIONS,
  previousRange,
  rangeFor,
  shiftDate,
  todayInZone,
} from '@/features/dashboard/period'
import {
  bucketFor,
  computeDelta,
  currenciesIn,
  formatMoney,
  formatRating,
  formatRatioAsPercent,
  UNAVAILABLE,
} from '@/features/dashboard/format'

/**
 * The date and number rules, tested where they are decidable.
 *
 * These are pure functions, so they can be pinned exactly rather than observed through a
 * rendered component. Two of the properties below -- hotel-local "today", and inclusive
 * bounds -- are the ones that would produce a *plausible but wrong* dashboard if they broke,
 * which is precisely the kind of bug a screenshot does not catch.
 */

describe('hotel-local dates', () => {
  it('resolves today in the hotal time zone, not the browser one', () => {
    // 2026-09-10T22:30Z is already the 11th in Athens (UTC+3) and still the 10th in London.
    const instant = new Date('2026-09-10T22:30:00Z')

    expect(todayInZone('Europe/Athens', instant)).toBe('2026-09-11')
    expect(todayInZone('Europe/London', instant)).toBe('2026-09-10')
    expect(todayInZone('America/Los_Angeles', instant)).toBe('2026-09-10')
  })

  it('crosses the date line correctly', () => {
    const instant = new Date('2026-09-10T12:00:00Z')

    expect(todayInZone('Pacific/Auckland', instant)).toBe('2026-09-11')
    expect(todayInZone('Pacific/Honolulu', instant)).toBe('2026-09-10')
  })

  it('falls back to UTC for a zone the platform does not know', () => {
    const instant = new Date('2026-09-10T12:00:00Z')

    expect(todayInZone('Not/AZone', instant)).toBe('2026-09-10')
  })
})

describe('day arithmetic', () => {
  it('crosses month and year boundaries', () => {
    expect(shiftDate('2026-09-01', -1)).toBe('2026-08-31')
    expect(shiftDate('2026-01-01', -1)).toBe('2025-12-31')
    expect(shiftDate('2026-12-31', 1)).toBe('2027-01-01')
  })

  it('handles the leap day', () => {
    expect(shiftDate('2028-03-01', -1)).toBe('2028-02-29')
    expect(shiftDate('2027-03-01', -1)).toBe('2027-02-28')
  })
})

describe('period ranges', () => {
  const instant = new Date('2026-09-10T09:00:00Z')

  it('treats both bounds as inclusive, as the backend does', () => {
    // "Last 7 days" is today and the six before it -- seven dates, not eight.
    expect(rangeFor('last7', 'Europe/Athens', instant)).toEqual({
      dateFrom: '2026-09-04',
      dateTo: '2026-09-10',
    })
  })

  it('makes Today a single day, not an empty range', () => {
    expect(rangeFor('today', 'Europe/Athens', instant)).toEqual({
      dateFrom: '2026-09-10',
      dateTo: '2026-09-10',
    })
  })

  it('spans exactly the advertised number of days', () => {
    for (const option of PERIOD_OPTIONS) {
      const range = rangeFor(option.id, 'UTC', instant)
      const days =
        (Date.parse(`${range.dateTo}T00:00:00Z`) - Date.parse(`${range.dateFrom}T00:00:00Z`)) /
          86_400_000 +
        1
      expect(days).toBe(option.days)
    }
  })

  it('never offers a period the API would reject as too long', () => {
    // MAX_RANGE_DAYS is 366 on the backend.
    for (const option of PERIOD_OPTIONS) {
      expect(option.days).toBeLessThanOrEqual(366)
    }
  })

  it('anchors the range to the hotel, so two viewers see the same window', () => {
    const late = new Date('2026-09-10T23:30:00Z') // already the 11th in Athens

    expect(rangeFor('today', 'Europe/Athens', late).dateTo).toBe('2026-09-11')
    expect(rangeFor('today', 'Europe/London', late).dateTo).toBe('2026-09-11')
    // London is at UTC+1 in September, so 23:30Z is 00:30 on the 11th there too. The point
    // is that the zone -- not the browser -- decides, which the Los Angeles case shows.
    expect(rangeFor('today', 'America/Los_Angeles', late).dateTo).toBe('2026-09-10')
  })
})

describe('the comparison window', () => {
  it('is adjacent, equal in length, and does not overlap', () => {
    const range = { dateFrom: '2026-09-04', dateTo: '2026-09-10' }

    expect(previousRange(range, 7)).toEqual({ dateFrom: '2026-08-28', dateTo: '2026-09-03' })
  })

  it('ends the day before the current window starts', () => {
    const range = rangeFor('last30', 'UTC', new Date('2026-09-10T09:00:00Z'))
    const previous = previousRange(range, 30)

    expect(shiftDate(previous.dateTo, 1)).toBe(range.dateFrom)
  })

  it('compares a single day against the day before it', () => {
    expect(previousRange({ dateFrom: '2026-09-10', dateTo: '2026-09-10' }, 1)).toEqual({
      dateFrom: '2026-09-09',
      dateTo: '2026-09-09',
    })
  })
})

describe('range labels', () => {
  /* `en-GB` abbreviates September as "Sept", not "Sep" -- that is the locale's own
   * abbreviation, so the expectations follow the platform rather than the other way round.
   * A month with an unambiguous short form keeps the assertions honest about the shape. */

  it('collapses a single-day range to one date', () => {
    expect(describeRange({ dateFrom: '2026-10-10', dateTo: '2026-10-10' })).toBe('10 Oct 2026')
  })

  it('reads as a span otherwise', () => {
    expect(describeRange({ dateFrom: '2026-10-04', dateTo: '2026-10-10' })).toBe(
      '4 Oct – 10 Oct 2026',
    )
  })

  it('shows the year on both ends when a range crosses one', () => {
    expect(describeRange({ dateFrom: '2025-12-28', dateTo: '2026-01-03' })).toBe(
      '28 Dec 2025 – 3 Jan 2026',
    )
  })
})

describe('formatting backend decimals', () => {
  it('renders a ratio as a percentage', () => {
    // occupancy_rate is NUMERIC(5,4): "0.5804" is 58.04%.
    expect(formatRatioAsPercent('0.5804')).toBe('58.0%')
    expect(formatRatioAsPercent('0.0000')).toBe('0.0%')
    expect(formatRatioAsPercent('1.0000')).toBe('100.0%')
  })

  it('shows a dash, never a zero, for a metric the backend called undefined', () => {
    expect(formatRatioAsPercent(null)).toBe(UNAVAILABLE)
    expect(formatRating(null)).toBe(UNAVAILABLE)
  })

  it('names the currency rather than using a symbol', () => {
    // Two hotels can report in currencies whose symbol is the same glyph. The code cannot
    // be misread; "$1,240" beside "$980" can.
    expect(formatMoney('9750.00', 'EUR')).toContain('EUR')
    expect(formatMoney('9750.00', 'EUR')).toContain('9,750.00')
    expect(formatMoney('150.00', 'JPY')).toContain('JPY')
  })

  it('preserves the exact figure the backend sent', () => {
    // The string arrives as NUMERIC(14,2). Nothing is rounded or recomputed on the way in.
    expect(formatMoney('87.05', 'EUR')).toContain('87.05')
    expect(formatMoney('0.01', 'EUR')).toContain('0.01')
    expect(formatMoney('999999999999.99', 'EUR')).toContain('999,999,999,999.99')
  })

  it('restores a normalized rating to the five-point scale', () => {
    expect(formatRating('0.8750')).toBe('4.4 / 5')
  })
})

describe('currency buckets', () => {
  const buckets = [
    { currency: 'EUR', amount: '100.00' },
    { currency: 'USD', amount: '200.00' },
  ]

  it('finds a currency that is present', () => {
    expect(bucketFor(buckets, 'USD')?.amount).toBe('200.00')
  })

  it('returns null -- not a zero bucket -- for one that is absent', () => {
    // An absent bucket means "undefined", not "nothing was earned". Synthesising
    // { amount: "0.00" } here is the single change that would make the UI start lying.
    expect(bucketFor(buckets, 'GBP')).toBeNull()
    expect(bucketFor([], 'EUR')).toBeNull()
  })

  it('lists currencies in the order the backend returned them, without duplicates', () => {
    const roomRevenue = [
      { currency: 'EUR', room_revenue: '1.00', adr: null, revpar: null },
    ]
    // Two differently-shaped bucket lists, which is the case this exists for: room revenue
    // carries rates, ledger money does not.
    expect(currenciesIn(buckets, roomRevenue)).toEqual(['EUR', 'USD'])
  })
})

describe('period-over-period change', () => {
  it('is a proportion of the previous value', () => {
    expect(computeDelta('110.00', '100.00')?.change).toBeCloseTo(0.1, 10)
    expect(computeDelta('110.00', '100.00')?.direction).toBe('up')
    expect(computeDelta('90.00', '100.00')?.direction).toBe('down')
  })

  it('refuses to compare against an undefined metric', () => {
    // An undefined ADR is not zero, so there is no change to state.
    expect(computeDelta('150.00', null)).toBeNull()
    expect(computeDelta(null, '150.00')).toBeNull()
  })

  it('refuses to divide by zero rather than reporting an infinite rise', () => {
    expect(computeDelta('150.00', '0.00')).toBeNull()
  })

  it('reports an imperceptible change as flat rather than as a direction', () => {
    expect(computeDelta('100.01', '100.00')?.direction).toBe('flat')
  })
})
