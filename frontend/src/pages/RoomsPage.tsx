import { useEffect, useId, useState, type ReactNode } from 'react'
import { AlertTriangle, Building2, DoorClosed, Plus, WifiOff } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { Pagination } from '@/features/bookings/Pagination'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { RoomCards } from '@/features/rooms/RoomCards'
import { RoomForm } from '@/features/rooms/RoomForm'
import { RoomTable } from '@/features/rooms/RoomTable'
import { useRoomList } from '@/features/rooms/useRoomList'
import { formatCount, formatMoney } from '@/lib/format'
import { useIsCompact } from '@/lib/useIsCompact'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useHotelContext } from '@/session/HotelProvider'

import styles from './RoomsPage.module.css'

/** Every option is inside the router's own `ge=1, le=100`, so none can produce a 422. */
const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

const LIST_FAILURE_COPY = {
  notFound: {
    title: 'Rooms not available',
    detail:
      'This property or this room type could not be found. A type removed since the page loaded reads this way — try another, or reload.',
    canRetry: true,
  },
  serverFault: {
    title: 'Rooms are temporarily unavailable',
    detail: 'The room list could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

const CREATE_FAILURE_COPY = {
  forbidden: {
    title: 'Nothing was created',
    detail:
      'Adding a room needs the manager role on this property, which this account does not have. Reading the list does not — so the rooms above are complete, and nothing was written.',
    canRetry: false,
  },
  notFound: {
    title: 'Nothing was created',
    detail: 'This property or room type could not be found, so there was nothing to add the room to.',
    canRetry: false,
  },
  conflict: {
    title: 'Nothing was created',
    detail:
      'This property already has a room with that number. Room numbers are unique across the whole hotel, not just within a type — the number may be in use by a room of a different type.',
    canRetry: false,
  },
  serverFault: {
    title: 'Nothing was created',
    detail: 'The room could not be created. Nothing was written, so it can be tried again.',
    canRetry: true,
  },
} as const

/**
 * Rooms &mdash; the physical rooms of one property, by type.
 *
 * ## Why this page begins with a room type
 *
 * There is no hotel-wide room endpoint. The only collection is
 * `/hotels/{h}/room-types/{code}/rooms`, so the catalogue is fetched once and one type's
 * rooms are shown at a time.
 *
 * Building one flat list instead would mean a request per type, growing with the property,
 * and it could not page honestly: each type pages independently, so a merged list has no
 * single page number to honour. The chooser is the contract made visible rather than worked
 * around.
 *
 * ## What this page is not
 *
 * It is **room management, not availability**. `status` is the room's current operational
 * state; which dates a room is free is decided by reservations and the database's exclusion
 * constraint, and nothing here reads, derives or displays that. No occupancy is counted, no
 * inventory totalled.
 *
 * ## The query surface is a page, and nothing else
 *
 * `page` and `page_size`. No search, no status filter, no sort -- and none faked, because the
 * backend **ignores an unrecognised query parameter silently**: `?status=available` returned
 * every room with a 200, which looks exactly like a filter that matched everything.
 */
export function RoomsPage() {
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected
  const typeSelectId = useId()

  const rooms = useRoomList(hotel?.public_id ?? null)
  const [adding, setAdding] = useState(false)
  const isCompact = useIsCompact()

  /* Switching property closes the form and clears its banners: a refusal at one hotel must
   * not sit above a form now aimed at another. */
  useEffect(() => {
    setAdding(false)
    rooms.dismiss()
  }, [hotel?.public_id])

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Rooms are held per property, and this account is not yet a member of one. An owner or manager can add you to a hotel from its member list."
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

  if (rooms.typesError !== null) {
    const failure = describeFailure(rooms.typesError, {
      notFound: {
        title: 'Property not available',
        detail: 'This property could not be found, or your access to it has been removed.',
        canRetry: false,
      },
      serverFault: {
        title: 'Room types are temporarily unavailable',
        detail:
          'The room types could not be loaded, and rooms are listed by type — so there is nothing to show until this succeeds.',
        canRetry: true,
      },
    })
    return (
      <Frame>
        <StateMessage
          icon={rooms.typesError.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
          tone="alert"
          title={failure.title}
          detail={failure.detail}
          {...(failure.canRetry ? { onRetry: rooms.reload } : {})}
        />
      </Frame>
    )
  }

  if (rooms.types.length === 0) {
    return (
      <Frame>
        <div className={styles.skeleton} role="status" aria-busy="true">
          <Skeleton height="10rem" label="Loading room types" />
        </div>
      </Frame>
    )
  }

  const listFailure = rooms.error === null ? null : describeFailure(rooms.error, LIST_FAILURE_COPY)
  const createFailure =
    rooms.createError === null ? null : describeFailure(rooms.createError, CREATE_FAILURE_COPY)
  const timeZone = hotel?.timezone ?? 'UTC'
  const selected = rooms.types.find((type) => type.code === rooms.selectedType)

  return (
    <Frame>
      <div className={styles.bar}>
        <div className={styles.chooser}>
          <label className={styles.label} htmlFor={typeSelectId}>
            Room type
          </label>
          <select
            id={typeSelectId}
            className={styles.select}
            value={rooms.selectedType}
            disabled={rooms.status === 'loading'}
            onChange={(event) => {
              rooms.dismiss()
              setAdding(false)
              rooms.setSelectedType(event.target.value)
            }}
          >
            {rooms.types.map((type) => (
              <option key={type.code} value={type.code}>
                {type.name} ({type.code})
              </option>
            ))}
          </select>
          <span className={styles.hint}>
            Rooms are listed per type &mdash; the API has no property-wide room list.
          </span>
        </div>

        {adding ? null : (
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              rooms.dismiss()
              setAdding(true)
            }}
          >
            <Plus size={16} aria-hidden="true" />
            Add room
          </Button>
        )}
      </div>

      {selected ? (
        <div className={styles.typeCard}>
          <div className={styles.typeHeader}>
            <h2 className={styles.typeName}>{selected.name}</h2>
            <Badge tone={selected.is_active ? 'success' : 'danger'}>
              {selected.is_active ? 'Bookable type' : 'Withdrawn type'}
            </Badge>
          </div>
          {selected.description !== null ? (
            <p className={styles.typeDescription}>{selected.description}</p>
          ) : null}
          {/*
            * The type's own figures, from the catalogue already fetched -- not computed, and
            * not fetched per room. `base_price` is a Decimal string and is formatted, never
            * parsed for arithmetic.
            */}
          <dl className={styles.typeFacts}>
            <Fact label="Code">{selected.code}</Fact>
            <Fact label="Sleeps">
              {formatCount(selected.standard_occupancy)} standard,{' '}
              {formatCount(selected.max_occupancy)} maximum
            </Fact>
            <Fact label="Beds">{formatCount(selected.bed_count)}</Fact>
            {selected.bed_configuration !== null ? (
              <Fact label="Configuration">{selected.bed_configuration}</Fact>
            ) : null}
            <Fact label="Base price">
              {formatMoney(selected.base_price, selected.currency)}
            </Fact>
            {selected.size_sqm !== null ? <Fact label="Size">{selected.size_sqm} m²</Fact> : null}
          </dl>
        </div>
      ) : null}

      {rooms.created ? (
        <p className={styles.success} role="status">
          Room created. The server recorded {rooms.created.room_number} as{' '}
          {rooms.created.status.replace('_', ' ')}.
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
          <RoomForm
            roomTypeCode={rooms.selectedType}
            busy={rooms.creating}
            onDirty={rooms.dismiss}
            onCancel={() => {
              setAdding(false)
            }}
            onCreate={(payload) => {
              void rooms.create(payload).then((accepted) => {
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
          icon={rooms.error?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
          tone="alert"
          title={listFailure.title}
          detail={listFailure.detail}
          {...(listFailure.canRetry ? { onRetry: rooms.reload } : {})}
        />
      ) : rooms.status === 'loading' || rooms.status === 'idle' ? (
        <div className={styles.skeleton} role="status" aria-busy="true">
          <Skeleton height="10rem" label="Loading rooms" />
        </div>
      ) : rooms.rooms.length === 0 ? (
        <StateMessage
          icon={DoorClosed}
          tone="status"
          title="No rooms of this type"
          detail="This room type has no rooms recorded. Add one, or choose another type."
        />
      ) : (
        <>
          <p className={styles.count} role="status">
            {formatCount(rooms.total)} {rooms.total === 1 ? 'room' : 'rooms'} of this type.
            Counted by the server across every page.
          </p>

          {isCompact ? (
            <RoomCards rooms={rooms.rooms} timeZone={timeZone} />
          ) : (
            <RoomTable rooms={rooms.rooms} timeZone={timeZone} />
          )}

          <Pagination
            page={rooms.page}
            pageSize={rooms.pageSize}
            total={rooms.total}
            pages={rooms.pages}
            onPageChange={rooms.setPage}
            onPageSizeChange={rooms.setPageSize}
            pageSizeOptions={PAGE_SIZE_OPTIONS}
            itemNoun={{ singular: 'room', plural: 'rooms' }}
          />
        </>
      )}
    </Frame>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.fact}>
      <dt className={styles.factLabel}>{label}</dt>
      <dd className={styles.factValue}>{children}</dd>
    </div>
  )
}

/** The page heading, shared by every state so the frame never jumps. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Rooms"
        description="The physical rooms of this property, listed by room type. Status here is a room’s current operational state, not its availability over time."
      />
      <div className={styles.page}>{children}</div>
    </PageContainer>
  )
}
