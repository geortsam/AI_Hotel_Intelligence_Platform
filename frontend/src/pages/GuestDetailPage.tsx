import { useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { AlertTriangle, ArrowLeft, Building2, WifiOff } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { GuestForm } from '@/features/guests/GuestForm'
import { useGuestDetail } from '@/features/guests/useGuestDetail'
import { formatDate, formatDateTime } from '@/lib/format'
import { ROUTES } from '@/router/routes'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useHotelContext } from '@/session/HotelProvider'

import styles from './GuestDetailPage.module.css'

const LOAD_FAILURE_COPY = {
  notFound: {
    title: 'Guest not available',
    detail:
      'No guest with that identifier belongs to this property. They may have been deleted, or the record may belong to another hotel — guests are held per property, and an identifier from one is not valid at another.',
    canRetry: false,
  },
  serverFault: {
    title: 'This guest is temporarily unavailable',
    detail: 'The record could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

const UPDATE_FAILURE_COPY = {
  forbidden: {
    title: 'Nothing was saved',
    detail:
      'Editing a guest needs the staff role on this property, which this account does not have. Reading the record does not — so what is shown is current, and nothing was written.',
    canRetry: false,
  },
  notFound: {
    title: 'Nothing was saved',
    detail: 'This guest no longer exists at this property. They may have been deleted since this page was loaded.',
    canRetry: false,
  },
  conflict: {
    title: 'Nothing was saved',
    detail:
      'Another guest at this property already uses that email address. An address belongs to at most one guest here.',
    canRetry: false,
  },
  serverFault: {
    title: 'Nothing was saved',
    detail: 'The change could not be saved. The record is unchanged.',
    canRetry: true,
  },
} as const

const DELETE_FAILURE_COPY = {
  forbidden: {
    title: 'The guest was not deleted',
    detail:
      'Deleting a guest needs the manager role on this property — a higher permission than editing one, which this account may well have. Nothing was removed.',
    canRetry: false,
  },
  notFound: {
    title: 'The guest was not deleted',
    detail: 'This guest no longer exists at this property.',
    canRetry: false,
  },
  conflict: {
    title: 'The guest was not deleted',
    detail:
      'Existing reservations or reviews still refer to this guest, and the database will not remove a record those depend on. Remove or reassign them first. Nothing was changed.',
    canRetry: false,
  },
  serverFault: {
    title: 'The guest was not deleted',
    detail: 'The record could not be removed. It is unchanged.',
    canRetry: true,
  },
} as const

/**
 * One guest: their record, the form that edits it, and the one destructive action in this
 * application.
 *
 * ## The URL is a public identifier, and there is no other kind
 *
 * `/guests/{public_id}` — a UUID. `GuestResponse` carries no internal key, so there is no
 * numeric id available to put in a route even by accident, and the backend resolves the guest
 * by **hotel and public id together**. A guest of another property answers 404 through this
 * hotel's URL, verified live; the copy therefore says "not available here" rather than
 * "deleted", because the client cannot tell those apart and should not pretend to.
 *
 * ## Deleting asks first, and says what will stop it
 *
 * This is the only hard delete the platform exposes. It needs the **manager** role — higher
 * than the staff role that edits — and the database refuses it whenever a reservation or a
 * review still refers to the guest. That refusal is not a rare edge: every seeded guest has
 * bookings, so it is the *usual* outcome, and the confirmation says so before the click
 * rather than after it.
 *
 * Nothing is removed from the screen before the server returns 204. On success the page keeps
 * standing and says the record is gone, instead of navigating away from underneath the person
 * who just acted.
 */
export function GuestDetailPage() {
  const { guestPublicId } = useParams<{ guestPublicId: string }>()
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected

  const detail = useGuestDetail({
    hotelPublicId: hotel?.public_id ?? null,
    guestPublicId,
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
          detail="Guests are held per property, and this account is not yet a member of one."
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
          icon={Building2}
          tone="status"
          title="Guest deleted"
          detail="The record has been removed from this property. Nothing else was changed."
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

  if (detail.guest === null) {
    return (
      <Frame>
        <div className={styles.skeleton} role="status" aria-busy="true">
          <Skeleton height="14rem" label="Loading guest" />
        </div>
      </Frame>
    )
  }

  const guest = detail.guest
  const timeZone = hotel?.timezone ?? 'UTC'

  return (
    <Frame name={`${guest.first_name} ${guest.last_name}`}>
      {detail.saved ? (
        <p className={styles.success} role="status">
          {detail.saved} The record below is the server&rsquo;s own copy, not what was typed.
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
        <section className={styles.panel} aria-labelledby="guest-edit">
          <h2 className={styles.panelTitle} id="guest-edit">
            Edit guest
          </h2>
          <GuestForm
            guest={guest}
            busy={detail.pending === 'update'}
            onDirty={detail.dismiss}
            onCancel={() => {
              setEditing(false)
            }}
            onUpdate={(payload) => {
              void detail.update(payload).then((accepted) => {
                if (accepted) {
                  setEditing(false)
                }
              })
            }}
          />
        </section>
      ) : (
        <section className={styles.panel} aria-labelledby="guest-record">
          <div className={styles.panelHeader}>
            <h2 className={styles.panelTitle} id="guest-record">
              Guest record
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
              Edit guest
            </Button>
          </div>

          <dl className={styles.fields}>
            <Field label="Name">
              {guest.first_name} {guest.last_name}
            </Field>
            <Field label="Email">
              {guest.email ?? <span className={styles.absent}>Not recorded</span>}
            </Field>
            <Field label="Phone">
              {guest.phone ?? <span className={styles.absent}>Not recorded</span>}
            </Field>
            <Field label="Country">
              {guest.country_code ?? <span className={styles.absent}>Not recorded</span>}
            </Field>
            <Field label="Preferred language">
              {guest.preferred_language ?? <span className={styles.absent}>Not recorded</span>}
            </Field>
            <Field label="Date of birth">
              {guest.date_of_birth === null ? (
                <span className={styles.absent}>Not recorded</span>
              ) : (
                formatDate(guest.date_of_birth)
              )}
            </Field>
            <Field label="Marketing">
              <Badge tone={guest.marketing_opt_in ? 'success' : 'neutral'}>
                {guest.marketing_opt_in ? 'Opted in' : 'No consent'}
              </Badge>
            </Field>
            <Field label="Notes">
              {guest.notes === null || guest.notes === '' ? (
                <span className={styles.absent}>None</span>
              ) : (
                /* Free text somebody typed. A React text node, so it is escaped by
                 * construction; `pre-wrap` keeps the line breaks and interprets nothing. */
                <span className={styles.notes}>{guest.notes}</span>
              )}
            </Field>
            <Field label="Created">{formatDateTime(guest.created_at, timeZone)}</Field>
            <Field label="Last updated">{formatDateTime(guest.updated_at, timeZone)}</Field>
          </dl>
        </section>
      )}

      <section className={styles.danger} aria-labelledby="guest-delete">
        <h2 className={styles.panelTitle} id="guest-delete">
          Delete this guest
        </h2>
        {confirmingDelete ? (
          <div className={styles.confirm} role="group" aria-label="Confirm deleting this guest">
            <p className={styles.confirmQuestion}>
              Permanently delete <strong>{guest.first_name} {guest.last_name}</strong> from{' '}
              {hotel?.name ?? 'this property'}? This cannot be undone, and there is no archive
              to restore from.
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
                {detail.pending === 'delete' ? 'Deleting…' : 'Yes, delete this guest'}
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
              The database refuses this while any reservation or review still refers to the
              guest, and deleting needs the manager role. Neither is checked here &mdash; the
              server decides, and says so if it refuses.
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
              Delete guest
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
      <Link className={styles.back} to={ROUTES.guests}>
        <ArrowLeft size={15} aria-hidden="true" />
        All guests
      </Link>
      <SectionHeader
        as="h1"
        title={name ?? 'Guest'}
        description="One guest record at this property."
      />
      <div className={styles.page}>{children}</div>
    </PageContainer>
  )
}
