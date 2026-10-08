import { describe, expect, it } from 'vitest'

import { ApiError } from '@/services/api/ApiError'
import { describeFailure, STALE_UPDATE_NOTICE } from '@/services/api/failures'
import { changedFields, ifMatch, STALE_UPDATE } from '@/services/api/preconditions'

/* Issue H6: conditional updates, as the client sees them. */

describe('ifMatch', () => {
  it('quotes the updated_at exactly as the API returned it', () => {
    expect(ifMatch('2026-10-09T13:00:00.123456+03:00')).toEqual({
      'If-Match': '"2026-10-09T13:00:00.123456+03:00"',
    })
    // Not reformatted: trailing zeros, offset and missing microseconds are left alone.
    expect(ifMatch('2026-10-09T10:00:00Z')).toEqual({ 'If-Match': '"2026-10-09T10:00:00Z"' })
  })
})

interface Row {
  readonly name: string
  readonly email: string | null
  readonly notes: string | null
  readonly opt_in: boolean
  readonly floor: number
}

describe('changedFields', () => {
  const before: Row = { name: 'Elena', email: 'e@example.test', notes: null, opt_in: true, floor: 2 }

  it('is empty when nothing changed', () => {
    expect(changedFields(before, { ...before })).toEqual({})
  })

  it('keeps only the entries that differ', () => {
    expect(changedFields(before, { ...before, name: 'Eleni', floor: 3 })).toEqual({
      name: 'Eleni',
      floor: 3,
    })
  })

  it('treats an intentional clearing as a change to null', () => {
    expect(changedFields(before, { ...before, email: null })).toEqual({ email: null })
  })

  it('treats setting a cleared field as a change from null', () => {
    expect(changedFields(before, { ...before, notes: 'Late arrival' })).toEqual({
      notes: 'Late arrival',
    })
  })

  it('sends a boolean flipped either way, and not one left alone', () => {
    expect(changedFields(before, { ...before, opt_in: false })).toEqual({ opt_in: false })
    expect(changedFields({ ...before, opt_in: false }, before)).toEqual({ opt_in: true })
  })

  it('distinguishes an empty string from null', () => {
    expect(changedFields({ a: '' as string | null }, { a: null })).toEqual({ a: null })
    expect(changedFields({ a: null as string | null }, { a: '' })).toEqual({ a: '' })
  })
})

describe('describeFailure on a stale update', () => {
  const stale = new ApiError(412, STALE_UPDATE, 'The server’s words, never displayed.')

  it('offers a reload, not a retry', () => {
    const notice = describeFailure(stale)
    expect(notice).toEqual(STALE_UPDATE_NOTICE)
    expect(notice.canReload).toBe(true)
    expect(notice.canRetry).toBe(false)
    expect(notice.detail).not.toMatch(/never displayed/)
  })

  it('takes a screen’s own wording for the record', () => {
    const own = { title: 'Nothing was saved', detail: 'This guest…', canRetry: false, canReload: true }
    expect(describeFailure(stale, { stale: own })).toBe(own)
  })

  it('goes by the code alone: a 412 with another code is not stale', () => {
    const other = new ApiError(412, 'PRECONDITION_FAILED', 'Someone else changed this record.')
    expect(describeFailure(other).canReload).toBeUndefined()
  })

  it('never treats a 409, whatever it says, as stale', () => {
    const conflict = new ApiError(409, 'CONFLICT', 'STALE_UPDATE: someone else changed it')
    expect(describeFailure(conflict).canReload).toBeUndefined()
  })
})
