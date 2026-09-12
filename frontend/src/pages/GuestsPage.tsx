import { useEffect, useState, type ReactNode } from 'react'
import { AlertTriangle, Building2, Plus, UserX, WifiOff } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { Pagination } from '@/features/bookings/Pagination'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { GuestCards } from '@/features/guests/GuestCards'
import { GuestForm } from '@/features/guests/GuestForm'
import { GuestTable } from '@/features/guests/GuestTable'
import { useGuestList } from '@/features/guests/useGuestList'
import { formatCount } from '@/lib/format'
import { useIsCompact } from '@/lib/useIsCompact'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useHotelContext } from '@/session/HotelProvider'

import styles from './GuestsPage.module.css'

/** Every option is inside the router's own `ge=1, le=100`, so none can produce a 422. */
const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

const LIST_FAILURE_COPY = {
  notFound: {
    title: 'Hotel not available',
    detail:
      'This property could not be found, or your access to it has been removed. Try selecting another hotel.',
    canRetry: false,
  },
  serverFault: {
    title: 'Guests are temporarily unavailable',
    detail: 'The guest list could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

const CREATE_FAILURE_COPY = {
  forbidden: {
    title: 'Nothing was created',
    detail:
      'Adding a guest needs the staff role on this property, which this account does not have. Reading the list does not — so the guests above are complete, and nothing was written.',
    canRetry: false,
  },
  notFound: {
    title: 'Nothing was created',
    detail: 'This property could not be found, so there was nothing to add the guest to.',
    canRetry: false,
  },
  conflict: {
    title: 'Nothing was created',
    detail:
      'Another guest at this property already uses that email address. An address belongs to at most one guest here — find the existing record, or leave the address off.',
    canRetry: false,
  },
  serverFault: {
    title: 'Nothing was created',
    detail: 'The guest could not be created. Nothing was written, so it can be tried again.',
    canRetry: true,
  },
} as const

/**
 * Guests &mdash; the people on file at one property.
 *
 * ## What the backend gives, and what this page therefore is
 *
 * `GET /hotels/{h}/guests` takes `page` and `page_size` (max 100). That is the entire query
 * surface: **no search, no filter, no sort**. Verified against the live OpenAPI document and
 * worth verifying, because the backend **ignores an unrecognised query parameter silently** —
 * `?search=smith` returned all twelve guests with a 200, which looks exactly like a search
 * that matched everything.
 *
 * So this page has no search box. Narrowing only the rows already fetched would be worse here
 * than almost anywhere: a guest list is consulted to answer "is this person already on file",
 * and a box that silently searched twenty of twelve hundred records would answer "no" for
 * someone sitting on page 3 — and a second record would be created for them. The page size
 * control is the honest lever: a hundred rows in one request, ordered by surname.
 *
 * ## Guests belong to a property, not to the platform
 *
 * Every route is nested under the hotel, and the schema is explicit that the same physical
 * person at two properties is two rows with two public ids. The page says so, because an
 * operator who expects a shared address book would otherwise read an empty list as data loss.
 */
export function GuestsPage() {
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected

  const guests = useGuestList(hotel?.public_id ?? null)
  const [adding, setAdding] = useState(false)
  const isCompact = useIsCompact()

  /*
   * Switching property closes the form and clears its banners.
   *
   * Without this, a create refused at one hotel leaves its refusal above a form now aimed at
   * another — and the operator would be reading a message about a property they have left.
   */
  useEffect(() => {
    setAdding(false)
    guests.dismiss()
    // `dismiss` is stable; the hotel is what this reacts to.
  }, [hotel?.public_id])

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Guests are held per property, and this account is not yet a member of one. An owner or manager can add you to a hotel from its member list."
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

  const listFailure = guests.error === null ? null : describeFailure(guests.error, LIST_FAILURE_COPY)
  const createFailure =
    guests.createError === null ? null : describeFailure(guests.createError, CREATE_FAILURE_COPY)
  const timeZone = hotel?.timezone ?? 'UTC'

  return (
    <Frame>
      <div className={styles.bar}>
        <p className={styles.scope}>
          Guests of {hotel?.name ?? 'this property'}, ordered by surname. A guest record belongs
          to one property; the same person at another is a separate record.
        </p>
        {adding ? null : (
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              guests.dismiss()
              setAdding(true)
            }}
          >
            <Plus size={16} aria-hidden="true" />
            Add guest
          </Button>
        )}
      </div>

      {guests.created ? (
        <p className={styles.success} role="status">
          Guest created. The server recorded {guests.created.last_name}, {guests.created.first_name}
          .
        </p>
      ) : null}

      {createFailure ? (
        <div className={styles.failure} role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            <p className={styles.failureTitle}>{createFailure.title}</p>
            <p className={styles.failureDetail}>{createFailure.detail}</p>
          </div>
        </div>
      ) : null}

      {adding ? (
        <div className={styles.formPanel}>
          <GuestForm
            busy={guests.creating}
            onDirty={guests.dismiss}
            onCancel={() => {
              setAdding(false)
            }}
            onCreate={(payload) => {
              void guests.create(payload).then((accepted) => {
                if (accepted) {
                  setAdding(false)
                }
              })
            }}
          />
        </div>
      ) : null}

      {listFailure ? (
        <StateMessage
          icon={guests.error?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
          tone="alert"
          title={listFailure.title}
          detail={listFailure.detail}
          {...(listFailure.canRetry ? { onRetry: guests.reload } : {})}
        />
      ) : guests.status === 'loading' || guests.status === 'idle' ? (
        <div className={styles.skeleton} role="status" aria-busy="true">
          <Skeleton height="12rem" label="Loading guests" />
        </div>
      ) : guests.guests.length === 0 ? (
        <StateMessage
          icon={UserX}
          tone="status"
          title="No guests on file"
          detail="This property has no guest records yet. A guest is created here, or arrives with the first reservation made for them."
        />
      ) : (
        <>
          <p className={styles.count} role="status">
            {formatCount(guests.total)} {guests.total === 1 ? 'guest' : 'guests'} on file. Counted
            by the server across every page.
          </p>

          {isCompact ? (
            <GuestCards guests={guests.guests} timeZone={timeZone} />
          ) : (
            <GuestTable guests={guests.guests} timeZone={timeZone} />
          )}

          <Pagination
            page={guests.page}
            pageSize={guests.pageSize}
            total={guests.total}
            pages={guests.pages}
            onPageChange={guests.setPage}
            onPageSizeChange={guests.setPageSize}
            pageSizeOptions={PAGE_SIZE_OPTIONS}
            itemNoun={{ singular: 'guest', plural: 'guests' }}
          />
        </>
      )}
    </Frame>
  )
}

/** The page heading, shared by every state so the frame never jumps. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Guests"
        description="The people on file at this property — their contact details, preferences and marketing consent."
      />
      <div className={styles.page}>{children}</div>
    </PageContainer>
  )
}
