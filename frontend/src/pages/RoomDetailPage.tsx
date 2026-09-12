import { useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { AlertTriangle, ArrowLeft, Building2, DoorClosed, WifiOff } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { RoomForm } from '@/features/rooms/RoomForm'
import { RoomStatusControl } from '@/features/rooms/RoomStatusControl'
import { useRoomDetail } from '@/features/rooms/useRoomDetail'
import { activePresentation, statusPresentation } from '@/features/rooms/vocabulary'
import { formatDateTime, UNAVAILABLE } from '@/lib/format'
import { ROUTES } from '@/router/routes'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useHotelContext } from '@/session/HotelProvider'

import styles from './RoomDetailPage.module.css'

const LOAD_FAILURE_COPY = {
  notFound: {
    title: 'Room not available',
    detail:
      'No room with that number exists under that room type at this property. The same number under a different type is a different address, and a room removed since the page loaded reads this way too.',
    canRetry: false,
  },
  serverFault: {
    title: 'This room is temporarily unavailable',
    detail: 'The room could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

const UPDATE_FAILURE_COPY = {
  forbidden: {
    title: 'Nothing was saved',
    detail:
      'Changing a room needs the manager role on this property, which this account does not have. Reading it does not — so what is shown is current, and nothing was written.',
    canRetry: false,
  },
  notFound: {
    title: 'Nothing was saved',
    detail: 'This room no longer exists under this room type. It may have been removed since the page was loaded.',
    canRetry: false,
  },
  conflict: {
    title: 'Nothing was saved',
    detail: 'The change conflicts with existing data. The room is unchanged.',
    canRetry: true,
  },
  serverFault: {
    title: 'Nothing was saved',
    detail: 'The change could not be saved. The room is unchanged.',
    canRetry: true,
  },
} as const

const DELETE_FAILURE_COPY = {
  forbidden: {
    title: 'The room was not deleted',
    detail:
      'Deleting a room needs the manager role on this property, which this account does not have. Nothing was removed.',
    canRetry: false,
  },
  notFound: {
    title: 'The room was not deleted',
    detail: 'This room no longer exists under this room type.',
    canRetry: false,
  },
  conflict: {
    title: 'The room was not deleted',
    detail:
      'Existing reservations still refer to this room, and no cascade is performed. Withdrawing it from service keeps the history and takes it out of use; deleting it does not.',
    canRetry: false,
  },
  serverFault: {
    title: 'The room was not deleted',
    detail: 'The room could not be removed. It is unchanged.',
    canRetry: true,
  },
} as const

/**
 * One room: its record, its status, the form that edits it, and its deletion.
 *
 * ## Two segments identify a room, not one
 *
 * `/rooms/{type}/{number}`. The backend resolves a room against the whole chain, and the same
 * number under another type is a 404 -- verified. Both segments are public identifiers; no
 * internal key exists to put in a URL.
 *
 * ## Status is not availability
 *
 * The five states are the room's current operational condition. Whether it can be booked on a
 * given date is decided by reservations and a database exclusion constraint, and this page
 * neither reads nor derives that. Nothing here counts occupancy.
 *
 * ## Deleting is offered, and withdrawing is suggested first
 *
 * `DELETE` exists and needs the manager role. It is refused whenever a reservation references
 * the room -- no cascade is performed -- which makes it the *usual* outcome for a room that
 * has ever been sold. The confirmation says so, and points at `is_active` as the reversible
 * alternative, because taking a room out of service is what is usually meant.
 */
export function RoomDetailPage() {
  const { roomTypeCode, roomNumber } = useParams<{
    roomTypeCode: string
    roomNumber: string
  }>()
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected

  const detail = useRoomDetail({
    hotelPublicId: hotel?.public_id ?? null,
    roomTypeCode,
    roomNumber,
  })
  const [editing, setEditing] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Rooms are held per property, and this account is not yet a member of one."
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

  if (detail.deleted) {
    return (
      <Frame>
        <StateMessage
          icon={DoorClosed}
          tone="status"
          title="Room deleted"
          detail="The room has been removed from this property. Nothing else was changed."
        />
      </Frame>
    )
  }

  const loadFailure = detail.error === null ? null : describeFailure(detail.error, LOAD_FAILURE_COPY)
  const mutationFailure =
    detail.mutationError === null
      ? null
      : describeFailure(
          detail.mutationError,
          detail.failedMutation === 'delete' ? DELETE_FAILURE_COPY : UPDATE_FAILURE_COPY,
        )

  if (loadFailure) {
    return (
      <Frame>
        <StateMessage
          icon={detail.error?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
          tone="alert"
          title={loadFailure.title}
          detail={loadFailure.detail}
          {...(loadFailure.canRetry ? { onRetry: detail.reload } : {})}
        />
      </Frame>
    )
  }

  if (detail.room === null) {
    return (
      <Frame>
        <div className={styles.skeleton} role="status" aria-busy="true">
          <Skeleton height="14rem" label="Loading room" />
        </div>
      </Frame>
    )
  }

  const room = detail.room
  const timeZone = hotel?.timezone ?? 'UTC'
  const status = statusPresentation(room.status)
  const active = activePresentation(room.is_active)

  return (
    <Frame name={`Room ${room.room_number}`}>
      {detail.saved ? (
        <p className={styles.success} role="status">
          {detail.saved} The record below is the server&rsquo;s own copy.
        </p>
      ) : null}

      {mutationFailure ? (
        <div className={styles.failure} role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            <p className={styles.failureTitle}>{mutationFailure.title}</p>
            <p className={styles.failureDetail}>{mutationFailure.detail}</p>
          </div>
        </div>
      ) : null}

      {editing ? (
        <section className={styles.panel} aria-labelledby="room-edit">
          <h2 className={styles.panelTitle} id="room-edit">
            Edit room
          </h2>
          <RoomForm
            room={room}
            roomTypeCode={room.room_type_code}
            busy={detail.pending === 'update'}
            onDirty={detail.dismiss}
            onCancel={() => {
              setEditing(false)
            }}
            onUpdate={(payload) => {
              void detail.update(payload, 'Room updated.').then((accepted) => {
                if (accepted) {
                  setEditing(false)
                }
              })
            }}
          />
        </section>
      ) : (
        <>
          <section className={styles.panel} aria-labelledby="room-record">
            <div className={styles.panelHeader}>
              <h2 className={styles.panelTitle} id="room-record">
                Room record
              </h2>
              <Button
                variant="secondary"
                size="sm"
                disabled={detail.pending !== null}
                onClick={() => {
                  detail.dismiss()
                  setEditing(true)
                }}
              >
                Edit room
              </Button>
            </div>

            <dl className={styles.fields}>
              <Field label="Number">{room.room_number}</Field>
              <Field label="Room type">{room.room_type_code}</Field>
              <Field label="Floor">
                {room.floor === null ? (
                  <span className={styles.absent}>{UNAVAILABLE}</span>
                ) : (
                  room.floor
                )}
              </Field>
              <Field label="Status">
                <Badge tone={status.tone} withDot>
                  {status.label}
                </Badge>
              </Field>
              <Field label="Service">
                <Badge tone={active.tone}>{active.label}</Badge>
              </Field>
              <Field label="Notes">
                {room.notes === null || room.notes === '' ? (
                  <span className={styles.absent}>None</span>
                ) : (
                  /* Free text somebody typed. A React text node, escaped by construction;
                   * `pre-wrap` keeps line breaks and interprets nothing. */
                  <span className={styles.notes}>{room.notes}</span>
                )}
              </Field>
              <Field label="Created">{formatDateTime(room.created_at, timeZone)}</Field>
              <Field label="Last updated">{formatDateTime(room.updated_at, timeZone)}</Field>
            </dl>
          </section>

          <section className={styles.panel} aria-labelledby="room-status">
            <h2 className={styles.panelTitle} id="room-status">
              Status
            </h2>
            <RoomStatusControl
              room={room}
              busy={detail.pending !== null}
              onChange={(next) => {
                detail.dismiss()
                void detail.update(
                  { status: next },
                  `Status set to ${statusPresentation(next).label.toLowerCase()}.`,
                )
              }}
            />
          </section>
        </>
      )}

      <section className={styles.danger} aria-labelledby="room-delete">
        <h2 className={styles.panelTitle} id="room-delete">
          Delete this room
        </h2>
        {confirmingDelete ? (
          <div className={styles.confirm} role="group" aria-label="Confirm deleting this room">
            <p className={styles.confirmQuestion}>
              Permanently delete <strong>room {room.room_number}</strong> ({room.room_type_code})
              from {hotel?.name ?? 'this property'}? This cannot be undone.
            </p>
            <div className={styles.actions}>
              <Button
                autoFocus
                variant="danger"
                size="sm"
                disabled={detail.pending !== null}
                onClick={() => {
                  setConfirmingDelete(false)
                  void detail.remove()
                }}
              >
                {detail.pending === 'delete' ? 'Deleting…' : 'Yes, delete this room'}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={detail.pending !== null}
                onClick={() => {
                  setConfirmingDelete(false)
                }}
              >
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <>
            <p className={styles.dangerNote}>
              The database refuses this while any reservation still refers to the room, and no
              cascade is performed &mdash; so a room that has ever been sold cannot be deleted.
              Withdrawing it from service takes it out of use and keeps its history.
            </p>
            <Button
              variant="danger"
              size="sm"
              disabled={detail.pending !== null}
              onClick={() => {
                detail.dismiss()
                setConfirmingDelete(true)
              }}
            >
              Delete room
            </Button>
          </>
        )}
      </section>
    </Frame>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.field}>
      <dt className={styles.fieldLabel}>{label}</dt>
      <dd className={styles.fieldValue}>{children}</dd>
    </div>
  )
}

/** The page heading and the way back, shared by every state so the frame never jumps. */
function Frame({ children, name }: { children: ReactNode; name?: string }) {
  return (
    <PageContainer>
      <Link className={styles.back} to={ROUTES.rooms}>
        <ArrowLeft size={15} aria-hidden="true" />
        All rooms
      </Link>
      <SectionHeader
        as="h1"
        title={name ?? 'Room'}
        description="One physical room at this property."
      />
      <div className={styles.page}>{children}</div>
    </PageContainer>
  )
}
