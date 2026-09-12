import { useMemo, type ReactNode } from 'react'
import { AlertTriangle, Building2, Search, WifiOff } from 'lucide-react'

import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { AvailabilityForm } from '@/features/availability/AvailabilityForm'
import { AvailabilityResults } from '@/features/availability/AvailabilityResults'
import { useAvailability } from '@/features/availability/useAvailability'
import { todayInZone } from '@/features/dashboard/period'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useHotelContext } from '@/session/HotelProvider'

import styles from './AvailabilityPage.module.css'

/**
 * What a failed **search** means. Never the backend's own words.
 *
 * A 404 here is specific: the hotel is unreachable, or the room type asked for is not a code
 * at this property -- including one that belongs to another hotel. It is emphatically **not**
 * "nothing is available", which arrives as a 200.
 */
const SEARCH_FAILURE_COPY = {
  notFound: {
    title: 'That search could not be run',
    detail:
      'This property could not be found, or the room type asked for is not one of its types. A code from another property is not valid here. Nothing about availability has been determined.',
    canRetry: false,
  },
  serverFault: {
    title: 'Availability is temporarily unavailable',
    detail:
      'The search could not be completed. This says nothing about whether rooms are free — the question was not answered.',
    canRetry: true,
  },
} as const

/**
 * Availability &mdash; what this property could sell for a stay.
 *
 * ## The database is the authority, and this page is a client of it
 *
 * Every figure on screen came back from `GET /hotels/{h}/availability`: the verdict
 * (`sufficient`), the free-room count, each type's availability and the individual room
 * numbers. The service answering it runs one query that mirrors the database's own exclusion
 * constraint over half-open date ranges.
 *
 * Nothing here inspects bookings, subtracts dates, counts rooms, reasons about overlap or
 * infers anything from a room's housekeeping status. The difference is not cosmetic: a
 * browser reconstructing that rule would be a second definition of "available", and the two
 * would disagree the first time someone booked between the two requests.
 *
 * ## Nothing is searched until someone asks
 *
 * There is no default search. Availability is a question about specific dates, and answering
 * one nobody asked would put a result on screen that looks authoritative and was not
 * requested.
 *
 * ## A search is read-only
 *
 * It writes nothing and holds nothing. The page says so, because a screen that lists room
 * numbers for a stay invites the assumption that they are now spoken for.
 */
export function AvailabilityPage() {
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected

  const availability = useAvailability(hotel?.public_id ?? null)
  const timeZone = hotel?.timezone ?? 'UTC'
  // The property's today, not the browser's -- the same rule every dated screen here follows.
  const today = useMemo(() => todayInZone(timeZone), [timeZone])

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Availability is searched per property, and this account is not yet a member of one. An owner or manager can add you to a hotel from its member list."
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

  if (availability.typesError !== null) {
    const failure = describeFailure(availability.typesError, {
      notFound: {
        title: 'Property not available',
        detail: 'This property could not be found, or your access to it has been removed.',
        canRetry: false,
      },
      serverFault: {
        title: 'Room types are temporarily unavailable',
        detail:
          'The room types could not be loaded. A search can still be run across every type, but no type can be named until this succeeds.',
        canRetry: true,
      },
    })
    return (
      <Frame>
        <StateMessage
          icon={availability.typesError.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
          tone="alert"
          title={failure.title}
          detail={failure.detail}
        />
      </Frame>
    )
  }

  const searchFailure =
    availability.error === null ? null : describeFailure(availability.error, SEARCH_FAILURE_COPY)

  return (
    <Frame>
      <section className={styles.block} aria-labelledby="availability-search">
        <h2 className={styles.blockTitle} id="availability-search">
          Search
        </h2>
        <p className={styles.blockNote}>
          Dates are this property&rsquo;s own calendar ({timeZone}). The stay runs from the
          check-in night up to but not including the check-out day.
        </p>
        <div className={styles.formPanel}>
          <AvailabilityForm
            types={availability.types}
            today={today}
            busy={availability.status === 'searching'}
            onDirty={() => {
              /* Clearing on edit would remove the answer being compared against; the
               * failure banner is cleared by the next search instead. */
            }}
            onSearch={(request) => {
              void availability.search(request)
            }}
          />
        </div>
      </section>

      <section className={styles.block} aria-labelledby="availability-answer">
        <h2 className={styles.blockTitle} id="availability-answer">
          Answer
        </h2>

        {searchFailure ? (
          <StateMessage
            icon={availability.error?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
            tone="alert"
            title={searchFailure.title}
            detail={searchFailure.detail}
          />
        ) : availability.status === 'searching' ? (
          <div className={styles.searching} role="status" aria-busy="true">
            <Skeleton height="8rem" label="Searching availability" />
          </div>
        ) : availability.result !== null ? (
          <AvailabilityResults result={availability.result} />
        ) : (
          <StateMessage
            icon={Search}
            tone="status"
            title="No search has been run yet"
            detail="Choose the dates and what you need, then search. Nothing is assumed about availability until the server has been asked."
          />
        )}
      </section>
    </Frame>
  )
}

/** The page heading, shared by every state so the frame never jumps. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Availability"
        description="What this property could sell for a stay. The database decides what is free; this page asks it and shows the answer. A result holds nothing."
      />
      <div className={styles.page}>{children}</div>
    </PageContainer>
  )
}
