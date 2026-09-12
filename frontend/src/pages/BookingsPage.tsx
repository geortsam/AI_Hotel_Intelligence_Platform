import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { AlertTriangle, Building2, CalendarX2, SearchX, WifiOff } from 'lucide-react'

import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { BookingCards } from '@/features/bookings/BookingCards'
import { BookingFilters } from '@/features/bookings/BookingFilters'
import { BookingTable } from '@/features/bookings/BookingTable'
import { Pagination } from '@/features/bookings/Pagination'
import {
  applyFilters,
  EMPTY_FILTERS,
  type BookingFilterState,
} from '@/features/bookings/filters'
import { useBookingList } from '@/features/bookings/useBookingList'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { useIsCompact } from '@/lib/useIsCompact'
import { describeFailure } from '@/services/api/failures'
import { DEFAULT_PAGE_SIZE } from '@/services/bookings/bookingService'
import { useHotelContext } from '@/session/HotelProvider'

import styles from './BookingsPage.module.css'

/** Every option is inside the router's own `ge=1, le=100`, so none can produce a 422. */
const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

/**
 * Bookings — the hotel operations list.
 *
 * ## What the backend gives, and what this page therefore is
 *
 * `GET /hotels/{h}/bookings` takes `page` and `page_size`. That is the entire query surface:
 * no status filter, no date range, no guest search, no sort. Verified against the live
 * OpenAPI document, and worth verifying, because **the backend ignores unrecognised query
 * parameters silently** -- `?status=confirmed` returns all 165 rows with a 200, which looks
 * exactly like a filter that matched everything. An invented parameter here would not fail
 * loudly; it would appear to work.
 *
 * So the page is honest about its shape: the server pages, and the filters narrow what the
 * page already holds, saying so in the same breath as the count. See `filters.ts`.
 *
 * ## Why there is no guest name column
 *
 * `BookingResponse` carries `guest_public_id` and no guest name, email or phone. Resolving
 * the booker for twenty rows is twenty requests -- the N+1 the brief rules out -- and
 * `GET /guests` offers no way to fetch a set of ids. The list shows the occupant named on
 * the allocation instead, labelled "Occupant" so it is not mistaken for the booker, and the
 * detail page fetches the real guest record for one booking.
 *
 * ## One request per view
 *
 * Each list item is already a complete booking, allocations and nightly rates included, so
 * nothing is fetched per row. Changing page, or page size, issues exactly one request and
 * aborts the one it supersedes.
 */
export function BookingsPage() {
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE)
  const [filters, setFilters] = useState<BookingFilterState>(EMPTY_FILTERS)

  const { status, page: result, error, retry } = useBookingList(
    hotel?.public_id ?? null,
    page,
    pageSize,
  )

  /*
   * Switching hotel resets the page.
   *
   * Without this, moving from a property with nine pages to one with two leaves `page` at 7
   * and the operator lands on an empty screen that looks like a hotel with no bookings. The
   * filters are cleared for the same reason: they were about the other property's rows.
   */
  useEffect(() => {
    setPage(1)
    setFilters(EMPTY_FILTERS)
  }, [hotel?.public_id])

  const isCompact = useIsCompact()
  const items = result?.items ?? []
  const visible = useMemo(() => applyFilters(items, filters), [items, filters])

  /* --- no hotel to list bookings for --------------------------------------------------- */

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Bookings are held per property, and this account is not yet a member of one. An owner or manager can add you to a hotel from its member list."
        />
      </Frame>
    )
  }

  if (hotelContext.status === 'error') {
    return (
      <Frame>
        <StateMessage
          icon={WifiOff}
          tone="alert"
          title="Could not load your hotels"
          detail="The list of properties you have access to could not be retrieved. Check your connection and try again."
          onRetry={hotelContext.retry}
        />
      </Frame>
    )
  }

  /* --- failure ------------------------------------------------------------------------- */

  if (status === 'error' && error !== null) {
    const notice = describeFailure(error, {
      notFound: {
        title: 'Hotel not available',
        detail:
          'This hotel could not be found, or your access to it has been removed. Try selecting another hotel.',
        canRetry: false,
      },
      serverFault: {
        title: 'Bookings are temporarily unavailable',
        detail: 'The list could not be loaded. Try again shortly.',
        canRetry: true,
      },
    })
    return (
      <Frame>
        <StateMessage
          icon={AlertTriangle}
          tone="alert"
          title={notice.title}
          detail={notice.detail}
          {...(notice.canRetry ? { onRetry: retry } : {})}
        />
      </Frame>
    )
  }

  /* --- loading ------------------------------------------------------------------------- */

  const isLoading = hotel === null || status === 'idle' || status === 'loading'

  if (isLoading) {
    return (
      <Frame>
        <p className={styles.loadingNote} role="status">
          Loading bookings…
        </p>
        {isCompact ? (
          <BookingCards bookings={[]} isLoading skeletonRows={5} />
        ) : (
          <BookingTable bookings={[]} timeZone="UTC" isLoading skeletonRows={8} />
        )}
      </Frame>
    )
  }

  /* --- ready --------------------------------------------------------------------------- */

  const total = result?.total ?? 0
  const pages = result?.pages ?? 0

  return (
    <Frame>
      <BookingFilters
        value={filters}
        onChange={setFilters}
        pageCount={items.length}
        matchCount={visible.length}
        totalCount={total}
        disabled={total === 0}
      />

      {total === 0 ? (
        // The hotel genuinely has no bookings. Not a failure, and not a filtered result.
        <StateMessage
          icon={CalendarX2}
          tone="status"
          title="No bookings yet"
          detail="This hotel has no bookings recorded. New reservations will appear here as they are taken."
        />
      ) : visible.length === 0 ? (
        // The page has rows; none matches the filters. The distinction matters, and so does
        // saying that other pages were never searched.
        <StateMessage
          icon={SearchX}
          tone="status"
          title="No bookings on this page match"
          detail={`None of the ${items.length} bookings on page ${page} of ${pages} match these filters. Filters apply to the current page only — try another page, a larger page size, or clear the filters.`}
        />
      ) : isCompact ? (
        <BookingCards bookings={visible} />
      ) : (
        <BookingTable bookings={visible} timeZone={hotel.timezone} />
      )}

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        pages={pages}
        pageSizeOptions={PAGE_SIZE_OPTIONS}
        onPageChange={setPage}
        onPageSizeChange={(next) => {
          // A new page size makes the old offset meaningless -- page 5 of 20-row pages is not
          // page 5 of 100-row pages -- so the view returns to the first page rather than
          // landing somewhere the operator did not choose.
          setPageSize(next)
          setPage(1)
        }}
      />
    </Frame>
  )
}

/**
 * The page frame, held constant across every state.
 *
 * The heading and description do not move as data arrives or a request fails, so the page
 * does not reflow under the pointer and a screen-reader user does not lose their place.
 */
function Frame({ children }: { readonly children: ReactNode }) {
  return (
    <PageContainer>
      <SectionHeader
        as="h2"
        title="Bookings"
        description="Arrivals, in-house stays and completed bookings for the selected property. Select a reference to open a booking."
      />
      {children}
    </PageContainer>
  )
}
