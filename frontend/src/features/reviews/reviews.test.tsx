import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { ReviewsPage } from '@/pages/ReviewsPage'
import { ROUTES } from '@/router/routes'
import {
  clearStoredToken,
  hotelPage,
  installFetchStub,
  renderWithAuth,
  seedStoredToken,
  TEST_HOTEL,
  TEST_USER,
  type FetchStub,
} from '@/test/harness'
import type { Review, ReviewAnalyticsResponse } from '@/types/review'

/**
 * The reviews screen, as the UI reads and moderates it.
 *
 * Mocked at `fetch`, so the method, the URL, **every query parameter** and **the exact
 * request body** are observable. Three things this file is mostly about, each a way this
 * page could be wrong in a way a screenshot would not show:
 *
 * * a **rating shown on the wrong scale** -- the fixtures deliberately mix a five-point and a
 *   ten-point review, because the live data does;
 * * a **statistic invented in the browser** -- the fixtures are chosen so that any average or
 *   sum a component might compute would be a recognisable number, and the tests assert those
 *   numbers appear nowhere;
 * * **review text treated as markup** -- one fixture's body is an XSS payload.
 *
 * The companion `architecture.node.test.ts` asserts the same claims from the source side.
 */

/* --- fixtures --------------------------------------------------------------------------- */

const BOOKING_ID = 'e5b72bf8-11a1-456b-9755-5a5ac5219782'
const OTHER_BOOKING_ID = '2c8f6a71-0d3e-4b19-9a5c-7f1e2d3c4b5a'
const GUEST_ID = '98c235eb-5ae6-4c75-a743-836e1a2b38da'

function review(overrides: Partial<Review> = {}): Review {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    booking_public_id: BOOKING_ID,
    guest_public_id: GUEST_ID,
    source: 'google',
    external_review_id: null,
    rating: '4.00',
    rating_scale: 5,
    rating_normalized: '0.8000',
    title: 'Comfortable and central',
    body: 'The harbour view was worth it. Breakfast could start earlier.',
    language: 'en',
    reviewer_name: 'A. Petrou',
    review_date: '2026-09-09',
    is_published: true,
    responded_at: null,
    created_at: '2026-09-09T18:20:00+03:00',
    updated_at: '2026-09-09T18:20:00+03:00',
    ...overrides,
  }
}

/** A ten-point review, because Booking.com scores out of ten and the seeded data has both. */
function tenPointReview(overrides: Partial<Review> = {}): Review {
  return review({
    booking_public_id: OTHER_BOOKING_ID,
    source: 'booking_com',
    rating: '9.00',
    rating_scale: 10,
    rating_normalized: '0.9000',
    title: 'Excellent stay',
    body: 'Staff went out of their way.',
    reviewer_name: 'M. Larsen',
    review_date: '2026-09-06',
    ...overrides,
  })
}

function page(items: readonly unknown[], total = items.length, pages = 1, pageNumber = 1) {
  return { items, total, page: pageNumber, page_size: 20, pages }
}

/**
 * The server's statistics.
 *
 * The figures are chosen so a client-side mistake would be visible: the two fixture reviews
 * rate 4 and 9, so a naive average would render `6.5`, and their counts would sum to `2`
 * against a server total of `55`. Neither number may appear.
 */
const ANALYTICS: ReviewAnalyticsResponse = {
  hotel_public_id: TEST_HOTEL.public_id,
  range: { date_from: '2026-09-05', date_to: '2026-09-11', days: 7 },
  totals: {
    review_count: 55,
    published_count: 46,
    average_rating_normalized: '0.7885',
  },
  rating_distribution: [
    { bucket: 1, lower_bound: '0.0000', upper_bound: '0.2000', count: 0 },
    { bucket: 2, lower_bound: '0.2000', upper_bound: '0.4000', count: 0 },
    { bucket: 3, lower_bound: '0.4000', upper_bound: '0.6000', count: 12 },
    { bucket: 4, lower_bound: '0.6000', upper_bound: '0.8000', count: 28 },
    { bucket: 5, lower_bound: '0.8000', upper_bound: '1.0000', count: 21 },
  ],
  by_source: [
    {
      source: 'booking_com',
      review_count: 20,
      published_count: 18,
      average_rating_normalized: '0.7850',
    },
    { source: 'google', review_count: 18, published_count: 14, average_rating_normalized: '0.7833' },
    // A code outside the typed union, to prove an unknown channel renders rather than crashes.
    { source: 'kayak', review_count: 2, published_count: 2, average_rating_normalized: '0.9000' },
  ],
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

/**
 * Registration order is load-bearing.
 *
 * The stub matches the first handler whose fragment the URL **contains**, so
 * `/analytics/reviews` must be registered before anything matching `/reviews`, and
 * `/hotels?page=` must be specific enough not to swallow `/hotels/{id}/reviews`.
 */
beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/analytics/reviews', { body: ANALYTICS })
  fetchStub.on('GET', '/reviews?page=', { body: page([review(), tenPointReview()], 55, 3) })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
})

function renderReviews() {
  return renderWithAuth([{ path: ROUTES.reviews, element: <ReviewsPage /> }], {
    initialEntries: [ROUTES.reviews],
  })
}

function requestsFor(fragment: string, method = 'GET') {
  return fetchStub.calls.filter((call) => call.method === method && call.url.includes(fragment))
}

function lastRequestFor(fragment: string, method = 'GET') {
  const matches = requestsFor(fragment, method)
  return matches[matches.length - 1]
}

function visibleText(): string {
  return document.body.textContent ?? ''
}

/* --- reading the list ------------------------------------------------------------------- */

describe('the review list', () => {
  it('asks the documented endpoint, with only the parameters it has', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    const request = lastRequestFor('/reviews?page=')
    expect(request).toBeDefined()
    const url = new URL(request!.url, 'http://localhost')
    expect(url.pathname).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/reviews`)
    expect([...url.searchParams.keys()].sort()).toEqual(['page', 'page_size'])
    expect(url.searchParams.get('page')).toBe('1')
    expect(url.searchParams.get('page_size')).toBe('20')
    expect(request!.authorization).toMatch(/^Bearer /)
  })

  it('renders the guest’s words as written', async () => {
    renderReviews()

    expect(await screen.findByText('Comfortable and central')).toBeInTheDocument()
    expect(
      screen.getByText('The harbour view was worth it. Breakfast could start earlier.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'A. Petrou' })).toBeInTheDocument()
  })

  it('reports the count the server gave, not the number of cards on screen', async () => {
    renderReviews()

    // Two cards rendered; 55 is the server's count across three pages.
    expect(await screen.findByText(/55 reviews/)).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText(/Page 1 of 3/)).toBeInTheDocument()
  })

  it('says a property has no reviews without calling that a rating', async () => {
    fetchStub.on('GET', '/reviews?page=', { body: page([], 0, 0) })
    renderReviews()

    expect(await screen.findByText('No reviews yet')).toBeInTheDocument()
    // Not a zero rating, and not a zero average.
    expect(visibleText()).not.toMatch(/0 \/ 5/)
  })

  it('treats a 200 that is not the documented shape as a failure, not an empty list', async () => {
    fetchStub.on('GET', '/reviews?page=', { body: { items: null, total: 0 } })
    renderReviews()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
    expect(screen.queryByText('No reviews yet')).not.toBeInTheDocument()
  })

  it('renders a 404 as an unreachable property, not as an absence of reviews', async () => {
    fetchStub.on('GET', '/reviews?page=', {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Hotel not found.' } },
    })
    renderReviews()

    expect(await screen.findByText('Hotel not available')).toBeInTheDocument()
    expect(screen.queryByText('No reviews yet')).not.toBeInTheDocument()
  })

  it('renders a network failure as a failure', async () => {
    fetchStub.on('GET', '/reviews?page=', { networkError: true })
    renderReviews()

    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
  })

  it('makes no request per review', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    // One list, one aggregate, and nothing keyed by a booking or a guest.
    expect(requestsFor('/reviews?page=')).toHaveLength(1)
    expect(requestsFor('/analytics/reviews')).toHaveLength(1)
    expect(requestsFor('/guests')).toHaveLength(0)
    expect(fetchStub.calls.filter((c) => /\/bookings\//.test(c.url))).toHaveLength(0)
  })
})

/* --- rating semantics ------------------------------------------------------------------- */

describe('rating semantics', () => {
  it('shows each rating on the scale it was given on', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    expect(screen.getByText('4 / 5')).toBeInTheDocument()
    expect(screen.getByText('9 / 10')).toBeInTheDocument()
  })

  it('never converts a ten-point score to a five-point one', async () => {
    renderReviews()
    await screen.findByText('Excellent stay')

    // 9/10 as a five-point score would be 4.5; as a fraction, 0.9. Neither may appear on the
    // review itself, because the guest did not give either number.
    const card = screen.getByRole('heading', { name: 'M. Larsen' }).closest('article')!
    expect(within(card).queryByText(/4\.5/)).not.toBeInTheDocument()
    expect(within(card).queryByText(/0\.9/)).not.toBeInTheDocument()
  })

  it('spells the scale out for a screen reader', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    // "4 / 5" is read as "four slash five" or worse; the accessible name says it plainly.
    expect(screen.getByLabelText('Rated 4 out of 5')).toBeInTheDocument()
    expect(screen.getByLabelText('Rated 9 out of 10')).toBeInTheDocument()
  })

  it('averages nothing across the two scales', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    // (4 + 9) / 2 = 6.5, the number a naive average of this page would produce. 6.5
    // cannot appear anywhere on this page for an innocent reason, so the whole document
    // is the right scope for it.
    expect(visibleText()).not.toMatch(/6\.5/)

    /*
     * 13 is their sum. Unlike 6.5, a bare 13 DOES have an innocent reading here: the page
     * prints a reporting period, and 13 is a day of the month. Asserting it against the
     * whole document made this test fail on the thirteenth of a month over a date -- which
     * is not what the invariant is about. The invariant is that no blended value reaches
     * the UI that renders a rating, so that is what this reads: the summary's headline
     * figures, and each review's own rating including its accessible name (a wrong scale
     * would corrupt the name too, and `textContent` alone would not see it).
     */
    const figures = screen.getByText('Average rating').closest('dl')!
    const headline = [...figures.querySelectorAll('dd')].map((element) => element.textContent)
    const rated = screen.getAllByLabelText(/^Rated /)
    // A scope that matched nothing would pass no matter what the page rendered.
    expect(headline).toHaveLength(3)
    expect(rated).toHaveLength(2)
    /*
     * Each value separately, joined by a separator. Reading `textContent` off a container
     * instead would run the label into the value -- `Average rating13.0 / 5` -- and there
     * is no word boundary between `g` and `1`, so `13` would silently stop matching.
     * Mutation testing caught exactly that; hence the values, not their container.
     */
    const ratingText = [
      ...headline,
      ...rated.map((element) => `${element.getAttribute('aria-label')} ${element.textContent}`),
    ].join(' | ')

    expect(ratingText).not.toMatch(/\b13\b/)
  })
})

/* --- the server's statistics ------------------------------------------------------------ */

describe('the summary', () => {
  it('asks the analytics endpoint with both required bounds', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    const url = new URL(lastRequestFor('/analytics/reviews')!.url, 'http://localhost')
    expect(url.searchParams.get('date_from')).not.toBeNull()
    expect(url.searchParams.get('date_to')).not.toBeNull()
  })

  it('prints the server’s counts and average', async () => {
    renderReviews()

    expect(await screen.findByText('55')).toBeInTheDocument()
    expect(screen.getByText('46')).toBeInTheDocument()
    // 0.7885 of five, rendered with the scale in the string. Scoped to the headline figure:
    // two of the per-channel averages round to the same string, which is itself a reason the
    // scale belongs in the text.
    const average = screen.getByText('Average rating').closest('div')!
    expect(within(average).getByText('3.9 / 5')).toBeInTheDocument()
  })

  it('prints the distribution the server bucketed', async () => {
    renderReviews()

    const table = await screen.findByRole('table', { name: /Reviews per band/ })
    expect(within(table).getByText('12')).toBeInTheDocument()
    expect(within(table).getByText('28')).toBeInTheDocument()
    expect(within(table).getByText('21')).toBeInTheDocument()
    // The bounds are the server's own, not recomputed from a bucket number.
    expect(within(table).getByText('0.6000 – 0.8000')).toBeInTheDocument()
  })

  it('renders a channel the typed vocabulary does not contain', async () => {
    renderReviews()

    const table = await screen.findByRole('table', { name: /Reviews per channel/ })
    // `kayak` is not in `ReviewSource`. It must render as itself rather than crash or blank.
    expect(within(table).getByText('kayak')).toBeInTheDocument()
    expect(within(table).getByText('Booking.com')).toBeInTheDocument()
  })

  it('does not refetch the statistics when a filter changes, because they cannot be filtered', async () => {
    const user = userEvent.setup()
    renderReviews()
    await screen.findByText('Comfortable and central')

    const before = requestsFor('/analytics/reviews').length
    await user.selectOptions(screen.getByLabelText('Filter by channel'), 'google')

    await waitFor(() => {
      expect(requestsFor('/reviews?page=').length).toBeGreaterThan(1)
    })
    expect(requestsFor('/analytics/reviews')).toHaveLength(before)
  })

  it('keeps the list readable when the statistics fail', async () => {
    fetchStub.on('GET', '/analytics/reviews', {
      status: 500,
      body: { error: { code: 'INTERNAL_ERROR', message: 'Internal server error.' } },
    })
    renderReviews()

    expect(
      await screen.findByText('Review statistics are temporarily unavailable'),
    ).toBeInTheDocument()
    expect(screen.getByText('Comfortable and central')).toBeInTheDocument()
    // And no substitute average is computed from the rows that did load.
    expect(visibleText()).not.toMatch(/6\.5/)
  })

  it('shows no average when the server reports none', async () => {
    fetchStub.on('GET', '/analytics/reviews', {
      body: {
        ...ANALYTICS,
        totals: { review_count: 0, published_count: 0, average_rating_normalized: null },
      },
    })
    renderReviews()
    await screen.findByText('Comfortable and central')

    // An em dash, not a zero: a period with no reviews has no average.
    const figures = screen.getByText('Average rating').closest('div')!
    expect(within(figures).getByText('—')).toBeInTheDocument()
  })
})

/* --- filters and pagination -------------------------------------------------------------- */

describe('filters and pagination', () => {
  it('sends the channel as the schema names it', async () => {
    const user = userEvent.setup()
    renderReviews()
    await screen.findByText('Comfortable and central')

    await user.selectOptions(screen.getByLabelText('Filter by channel'), 'tripadvisor')

    await waitFor(() => {
      const url = new URL(lastRequestFor('/reviews?page=')!.url, 'http://localhost')
      expect(url.searchParams.get('source')).toBe('tripadvisor')
    })
  })

  it('offers only the channels the schema has', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    const options = within(screen.getByLabelText('Filter by channel'))
      .getAllByRole('option')
      .map((option) => (option as HTMLOptionElement).value)
    expect(options).toEqual([
      '',
      'direct',
      'booking_com',
      'tripadvisor',
      'google',
      'expedia',
      'airbnb',
      'other',
    ])
  })

  it('treats visibility as the tri-state it is on the wire', async () => {
    const user = userEvent.setup()
    renderReviews()
    await screen.findByText('Comfortable and central')

    await user.selectOptions(screen.getByLabelText('Filter by visibility'), 'hidden')
    await waitFor(() => {
      const url = new URL(lastRequestFor('/reviews?page=')!.url, 'http://localhost')
      expect(url.searchParams.get('is_published')).toBe('false')
    })

    await user.selectOptions(screen.getByLabelText('Filter by visibility'), 'published')
    await waitFor(() => {
      const url = new URL(lastRequestFor('/reviews?page=')!.url, 'http://localhost')
      expect(url.searchParams.get('is_published')).toBe('true')
    })

    // Cleared: the parameter is absent, which is not the same request as `is_published=false`.
    await user.selectOptions(screen.getByLabelText('Filter by visibility'), '')
    await waitFor(() => {
      const url = new URL(lastRequestFor('/reviews?page=')!.url, 'http://localhost')
      expect(url.searchParams.has('is_published')).toBe(false)
    })
  })

  it('returns to the first page when a filter narrows the list', async () => {
    const user = userEvent.setup()
    renderReviews()
    await screen.findByText(/55 reviews/)

    await user.click(screen.getByRole('button', { name: /Next/ }))
    await waitFor(() => {
      const url = new URL(lastRequestFor('/reviews?page=')!.url, 'http://localhost')
      expect(url.searchParams.get('page')).toBe('2')
    })

    await user.selectOptions(screen.getByLabelText('Filter by channel'), 'google')
    await waitFor(() => {
      const url = new URL(lastRequestFor('/reviews?page=')!.url, 'http://localhost')
      expect(url.searchParams.get('page')).toBe('1')
      expect(url.searchParams.get('source')).toBe('google')
    })
  })

  it('never asks for a page larger than the router allows', async () => {
    const user = userEvent.setup()
    renderReviews()
    await screen.findByText(/55 reviews/)

    await user.selectOptions(screen.getByLabelText('Per page'), '100')
    await waitFor(() => {
      const url = new URL(lastRequestFor('/reviews?page=')!.url, 'http://localhost')
      // `le=100` on the router; 101 is a 422.
      expect(Number(url.searchParams.get('page_size'))).toBeLessThanOrEqual(100)
    })
  })

  it('says an empty filtered result is a filtered result', async () => {
    const user = userEvent.setup()
    renderReviews()
    await screen.findByText('Comfortable and central')

    fetchStub.on('GET', '/reviews?page=', { body: page([], 0, 0) })
    await user.selectOptions(screen.getByLabelText('Filter by channel'), 'airbnb')

    expect(await screen.findByText('No reviews match those filters')).toBeInTheDocument()
    expect(screen.queryByText('No reviews yet')).not.toBeInTheDocument()
  })
})

/* --- moderation --------------------------------------------------------------------------- */

describe('moderation', () => {
  it('hides a review with the only payload PATCH accepts, after confirming', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/review', { body: review({ is_published: false }) })
    renderReviews()
    await screen.findByText('Comfortable and central')

    const card = screen.getByRole('heading', { name: 'A. Petrou' }).closest('article')!
    await user.click(within(card).getByRole('button', { name: 'Hide review' }))
    // Filling the form does not post it.
    expect(requestsFor('/review', 'PATCH')).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: 'Yes, hide it' }))

    await waitFor(() => {
      expect(requestsFor('/review', 'PATCH')).toHaveLength(1)
    })
    const request = requestsFor('/review', 'PATCH')[0]!
    expect(new URL(request.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/bookings/${BOOKING_ID}/review`,
    )
    expect(JSON.parse(request.body!)).toEqual({ is_published: false })
  })

  it('publishes without a confirmation, because that is not a withdrawal', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/reviews?page=', { body: page([review({ is_published: false })], 1, 1) })
    fetchStub.on('PATCH', '/review', { body: review({ is_published: true }) })
    renderReviews()
    await screen.findByText('Comfortable and central')

    await user.click(screen.getByRole('button', { name: 'Publish review' }))

    await waitFor(() => {
      expect(requestsFor('/review', 'PATCH')).toHaveLength(1)
    })
    expect(JSON.parse(requestsFor('/review', 'PATCH')[0]!.body!)).toEqual({ is_published: true })
  })

  it('marks a review answered with an instant, and clears it with an explicit null', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/review', { body: review({ responded_at: '2026-09-11T10:00:00Z' }) })
    renderReviews()
    await screen.findByText('Comfortable and central')

    // The re-read happens the moment the PATCH resolves, so the answered fixture is in place
    // before the click that causes it.
    fetchStub.on('GET', '/reviews?page=', {
      body: page([review({ responded_at: '2026-09-11T10:00:00Z' })], 1, 1),
    })

    const card = screen.getByRole('heading', { name: 'A. Petrou' }).closest('article')!
    await user.click(within(card).getByRole('button', { name: 'Mark answered' }))

    await waitFor(() => {
      expect(requestsFor('/review', 'PATCH')).toHaveLength(1)
    })
    const marked = JSON.parse(requestsFor('/review', 'PATCH')[0]!.body!) as Record<string, unknown>
    expect(Object.keys(marked)).toEqual(['responded_at'])
    expect(typeof marked.responded_at).toBe('string')

    // The review now comes back answered, so the control becomes the clearing one.
    await user.click(await screen.findByRole('button', { name: 'Clear answered mark' }))

    await waitFor(() => {
      expect(requestsFor('/review', 'PATCH')).toHaveLength(2)
    })
    const cleared = JSON.parse(requestsFor('/review', 'PATCH')[1]!.body!) as Record<string, unknown>
    // An explicit null, not an omitted key: omitting it would leave the column untouched.
    expect('responded_at' in cleared).toBe(true)
    expect(cleared.responded_at).toBeNull()
  })

  it('re-reads the list and the statistics after the server confirms', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/review', { body: review({ is_published: false }) })
    renderReviews()
    await screen.findByText('Comfortable and central')

    const listsBefore = requestsFor('/reviews?page=').length
    const statsBefore = requestsFor('/analytics/reviews').length

    const card = screen.getByRole('heading', { name: 'A. Petrou' }).closest('article')!
    await user.click(within(card).getByRole('button', { name: 'Hide review' }))
    await user.click(screen.getByRole('button', { name: 'Yes, hide it' }))

    await waitFor(() => {
      expect(requestsFor('/reviews?page=').length).toBeGreaterThan(listsBefore)
      expect(requestsFor('/analytics/reviews').length).toBeGreaterThan(statsBefore)
    })
  })

  it('does not flip the badge until the server has said so', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/review', {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Requires staff role.' } },
    })
    renderReviews()
    await screen.findByText('Comfortable and central')

    const card = screen.getByRole('heading', { name: 'A. Petrou' }).closest('article')!
    await user.click(within(card).getByRole('button', { name: 'Hide review' }))
    await user.click(screen.getByRole('button', { name: 'Yes, hide it' }))

    expect(await screen.findByText('Nothing was changed')).toBeInTheDocument()
    expect(screen.getByText(/needs the staff role/)).toBeInTheDocument()
    // The review is still shown as published, because it still is.
    expect(within(card).getByText('Published')).toBeInTheDocument()
    // And nothing was retried.
    expect(requestsFor('/review', 'PATCH')).toHaveLength(1)
  })

  it('sends one request when the confirmation is clicked twice', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/review', { body: review({ is_published: false }) })
    renderReviews()
    await screen.findByText('Comfortable and central')

    const card = screen.getByRole('heading', { name: 'A. Petrou' }).closest('article')!
    await user.click(within(card).getByRole('button', { name: 'Hide review' }))
    const confirm = screen.getByRole('button', { name: 'Yes, hide it' })
    await Promise.all([user.click(confirm), user.click(confirm)])

    await waitFor(() => {
      expect(requestsFor('/review', 'PATCH').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/review', 'PATCH')).toHaveLength(1)
  })

  it('never renders the backend’s own words on a refusal', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/review', {
      status: 500,
      body: {
        error: {
          code: 'INTERNAL',
          message: 'psycopg.errors.CheckViolation: ck_reviews_rating_in_scale on table reviews',
        },
      },
    })
    renderReviews()
    await screen.findByText('Comfortable and central')

    const card = screen.getByRole('heading', { name: 'A. Petrou' }).closest('article')!
    await user.click(within(card).getByRole('button', { name: 'Hide review' }))
    await user.click(screen.getByRole('button', { name: 'Yes, hide it' }))

    expect(await screen.findByText('Nothing was changed')).toBeInTheDocument()
    for (const leak of ['psycopg', 'ck_reviews', 'CheckViolation', 'table reviews']) {
      expect(visibleText()).not.toContain(leak)
    }
  })

  it('reads back the field that changed, not whichever one is handy', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/review', { body: review({ responded_at: '2026-09-11T10:00:00Z' }) })
    renderReviews()
    await screen.findByText('Comfortable and central')

    const card = screen.getByRole('heading', { name: 'A. Petrou' }).closest('article')!
    await user.click(within(card).getByRole('button', { name: 'Mark answered' }))

    // The review is published and stays published -- but this change was about the answered
    // mark, so the confirmation must not report the publication state. Found in a browser,
    // where clearing an answered mark announced "recorded it as hidden".
    const banner = await screen.findByText(/Review marked answered/)
    expect(banner).toHaveTextContent('The server recorded the time it was answered.')
    expect(banner).not.toHaveTextContent(/recorded it as (?:published|hidden)/)
  })

  it('offers no moderation for a review with no stay, and says why', async () => {
    fetchStub.on('GET', '/reviews?page=', {
      body: page(
        [review({ booking_public_id: null, guest_public_id: null, reviewer_name: 'Imported' })],
        1,
        1,
      ),
    })
    renderReviews()
    await screen.findByText('Comfortable and central')

    expect(screen.queryByRole('button', { name: 'Hide review' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Mark answered' })).not.toBeInTheDocument()
    expect(screen.getByText(/no address of its own/)).toBeInTheDocument()
  })

  it('offers no delete anywhere, because the API has none', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    for (const name of [/delete/i, /remove/i, /approve/i, /reject/i, /flag/i]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
  })
})

/* --- recording a review ------------------------------------------------------------------ */

describe('recording a review', () => {
  async function openForm() {
    const user = userEvent.setup()
    renderReviews()
    await screen.findByText('Comfortable and central')
    await user.click(screen.getByRole('button', { name: /Record a review/ }))
    return user
  }

  it('posts to the stay’s own URL, with exactly the documented body', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/review', { status: 201, body: review() })

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.type(screen.getByLabelText('Rating'), '4')
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    await waitFor(() => {
      expect(requestsFor('/review', 'POST')).toHaveLength(1)
    })
    const request = requestsFor('/review', 'POST')[0]!
    // The stay is the address, not a field in the payload.
    expect(new URL(request.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/bookings/${BOOKING_ID}/review`,
    )
    const body = JSON.parse(request.body!) as Record<string, unknown>
    expect(body).toEqual({
      source: 'direct',
      rating: '4',
      rating_scale: 5,
      review_date: expect.any(String),
      is_published: true,
    })
    // A string, not a number that happens to print the same way.
    expect(typeof body.rating).toBe('string')
    // The author is derived from the booking; sending it is a 422.
    expect(body).not.toHaveProperty('guest_public_id')
    // GENERATED ALWAYS in PostgreSQL; sending it is a 422.
    expect(body).not.toHaveProperty('rating_normalized')
  })

  it('sends the optional fields that were filled in, and omits the ones that were not', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/review', { status: 201, body: review() })

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.selectOptions(screen.getByLabelText('Channel'), 'tripadvisor')
    await user.type(screen.getByLabelText('Rating'), '4.5')
    await user.type(screen.getByLabelText('Reviewer name (optional)'), 'K. Ioannou')
    await user.type(screen.getByLabelText('Language (optional)'), 'EL')
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    await waitFor(() => {
      expect(requestsFor('/review', 'POST')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/review', 'POST')[0]!.body!) as Record<string, unknown>
    expect(body).toMatchObject({
      source: 'tripadvisor',
      rating: '4.5',
      reviewer_name: 'K. Ioannou',
      // Lower-cased to satisfy `^[a-z]{2}$`, which is a canonicalisation of a code rather
      // than a rewrite of anything the guest wrote.
      language: 'el',
    })
    expect(body).not.toHaveProperty('title')
    expect(body).not.toHaveProperty('body')
    expect(body).not.toHaveProperty('external_review_id')
  })

  it('records a ten-point rating on its own scale', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/review', { status: 201, body: tenPointReview() })

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.selectOptions(screen.getByLabelText('Rating scale'), '10')
    await user.type(screen.getByLabelText('Rating'), '9')
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    await waitFor(() => {
      expect(requestsFor('/review', 'POST')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/review', 'POST')[0]!.body!) as Record<string, unknown>
    expect(body).toMatchObject({ rating: '9', rating_scale: 10 })
  })

  it('refuses a rating above its scale at the field, without a request', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.type(screen.getByLabelText('Rating'), '9')
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('cannot exceed its scale')
    expect(requestsFor('/review', 'POST')).toHaveLength(0)
  })

  it('refuses more precision than the column has', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.type(screen.getByLabelText('Rating'), '4.567')
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('at most two decimal places')
    expect(requestsFor('/review', 'POST')).toHaveLength(0)
  })

  it('refuses a malformed language code', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.type(screen.getByLabelText('Rating'), '4')
    await user.type(screen.getByLabelText('Language (optional)'), 'e1')
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('two-letter code')
    expect(requestsFor('/review', 'POST')).toHaveLength(0)
  })

  it('records a review hidden when asked to', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/review', { status: 201, body: review({ is_published: false }) })

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.type(screen.getByLabelText('Rating'), '4')
    await user.click(screen.getByRole('checkbox', { name: /Publish this review/ }))
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    await waitFor(() => {
      expect(requestsFor('/review', 'POST')).toHaveLength(1)
    })
    expect(JSON.parse(requestsFor('/review', 'POST')[0]!.body!)).toMatchObject({
      is_published: false,
    })
  })

  it('sends one request when the button is clicked twice', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/review', { status: 201, body: review() })

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.type(screen.getByLabelText('Rating'), '4')
    const submit = screen.getByRole('button', { name: 'Record review' })
    await Promise.all([user.click(submit), user.click(submit)])

    await waitFor(() => {
      expect(requestsFor('/review', 'POST').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/review', 'POST')).toHaveLength(1)
  })

  it('explains a 409 as one review per stay, and does not retry', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/review', {
      status: 409,
      body: { error: { code: 'CONFLICT', message: 'This booking already has a review.' } },
    })

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.type(screen.getByLabelText('Rating'), '4')
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    expect(await screen.findByText('Nothing was recorded')).toBeInTheDocument()
    expect(screen.getByText(/A stay can have exactly one/)).toBeInTheDocument()
    expect(requestsFor('/review', 'POST')).toHaveLength(1)
  })

  it('describes a failed recording as a recording, not as a change', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/review', {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Booking not found for this hotel.' } },
    })

    await user.type(screen.getByLabelText('Stay'), '00000000-0000-4000-8000-000000000000')
    await user.type(screen.getByLabelText('Rating'), '4')
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    // Not "nothing was updated": nothing was being updated. Found in a browser.
    expect(await screen.findByText('Nothing was recorded')).toBeInTheDocument()
    expect(screen.getByText(/No stay with that identifier/)).toBeInTheDocument()
    expect(visibleText()).not.toMatch(/nothing was updated/)
  })

  it('re-reads the list and the statistics after the server accepts', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/review', { status: 201, body: review() })

    const listsBefore = requestsFor('/reviews?page=').length
    const statsBefore = requestsFor('/analytics/reviews').length

    await user.type(screen.getByLabelText('Stay'), BOOKING_ID)
    await user.type(screen.getByLabelText('Rating'), '4')
    await user.click(screen.getByRole('button', { name: 'Record review' }))

    await waitFor(() => {
      expect(requestsFor('/reviews?page=').length).toBeGreaterThan(listsBefore)
      expect(requestsFor('/analytics/reviews').length).toBeGreaterThan(statsBefore)
    })
  })
})

/* --- untrusted content and security ------------------------------------------------------ */

describe('review text is untrusted content', () => {
  const PAYLOAD = '<img src=x onerror="document.title=\'pwned\'"> <script>alert(1)</script>'

  it('renders markup in a review body as text, not as HTML', async () => {
    fetchStub.on('GET', '/reviews?page=', {
      body: page([review({ body: PAYLOAD, title: PAYLOAD, reviewer_name: PAYLOAD })], 1, 1),
    })
    renderReviews()

    // The payload is on screen -- as characters.
    expect(await screen.findAllByText(PAYLOAD)).not.toHaveLength(0)
    // And no element was created from it.
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
    expect(document.title).not.toBe('pwned')
  })

  it('escapes the payload in the serialised DOM', async () => {
    fetchStub.on('GET', '/reviews?page=', { body: page([review({ body: PAYLOAD })], 1, 1) })
    renderReviews()
    await screen.findByText(PAYLOAD)

    const html = document.body.innerHTML
    expect(html).toContain('&lt;img')
    expect(html).not.toContain('<img src=x')
  })

  it('puts no credential in the page and no internal identifier on screen', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    expect(document.body.innerHTML).not.toContain('header.payload.signature-test-only')
    expect(visibleText()).not.toContain('header.payload.signature-test-only')
    // The guest id is used to say "matched" and is never printed.
    expect(visibleText()).not.toContain(GUEST_ID)

    // No numeric path segment anywhere: the API exposes no BIGINT and neither does a link.
    for (const href of screen.getAllByRole('link').map((n) => n.getAttribute('href') ?? '')) {
      for (const segment of href.split('/')) {
        expect(segment).not.toMatch(/^\d+$/)
      }
    }
  })

  it('links a review to its stay by public identifier', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    const card = screen.getByRole('heading', { name: 'A. Petrou' }).closest('article')!
    expect(within(card).getByRole('link', { name: 'View booking' })).toHaveAttribute(
      'href',
      `/bookings/${BOOKING_ID}`,
    )
  })
})

/* --- accessibility ------------------------------------------------------------------------ */

describe('accessibility', () => {
  it('presents the reviews as a list of articles with headings', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getAllByRole('article')).toHaveLength(2)
    expect(screen.getByRole('heading', { level: 1, name: 'Reviews' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Reviews' })).toBeInTheDocument()
  })

  it('announces a failure as an alert and a count as a status', async () => {
    renderReviews()
    await screen.findByText(/55 reviews/)

    // The count is an ordinary answer and is announced politely, not as an alert: a busy
    // week of reviews is not a fault.
    const statuses = screen.getAllByRole('status').map((node) => node.textContent ?? '')
    expect(statuses.some((text) => /55 reviews/.test(text))).toBe(true)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('states the source and the visibility as words, not only as colour', async () => {
    renderReviews()
    await screen.findByText('Comfortable and central')

    const card = screen.getByRole('heading', { name: 'A. Petrou' }).closest('article')!
    expect(within(card).getByText('Google')).toBeInTheDocument()
    expect(within(card).getByText('Published')).toBeInTheDocument()
  })
})
