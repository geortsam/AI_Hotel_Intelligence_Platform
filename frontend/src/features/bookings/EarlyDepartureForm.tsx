import { useEffect, useId, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/Button";
import { RepricingNotice } from "@/features/bookings/RepricingNotice";
import { formatDate } from "@/lib/format";
import { ApiError } from "@/services/api/ApiError";
import { describeFailure } from "@/services/api/failures";
import { bookingMutations } from "@/services/bookings/bookingService";
import type {
  Booking,
  StayDeparturePreview,
  StayDepartureRequest,
} from "@/types/booking";

import styles from "./StayForms.module.css";

export interface EarlyDepartureFormProps {
  readonly hotelPublicId: string;
  readonly booking: Booking;
  /** The hotel's own date today, `YYYY-MM-DD`: the date the form opens on. */
  readonly today: string;
  readonly onSubmit: (payload: StayDepartureRequest) => void;
  readonly onCancel: () => void;
  readonly busy: boolean;
}

/** Why the server would not preview a departure. Fixed copy: no server text reaches the DOM. */
const PREVIEW_FAILURE_COPY = {
  conflict: {
    title: "This departure cannot be recorded",
    detail:
      "The booking may no longer be checked in, or this date is outside what the server accepts. Choose a date in the range shown.",
    canRetry: false,
  },
  notFound: {
    title: "Booking not found",
    detail:
      "This booking no longer exists, or your access to it has been removed.",
    canRetry: false,
  },
  serverFault: {
    title: "The departure could not be previewed",
    detail: "Nothing was changed. Try again shortly.",
    canRetry: true,
  },
} as const;

/**
 * Recording that a checked-in guest left before the planned check-out (Issue H2).
 *
 * ## Why this is not the plain check-out
 *
 * Checking a guest out releases the room but keeps every night of the stay, and a checked-out
 * stay still counts as occupied. Done early, the nights nobody slept in would go on counting
 * as occupancy, revenue and money owed. `POST .../stay/departure` removes them in the same
 * transaction that checks the guest out, and the server refuses the plain check-out before the
 * planned day for exactly that reason.
 *
 * ## The server decides, and this form shows its answer
 *
 * Every figure here comes from `GET .../stay/departure` -- the nights that would leave, what
 * that does to the money, and the earliest and latest dates the booking accepts. The form
 * computes none of it: it shows the server's preview for the chosen date before anything is
 * confirmed, and the date picker is bounded by the server's own range. The departure itself is
 * then judged again, under the booking's lock, and its refusal is rendered if it comes.
 */
export function EarlyDepartureForm({
  hotelPublicId,
  booking,
  today,
  onSubmit,
  onCancel,
  busy,
}: EarlyDepartureFormProps) {
  const dateId = useId();
  const summaryId = useId();

  const [departure, setDeparture] = useState(today);
  const [preview, setPreview] = useState<StayDeparturePreview | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    bookingMutations
      .previewDeparture(
        hotelPublicId,
        booking.public_id,
        departure,
        controller.signal,
      )
      .then(
        (result) => {
          setPreview(result);
          setLoading(false);
        },
        (cause: unknown) => {
          if (controller.signal.aborted) {
            return;
          }
          setPreview(null);
          setError(
            cause instanceof ApiError
              ? cause
              : new ApiError(
                  0,
                  ApiError.NETWORK_CODE,
                  "The request could not be completed.",
                ),
          );
          setLoading(false);
        },
      );
    return () => {
      controller.abort();
    };
  }, [hotelPublicId, booking.public_id, departure]);

  const failure =
    error === null ? null : describeFailure(error, PREVIEW_FAILURE_COPY);
  const ready =
    preview !== null && preview.departure_date === departure && !loading;

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (busy || !ready) {
      return;
    }
    onSubmit({ departure_date: departure });
  };

  return (
    <form
      className={styles.form}
      onSubmit={handleSubmit}
      noValidate
      aria-label="Record an early departure"
    >
      <p className={styles.intro}>
        <strong>Early departure.</strong> The guest is planned to check out on{" "}
        {formatDate(booking.check_out_date)}. Recording that they left earlier
        checks them out and removes every night from the departure date on, so
        the nights nobody stayed stop counting as occupancy, revenue and money
        owed.
      </p>

      <div className={styles.fields}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={dateId}>
            Departure date
          </label>
          <input
            id={dateId}
            className={styles.input}
            type="date"
            value={departure}
            {...(preview
              ? {
                  min: preview.earliest_departure_date,
                  max: preview.latest_departure_date,
                }
              : {})}
            disabled={busy}
            required
            aria-describedby={summaryId}
            onChange={(event) => {
              if (event.target.value !== "") {
                setDeparture(event.target.value);
              }
            }}
          />
        </div>
      </div>

      <div id={summaryId} aria-live="polite">
        {loading ? (
          <p className={styles.hint}>
            Checking this departure with the server…
          </p>
        ) : null}
        {ready ? (
          <>
            <p className={styles.hint}>
              Removes {preview.nights_removed} night
              {preview.nights_removed === 1 ? "" : "s"}: the stay ends on{" "}
              {formatDate(preview.departure_date)} instead of{" "}
              {formatDate(preview.planned_check_out_date)}. Dates from{" "}
              {formatDate(preview.earliest_departure_date)} to{" "}
              {formatDate(preview.latest_departure_date)} can be recorded.
            </p>
            <RepricingNotice repricing={preview.repricing} />
          </>
        ) : null}
      </div>

      {failure ? (
        <div className={styles.error} role="alert">
          <strong>{failure.title}</strong> {failure.detail}
        </div>
      ) : null}

      <div className={styles.actions}>
        <Button
          type="submit"
          variant="primary"
          size="sm"
          disabled={busy || !ready}
        >
          {busy ? "Recording…" : "Record early departure"}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          onClick={onCancel}
        >
          Cancel
        </Button>
      </div>
    </form>
  );
}
