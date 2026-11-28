import { describe, expect, it } from 'vitest'

import { groupDecimalDigits } from '@/lib/decimal'

describe('groupDecimalDigits', () => {
  it('groups the whole part in thousands and keeps the fraction exactly', () => {
    expect(groupDecimalDigits('20300.62')).toBe('20,300.62')
    expect(groupDecimalDigits('1234567.00')).toBe('1,234,567.00')
  })

  it('leaves a figure under a thousand as it is', () => {
    expect(groupDecimalDigits('999.99')).toBe('999.99')
    expect(groupDecimalDigits('0.00')).toBe('0.00')
    expect(groupDecimalDigits('7')).toBe('7')
  })

  it('keeps the sign of a correcting line', () => {
    expect(groupDecimalDigits('-1441.96')).toBe('-1,441.96')
  })

  it('never rounds or pads: every decimal place the server sent survives', () => {
    expect(groupDecimalDigits('12345.6789')).toBe('12,345.6789')
    expect(groupDecimalDigits('1000.5')).toBe('1,000.5')
  })

  it('keeps digits a float would lose', () => {
    // 2^53 + 1 is not representable as a double; a Number() round trip would change it.
    expect(groupDecimalDigits('9007199254740993.01')).toBe('9,007,199,254,740,993.01')
  })

  it('returns anything that is not a plain decimal unchanged', () => {
    for (const odd of ['', ' 100.00', '1e5', '1,000.00', 'NaN', '.5', '12.']) {
      expect(groupDecimalDigits(odd)).toBe(odd)
    }
  })
})
