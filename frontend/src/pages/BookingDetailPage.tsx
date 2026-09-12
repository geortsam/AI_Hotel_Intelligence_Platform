import { useCallback, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  AlertTriangle,
  ArrowLeft,
  Building2,
  CalendarClock,
  CheckCircle2,
  Info,
  PencilLine,
  Plus,
} from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardBody, CardHeader } from '@/components/ui/Card'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { BookingStatusBadge } from '@/features/bookings/BookingStatusBadge'
import { RepricingNotice } from '@/features/bookings/RepricingNotice'
import { StatusActions } from '@/features/bookings/StatusActions'
import { StayEditor } from '@/features/bookings/StayEditor'
import { StayExtensionForm } from '@/features/bookings/StayExtensionForm'
import { canExtendStay, canModifyStay } from '@/features/bookings/transitions'
import { paymentStatePresentation, sourceLabel } from '@/features/bookings/vocabulary'
import { useBookingDetail } from '@/features/bookings/useBookingDetail'
import { useBookingMutations } from '@/features/bookings/useBookingMutations'
import { ChargeForm } from '@/features/payments/ChargeForm'
import { PaymentLedger } from '@/features/payments/PaymentLedger'
import { RefundForm } from '@/features/payments/RefundForm'
import { usePayments } from '@/features/payments/usePayments'
import { StateMessage } from '@/features/dashboard/StateMessage'
import {
  formatCount,
  formatDate,
  formatDateTime,
  formatMoney,
  maskEmail,
  maskPhone,
  UNAVAILABLE,
} from '@/lib/format'
import type { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { ROUTES } from '@/router/routes'
import { useHotelContext } from '@/session/HotelProvider'
import type {
  Booking,
  BookingReconciliation,
  BookingStatus,
  StayExtensionRequest,
  StayModificationRequest,
} from '@/types/booking'
import type { Guest } from '@/types/guest'
import type { Payment } from '@/types/payment'

import styles from './BookingDetailPage.module.css'

/**
 * One booking, in operational detail.
 *
 * ## The financial section is the point of this page
 *
 * A booking carries `total_amount`, and it is **not** what the stay is worth. Approved
 * decision 10 makes it the *contracted* figure, supplied by the client and deliberately not
 * defined as the sum of the nightly rates -- discounts, taxes and packages legitimately break
 * that equality. The authoritative amount comes from
 * `GET /bookings/{id}/reconciliation`, which reports both figures and whether they agree,
 * because the schema records no tax, fee or discount and so the server can report a
 * divergence but never explain one.
 *
 * This page reproduces that distinction exactly: `accommodation_total` is labelled as what
 * the server can prove, `declared_total` as what was contracted, and a disagreement is shown
 * rather than smoothed over. **Nothing here adds up nightly rates**, even though it has
 * them -- that sum is the backend's to state, and a second implementation of it in React is
 * how a page starts disagreeing with the ledger it is summarising.
 *
 * ## Sections exist only where data does
 *
 * The cancellation panel appears only for a cancelled booking. The guest panel says it is
 * unavailable if that request failed, rather than rendering an empty person. The financial
 * panel does the same. A section that failed to load must never look like one that came
 * back empty.
 */
export function BookingDetailPage() {
  const { bookingPublicId } = useParams<{ bookingPublicId: string }>()
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected
  const { status, data, error, retry, applyBooking, refreshReconciliation, refreshBooking } =
    useBookingDetail(hotel?.public_id ?? null, bookingPublicId)

  const mutations = useBookingMutations({
    hotelPublicId: hotel?.public_id ?? null,
    bookingPublicId,
    onBooking: applyBooking,
    onStayChanged: refreshReconciliation,
    onStayReplaced: refreshBooking,
  })

  /*
   * A posting changes what the booking owes, and every one of those figures is the server's.
   * So the ledger's success path re-reads reconciliation rather than adjusting a total here.
   */
  const payments = usePayments({
    hotelPublicId: hotel?.public_id ?? null,
    bookingPublicId,
    onPosted: refreshReconciliation,
  })

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Bookings are held per property, and this account is not yet a member of one."
        />
      </Frame>
    )
  }

  if (status === 'error' && error !== null) {
    const notice = describeFailure(error, {
      notFound: {
        title: 'Booking not found',
        detail:
          'No booking with this reference exists for the selected hotel, or your access to it has been removed.',
        canRetry: false,
      },
      serverFault: {
        title: 'This booking is temporarily unavailable',
        detail: 'The booking could not be loaded. Try again shortly.',
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

  if (status !== 'ready' || data === null) {
    return (
      <Frame>
        <div className={styles.loading} role="status" aria-busy="true">
          <Skeleton width="30%" height="1.5rem" />
          <Skeleton width="55%" height="1rem" />
          <Skeleton width="100%" height="8rem" />
        </div>
      </Frame>
    )
  }

  const { booking, reconciliation, guest } = data
  const timeZone = hotel?.timezone ?? 'UTC'

  return (
    <Frame reference={booking.reference} status={booking.status}>
      <div className={styles.sections}>
        <OperationsSection booking={booking} mutations={mutations} />
        <OverviewSection booking={booking} timeZone={timeZone} />
        <StaySection booking={booking} />
        <GuestSection guest={guest} />
        <RoomsSection booking={booking} />
        <FinancialSection
          booking={booking}
          reconciliation={reconciliation}
          reconciliationError={data.reconciliationError}
        />
        <PaymentsSection booking={booking} payments={payments} timeZone={timeZone} />
        {booking.cancelled_at !== null ? (
          <CancellationSection booking={booking} timeZone={timeZone} />
        ) : null}
        <MetadataSection booking={booking} timeZone={timeZone} />
      </div>
    </Frame>
  )
}

/* -------------------------------------------------------------------------------------- */

interface FrameProps {
  readonly reference?: string
  readonly status?: Booking['status']
  readonly children: ReactNode
}

/**
 * The page frame: a way back, the reference, and the status.
 *
 * The back link is a real `<Link>` to the list, not `history.back()`. A booking opened from
 * a bookmark or a pasted URL has nothing to go back to, and a control that sometimes leaves
 * the application is worse than one that always goes to the same, correct place.
 */
function Frame({ reference, status, children }: FrameProps) {
  return (
    <PageContainer>
      <Link className={styles.back} to={ROUTES.bookings}>
        <ArrowLeft size={15} aria-hidden="true" />
        All bookings
      </Link>

      <SectionHeader
        as="h2"
        title={reference ? `Booking ${reference}` : 'Booking'}
        description="Stay, rooms, guest and financial position for this reservation."
        actions={status ? <BookingStatusBadge status={status} /> : undefined}
      />

      {children}
    </PageContainer>
  )
}

/** A labelled fact list. The building block every section below is made of. */
function Facts({ children }: { readonly children: ReactNode }) {
  return <dl className={styles.facts}>{children}</dl>
}

function Fact({ label, children }: { readonly label: string; readonly children: ReactNode }) {
  return (
    <div className={styles.fact}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

/**
 * The operations panel: what may be done to this booking, and what happened when it was.
 *
 * ## Nothing on screen changes before the server answers
 *
 * Every control here submits and waits. The page then adopts the booking the **server
 * returned** -- all three endpoints answer with the booking as it now stands -- rather than
 * patching the local copy with what was asked for. An optimistic update would show a
 * confirmation the server might refuse, and a booking status is precisely the kind of claim
 * an operations console must never make on the server's behalf.
 *
 * ## Which panels appear is the backend's decision, not a guess
 *
 * `canModifyStay` and `canExtendStay` come from the backend's own status sets. A checked-in
 * booking gets the extension form and not the stay editor; a pending or confirmed one gets
 * the editor and not the extension. Both were verified live: the wrong operation on the
 * wrong status is a 409 either way, and this offers the one that works.
 *
 * ## Authorization is not decided here
 *
 * The mutations need `HotelRole.STAFF`, and **this application cannot discover the caller's
 * role**: `/auth/me` returns identity only, `GET /hotels` carries no role, and
 * `GET /hotels/{h}/members` -- the one endpoint that would say -- requires `MANAGER`, so a
 * staff user cannot read it either. Hiding the controls would therefore mean guessing. They
 * are shown to every member and a 403 is rendered plainly when the server refuses, which is
 * the honest arrangement: the server was always the authority, and now the interface admits
 * it. The gap is named in the report.
 */
function OperationsSection({
  booking,
  mutations,
}: {
  booking: Booking
  mutations: ReturnType<typeof useBookingMutations>
}) {
  const [editing, setEditing] = useState<'stay' | 'extension' | null>(null)
  const { pending, outcome, error, dismiss } = mutations
  const busy = pending !== null

  // The target being applied, so only the pressed button says "Working…".
  const [runningTarget, setRunningTarget] = useState<BookingStatus | null>(null)

  const changeStatus = useCallback(
    (target: BookingStatus, reason?: string) => {
      setRunningTarget(target)
      void mutations.changeStatus(target, reason).finally(() => {
        setRunningTarget(null)
      })
    },
    [mutations],
  )

  const submitStay = useCallback(
    (payload: StayModificationRequest) => {
      void mutations.modifyStay(payload).then(() => {
        setEditing((current) => (current === 'stay' ? null : current))
      })
    },
    [mutations],
  )

  const submitExtension = useCallback(
    (payload: StayExtensionRequest) => {
      void mutations.extendStay(payload).then(() => {
        setEditing((current) => (current === 'extension' ? null : current))
      })
    },
    [mutations],
  )

  const failure = error === null ? null : describeFailure(error, MUTATION_FAILURE_COPY)

  return (
    <Card padded={false}>
      <CardHeader title="Operations" />
      <CardBody>
        {/*
         * Success and failure are separate live regions with different urgency. A completed
         * action is `status` (polite); a refusal is `alert` (assertive), because it means the
         * thing the user asked for did not happen and they may be about to act as though it did.
         */}
        {outcome ? (
          <p className={styles.success} role="status">
            <CheckCircle2 size={16} aria-hidden="true" />
            <span>{outcome.message}</span>
            <button type="button" className={styles.dismiss} onClick={dismiss}>
              Dismiss
            </button>
          </p>
        ) : null}

        {failure ? (
          <div className={styles.failure} role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            <div>
              <strong>{failure.title}</strong>
              <p className={styles.failureDetail}>{failure.detail}</p>
            </div>
          </div>
        ) : null}

        <StatusActions
          current={booking.status}
          onChange={changeStatus}
          busy={busy}
          runningTarget={runningTarget}
        />

        {outcome?.repricing ? <RepricingNotice repricing={outcome.repricing} /> : null}

        {canModifyStay(booking.status) || canExtendStay(booking.status) ? (
          <div className={styles.stayOps}>
            {editing === null ? (
              <div className={styles.stayOpsActions}>
                {canModifyStay(booking.status) ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      dismiss()
                      setEditing('stay')
                    }}
                  >
                    <PencilLine size={14} aria-hidden="true" />
                    Change stay dates
                  </Button>
                ) : null}
                {canExtendStay(booking.status) ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      dismiss()
                      setEditing('extension')
                    }}
                  >
                    <CalendarClock size={14} aria-hidden="true" />
                    Extend stay
                  </Button>
                ) : null}
              </div>
            ) : editing === 'stay' ? (
              <StayEditor
                booking={booking}
                busy={busy}
                onSubmit={submitStay}
                onCancel={() => {
                  setEditing(null)
                }}
              />
            ) : (
              <StayExtensionForm
                booking={booking}
                busy={busy}
                onSubmit={submitExtension}
                onCancel={() => {
                  setEditing(null)
                }}
              />
            )}
          </div>
        ) : null}
      </CardBody>
    </Card>
  )
}

/**
 * What a refused mutation says.
 *
 * The 409 copy is the important one. It covers three genuinely different refusals the API
 * answers identically -- an illegal lifecycle move, a booking whose status changed under the
 * caller, and a room taken by someone else -- so it names what to do rather than guessing
 * which happened. The backend's own message is never rendered; it is accurate here, but the
 * rule that no server text reaches the DOM is what makes that true of a proxy's message too.
 */
const MUTATION_FAILURE_COPY = {
  notFound: {
    title: 'Booking not found',
    detail:
      'No booking with this reference exists for the selected hotel, or your access to it has been removed.',
    canRetry: false,
  },
  conflict: {
    title: 'This booking changed before the action completed',
    detail:
      'The booking may have moved to another status, or a room may have been taken for these dates. Reload the booking and try again.',
    canRetry: false,
  },
  serverFault: {
    title: 'The action could not be completed',
    detail: 'The request did not go through. Reload the booking and try again shortly.',
    canRetry: false,
  },
} as const

function OverviewSection({ booking, timeZone }: { booking: Booking; timeZone: string }) {
  return (
    <Card padded={false}>
      <CardHeader title="Overview" />
      <CardBody>
        <Facts>
          <Fact label="Reference">
            <span className={styles.mono}>{booking.reference}</span>
          </Fact>
          <Fact label="Status">
            <BookingStatusBadge status={booking.status} />
          </Fact>
          <Fact label="Source">{sourceLabel(booking.source)}</Fact>
          <Fact label="Channel reference">
            {booking.channel_reference ? (
              <span className={styles.mono}>{booking.channel_reference}</span>
            ) : (
              <Muted>Not supplied</Muted>
            )}
          </Fact>
          <Fact label="Booked">{formatDateTime(booking.booked_at, timeZone)}</Fact>
        </Facts>
      </CardBody>
    </Card>
  )
}

function StaySection({ booking }: { booking: Booking }) {
  // From the allocation's generated `nights` column, not computed from the two dates.
  const nights = booking.rooms[0]?.nights ?? null

  return (
    <Card padded={false}>
      <CardHeader title="Stay" />
      <CardBody>
        <Facts>
          <Fact label="Check-in">{formatDate(booking.check_in_date)}</Fact>
          <Fact label="Check-out">{formatDate(booking.check_out_date)}</Fact>
          <Fact label="Nights">
            {nights === null ? (
              <Muted>{UNAVAILABLE}</Muted>
            ) : (
              `${formatCount(nights)} night${nights === 1 ? '' : 's'}`
            )}
          </Fact>
          <Fact label="Occupancy">
            {formatCount(booking.adults)} adult{booking.adults === 1 ? '' : 's'}
            {booking.children > 0
              ? `, ${formatCount(booking.children)} child${booking.children === 1 ? '' : 'ren'}`
              : ''}
          </Fact>
          <Fact label="Special requests">
            {booking.special_requests ? booking.special_requests : <Muted>None</Muted>}
          </Fact>
        </Facts>
        <p className={styles.note}>
          The departure day is not a room night: a stay from {formatDate(booking.check_in_date)}{' '}
          to {formatDate(booking.check_out_date)} is billed for the nights before check-out.
        </p>
      </CardBody>
    </Card>
  )
}

/**
 * The booker.
 *
 * **Only what the desk needs to confirm identity.** The endpoint also returns a date of
 * birth and free-text notes; neither is rendered. A booking screen is read over the
 * receptionist's shoulder by whoever is standing at the desk, and a date of birth identifies
 * a person far beyond answering "is this the right reservation?".
 *
 * Email and phone are masked for the same reason. The domain and the last four digits are
 * what actually distinguish two guests with the same name; the rest is the guest's.
 */
function GuestSection({ guest }: { guest: Guest | null }) {
  return (
    <Card padded={false}>
      <CardHeader title="Guest" />
      <CardBody>
        {guest === null ? (
          <p className={styles.unavailable} role="status">
            The guest record for this booking could not be loaded. The booking details above
            are unaffected.
          </p>
        ) : (
          <>
            <Facts>
              <Fact label="Name">
                {guest.first_name} {guest.last_name}
              </Fact>
              <Fact label="Email">
                {guest.email ? maskEmail(guest.email) : <Muted>Not supplied</Muted>}
              </Fact>
              <Fact label="Phone">
                {guest.phone ? maskPhone(guest.phone) : <Muted>Not supplied</Muted>}
              </Fact>
              <Fact label="Country">
                {guest.country_code ?? <Muted>Not supplied</Muted>}
              </Fact>
              <Fact label="Language">
                {guest.preferred_language ?? <Muted>Not supplied</Muted>}
              </Fact>
              <Fact label="Marketing">
                {guest.marketing_opt_in ? 'Opted in' : 'Not opted in'}
              </Fact>
            </Facts>
            <p className={styles.note}>
              Contact details are shown partially. This is the account the booking was made
              under; the person in each room is named on its allocation above where one was
              given.
            </p>
          </>
        )}
      </CardBody>
    </Card>
  )
}

/**
 * The allocated rooms and their priced nights.
 *
 * `booking_rooms` is an **allocation**, not a pricing source -- the rates hang off the nights
 * beneath it, which is why each room lists its own nights rather than showing a room-level
 * "price". A complimentary night is flagged rather than hidden: it is occupied but not sold,
 * so it counts toward occupancy and not toward ADR, and an operator reading a zero needs to
 * know which of the two it is.
 */
function RoomsSection({ booking }: { booking: Booking }) {
  return (
    <Card padded={false}>
      <CardHeader
        title={`Rooms (${formatCount(booking.rooms.length)})`}
        actions={<Badge tone="neutral">{booking.currency}</Badge>}
      />
      <CardBody>
        {booking.rooms.length === 0 ? (
          <p className={styles.unavailable}>This booking has no room allocations.</p>
        ) : (
          booking.rooms.map((room) => (
            <section key={room.room_number} className={styles.room}>
              <h4 className={styles.roomTitle}>
                Room {room.room_number}
                <span className={styles.roomType}>{room.room_type_code}</span>
              </h4>
              <Facts>
                <Fact label="Occupancy">
                  {formatCount(room.adults)} adult{room.adults === 1 ? '' : 's'}
                  {room.children > 0
                    ? `, ${formatCount(room.children)} child${room.children === 1 ? '' : 'ren'}`
                    : ''}
                </Fact>
                <Fact label="Occupant">
                  {room.guest_name ? room.guest_name : <Muted>Not named</Muted>}
                </Fact>
                <Fact label="Nights">{formatCount(room.nights)}</Fact>
              </Facts>

              <div className={styles.tableScroll}>
                <table className={styles.nightsTable}>
                  <caption className={styles.tableCaption}>
                    Nightly rates for room {room.room_number}, in {booking.currency}. These are
                    the figures the server sums to reach the accommodation total.
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Night</th>
                      <th scope="col">Rate plan</th>
                      <th scope="col" className={styles.numeric}>
                        Rate
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {room.nightly_rates.map((night) => (
                      <tr key={night.stay_date}>
                        <th scope="row">{formatDate(night.stay_date)}</th>
                        <td>
                          {night.rate_plan_code ?? <Muted>Standard</Muted>}
                          {night.is_complimentary ? (
                            <Badge tone="info">Complimentary</Badge>
                          ) : null}
                        </td>
                        <td className={styles.numeric}>
                          {formatMoney(night.rate, booking.currency)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ))
        )}
      </CardBody>
    </Card>
  )
}

/**
 * What the stay is worth, what has been paid, and what remains.
 *
 * Every figure is the reconciliation endpoint's. The two totals are shown side by side and
 * labelled for what they are, exactly as the backend defines them, because the difference
 * between them is a finding rather than a rounding error.
 */
function FinancialSection({
  booking,
  reconciliation,
  reconciliationError,
}: {
  booking: Booking
  reconciliation: BookingReconciliation | null
  reconciliationError: ApiError | null
}) {
  if (reconciliation === null) {
    /*
     * A 409 here is not a fault and will not clear on its own. The server refuses to
     * reconcile a booking whose payments are not all in its own currency, because summing
     * them would produce a number that is not money and it performs no conversion. That is
     * worth explaining precisely -- an operator who reads "temporarily unavailable" will
     * wait for something that is never coming.
     */
    const mixedCurrency = reconciliationError?.status === 409
    return (
      <Card padded={false}>
        <CardHeader title="Financial summary" />
        <CardBody>
          <p className={styles.unavailable} role="status">
            {mixedCurrency
              ? 'This booking cannot be reconciled: it holds payments in more than one currency, and no conversion is performed. The payments below are listed individually, each in its own currency.'
              : 'The financial summary could not be loaded. It is derived by the server from the nightly rates and the payment ledger; no figure is shown here rather than an estimate.'}
          </p>
          <p className={styles.note}>
            The contracted total recorded on this booking is{' '}
            {formatMoney(booking.total_amount, booking.currency)}. That is the figure supplied
            when the booking was taken, not what the server can prove the stay is worth.
          </p>
        </CardBody>
      </Card>
    )
  }

  const payment = paymentStatePresentation(reconciliation.payment_state)
  const outstanding = Number(reconciliation.outstanding_amount)

  return (
    <Card padded={false}>
      <CardHeader
        title="Financial summary"
        actions={<Badge tone={payment.tone}>{payment.label}</Badge>}
      />
      <CardBody>
        <Facts>
          <Fact label="Accommodation total">
            <span className={styles.figure}>
              {formatMoney(reconciliation.accommodation_total, reconciliation.currency)}
            </span>
            <span className={styles.factNote}>
              Server-authoritative &mdash; the sum of this booking&rsquo;s nightly rates.
            </span>
          </Fact>
          <Fact label="Contracted total">
            {formatMoney(reconciliation.declared_total, reconciliation.currency)}
            <span className={styles.factNote}>
              What was recorded when the booking was taken. Not authoritative.
            </span>
          </Fact>
          <Fact label="Charged">
            {formatMoney(reconciliation.charged_total, reconciliation.currency)}
          </Fact>
          <Fact label="Refunded">
            {formatMoney(reconciliation.refunded_total, reconciliation.currency)}
          </Fact>
          <Fact label="Net paid">
            {formatMoney(reconciliation.net_paid, reconciliation.currency)}
          </Fact>
          <Fact label={outstanding < 0 ? 'Overpaid by' : 'Outstanding'}>
            <span className={styles.figure}>
              {formatMoney(reconciliation.outstanding_amount, reconciliation.currency)}
            </span>
          </Fact>
        </Facts>

        {!reconciliation.totals_agree ? (
          <p className={styles.divergence} role="status">
            <Info size={15} aria-hidden="true" />
            <span>
              The contracted total and the accommodation total disagree. The server reports
              the difference but cannot explain it: the schema records no tax, fee or discount,
              and any of those legitimately breaks the equality.
            </span>
          </p>
        ) : null}
      </CardBody>
    </Card>
  )
}

/**
 * The payment ledger for this booking, and the two postings that can be added to it.
 *
 * ## Where the money figures come from
 *
 * Not from here. This section lists postings and records new ones; every **total** on the
 * page -- charged, refunded, net paid, outstanding, payment state -- is read from
 * `GET .../reconciliation` and rendered by `FinancialSection` above. Nothing in this
 * component adds up a column, and after a successful posting it re-reads that endpoint rather
 * than adjusting a number locally.
 *
 * ## Why the ledger is inside the booking
 *
 * Because that is the whole of the API. Every payment route lives under
 * `/hotels/{h}/bookings/{b}/payments`; there is no hotel-wide payment listing, no search and
 * no filter. A payments dashboard would be an interface with no endpoint behind it.
 *
 * ## What is deliberately absent
 *
 * No edit and no delete. Payments are append-only -- the router exposes neither verb and its
 * own docstring says neither should be added, because a mistake is corrected by posting a
 * reversal. The UI mirrors that rather than hiding a control that would 405.
 */
function PaymentsSection({
  booking,
  payments,
  timeZone,
}: {
  booking: Booking
  payments: ReturnType<typeof usePayments>
  timeZone: string
}) {
  const [composing, setComposing] = useState<'charge' | null>(null)
  const [refunding, setRefunding] = useState<Payment | null>(null)
  const busy = payments.pending !== null

  const failure =
    payments.postError === null ? null : describeFailure(payments.postError, POSTING_FAILURE_COPY)
  const listFailure =
    payments.error === null ? null : describeFailure(payments.error, LEDGER_FAILURE_COPY)

  return (
    <Card padded={false}>
      <CardHeader
        title="Payments"
        actions={
          payments.status === 'ready' ? (
            <Badge tone="neutral">
              {formatCount(payments.total)} posting{payments.total === 1 ? '' : 's'}
            </Badge>
          ) : undefined
        }
      />
      <CardBody>
        {payments.posted ? (
          <p className={styles.success} role="status">
            <CheckCircle2 size={16} aria-hidden="true" />
            <span>
              {payments.posted.message}{' '}
              {formatMoney(payments.posted.payment.amount, payments.posted.payment.currency)}{' '}
              recorded against this booking.
            </span>
            <button type="button" className={styles.dismiss} onClick={payments.dismiss}>
              Dismiss
            </button>
          </p>
        ) : null}

        {failure ? (
          <div className={styles.failure} role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            <div>
              <strong>{failure.title}</strong>
              <p className={styles.failureDetail}>{failure.detail}</p>
            </div>
          </div>
        ) : null}

        {payments.status === 'loading' || payments.status === 'idle' ? (
          <PaymentLedger payments={[]} timeZone={timeZone} isLoading />
        ) : listFailure ? (
          /* A failed ledger read is never shown as an empty one: an operator who sees "no
           * payments" for a booking that has them may take money twice. */
          <div className={styles.failure} role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            <div>
              <strong>{listFailure.title}</strong>
              <p className={styles.failureDetail}>{listFailure.detail}</p>
            </div>
          </div>
        ) : payments.payments.length === 0 ? (
          <p className={styles.emptyLedger} role="status">
            No payments have been recorded against this booking yet.
          </p>
        ) : (
          <PaymentLedger
            payments={payments.payments}
            timeZone={timeZone}
            busy={busy}
            onRefund={(payment) => {
              payments.dismiss()
              setComposing(null)
              setRefunding(payment)
            }}
          />
        )}

        {payments.pages > 1 ? (
          <p className={styles.ledgerPaging} role="status">
            Showing page {formatCount(payments.page)} of {formatCount(payments.pages)}.{' '}
            <button
              type="button"
              className={styles.pageLink}
              disabled={payments.page <= 1 || busy}
              onClick={() => {
                payments.setPage(payments.page - 1)
              }}
            >
              Previous
            </button>
            <button
              type="button"
              className={styles.pageLink}
              disabled={payments.page >= payments.pages || busy}
              onClick={() => {
                payments.setPage(payments.page + 1)
              }}
            >
              Next
            </button>
          </p>
        ) : null}

        <div className={styles.postingArea}>
          {refunding !== null ? (
            <RefundForm
              parent={refunding}
              timeZone={timeZone}
              busy={busy}
              onDirty={payments.dismiss}
              onCancel={() => {
                setRefunding(null)
              }}
              onSubmit={(payload) => {
                void payments.refund(payload).then((accepted) => {
                  if (accepted) {
                    setRefunding(null)
                  }
                })
              }}
            />
          ) : composing === 'charge' ? (
            <ChargeForm
              currency={booking.currency}
              busy={busy}
              onDirty={payments.dismiss}
              onCancel={() => {
                setComposing(null)
              }}
              onSubmit={(payload) => {
                void payments.charge(payload).then((accepted) => {
                  if (accepted) {
                    setComposing(null)
                  }
                })
              }}
            />
          ) : (
            <Button
              variant="secondary"
              size="sm"
              disabled={busy || payments.status === 'error'}
              onClick={() => {
                payments.dismiss()
                setComposing('charge')
              }}
            >
              <Plus size={14} aria-hidden="true" />
              Record a charge
            </Button>
          )}
        </div>
      </CardBody>
    </Card>
  )
}

/**
 * What a refused posting says.
 *
 * The 409 copy carries the weight. The API answers five genuinely different refusals with
 * that one status -- a duplicate provider reference, a refund exceeding what remains, a
 * refund of a refund, a currency mismatch, and a parent that moved no money -- and this
 * cannot tell which. So the wording states the only thing true of all five: **nothing was
 * recorded**. On a financial screen, a message that leaves it ambiguous whether money moved
 * is worse than one that says less.
 */
const POSTING_FAILURE_COPY = {
  notFound: {
    title: 'Not found',
    detail:
      'This booking, or the payment being refunded, is not available. Reload the booking and try again.',
    canRetry: false,
  },
  conflict: {
    title: 'The posting was refused, and nothing was recorded',
    detail:
      'It may duplicate a transaction reference already on file, exceed what is still refundable on that payment, or reverse a payment that moved no money. Reload the payments and check before trying again.',
    canRetry: false,
  },
  serverFault: {
    title: 'The posting could not be completed',
    detail: 'Nothing was recorded. Reload the payments and try again shortly.',
    canRetry: false,
  },
} as const

/** What a failed ledger READ says. Distinct from a refused posting: nothing was attempted. */
const LEDGER_FAILURE_COPY = {
  notFound: {
    title: 'Payments not available',
    detail: 'This booking could not be found, or your access to it has been removed.',
    canRetry: false,
  },
  serverFault: {
    title: 'Payments could not be loaded',
    detail:
      'The ledger for this booking is unavailable. This is not the same as having no payments — do not record a charge until it loads.',
    canRetry: true,
  },
} as const

function CancellationSection({ booking, timeZone }: { booking: Booking; timeZone: string }) {
  return (
    <Card padded={false}>
      <CardHeader title="Cancellation" />
      <CardBody>
        <Facts>
          <Fact label="Cancelled">
            {booking.cancelled_at ? formatDateTime(booking.cancelled_at, timeZone) : UNAVAILABLE}
          </Fact>
          <Fact label="Reason">
            {booking.cancellation_reason ? booking.cancellation_reason : <Muted>Not recorded</Muted>}
          </Fact>
        </Facts>
      </CardBody>
    </Card>
  )
}

function MetadataSection({ booking, timeZone }: { booking: Booking; timeZone: string }) {
  return (
    <Card padded={false}>
      <CardHeader title="Record" />
      <CardBody>
        <Facts>
          <Fact label="Created">{formatDateTime(booking.created_at, timeZone)}</Fact>
          <Fact label="Last updated">{formatDateTime(booking.updated_at, timeZone)}</Fact>
        </Facts>
        <p className={styles.note}>
          Times are shown in the property&rsquo;s own time zone ({timeZone}). Stay dates are
          calendar dates and carry no time zone.
        </p>
      </CardBody>
    </Card>
  )
}

function Muted({ children }: { readonly children: ReactNode }) {
  return <span className={styles.muted}>{children}</span>
}
