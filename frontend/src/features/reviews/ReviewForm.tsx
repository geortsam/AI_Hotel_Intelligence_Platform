import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { REVIEW_SOURCES, formatReviewRating, sourceLabel } from '@/features/reviews/vocabulary'
import { compareDecimalStrings, toDecimalString } from '@/lib/decimal'
import type { RatingScale, ReviewCreateRequest, ReviewSource } from '@/types/review'

import styles from './ReviewForm.module.css'

export interface ReviewFormProps {
  /** Today in the hotel's own zone, as the default review date. */
  readonly today: string
  readonly onSubmit: (bookingPublicId: string, payload: ReviewCreateRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
  /** Clears the previous refusal as soon as a new attempt starts. */
  readonly onDirty?: () => void
}

/**
 * Recording the review of a stay.
 *
 * ## The booking is the address, not a field
 *
 * `POST /hotels/{h}/bookings/{b}/review` -- the stay is in the URL, and it is what makes the
 * review addressable at all. So the identifier is asked for first and is required, and it is
 * a pasted public id rather than a dropdown: no endpoint lists the stays still awaiting a
 * review, and populating a picker would mean fetching the property's entire booking history.
 *
 * ## The author is not in the payload, deliberately
 *
 * `guest_id` is derived from the booking. The schema's own reason is that accepting it would
 * create a way to attribute a review to someone who did not stay; sending `guest_public_id`
 * is a 422, verified. So this form has no author field, and the review it records is
 * attributed by the server to whoever the booking says stayed.
 *
 * ## The scale is chosen, and the rating is checked against it
 *
 * `rating_scale` is 5 or 10 and `ck_reviews_rating_in_scale` requires `0 <= rating <=
 * rating_scale`. Choosing the scale before typing the rating is what makes "9" meaningful.
 * The bound is checked here as a **string comparison** -- `compareDecimalStrings`, the same
 * comparator the refund form uses -- so a rating never becomes a float on its way to a
 * `NUMERIC(4,2)` column. Three decimal places are refused for the same reason: the column
 * would silently round 4.567 to 4.57, and the schema rejects rather than alter a rating.
 *
 * ## One step, because this one is reversible
 *
 * Unlike a ledger line, a review posted by mistake can be hidden -- `is_published` is exactly
 * that column -- so there is no confirmation step. What there is instead is a guard against
 * sending it twice: the button disables while a request is in flight, and the hook holds a
 * ref so two clicks in one tick cannot both fire. A repeat that did get through would be a
 * **409** from `uq_reviews_booking_id`, not a second review, and that refusal is rendered.
 */
export function ReviewForm({ today, onSubmit, onCancel, busy, onDirty }: ReviewFormProps) {
  const bookingId = useId()
  const sourceId = useId()
  const ratingId = useId()
  const scaleId = useId()
  const dateId = useId()
  const titleId = useId()
  const bodyId = useId()
  const nameId = useId()
  const languageId = useId()
  const externalId = useId()
  const publishedId = useId()
  const errorId = useId()

  const [booking, setBooking] = useState('')
  const [source, setSource] = useState<ReviewSource>('direct')
  const [rating, setRating] = useState('')
  const [scale, setScale] = useState<RatingScale>(5)
  const [date, setDate] = useState(today)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [name, setName] = useState('')
  const [language, setLanguage] = useState('')
  const [external, setExternal] = useState('')
  const [published, setPublished] = useState(true)
  const [problem, setProblem] = useState<string | null>(null)

  const touched = () => {
    setProblem(null)
    onDirty?.()
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (busy) {
      return
    }
    if (booking.trim() === '') {
      setProblem('Enter the identifier of the stay this review is of.')
      return
    }
    // `NUMERIC(4,2)`, non-negative, at most two decimal places. Checked as a string.
    if (!/^(?:\d{1,2})(?:\.\d{1,2})?$/.test(rating.trim())) {
      setProblem('Enter a rating with at most two decimal places, such as 4 or 8.5.')
      return
    }
    if (compareDecimalStrings(rating.trim(), String(scale)) > 0) {
      setProblem(`A rating cannot exceed its scale. This review is out of ${scale}.`)
      return
    }
    if (date === '') {
      setProblem('Enter the date of the review.')
      return
    }
    // `ck_reviews_language_format`: null, or exactly two lower-case letters.
    if (language.trim() !== '' && !/^[A-Za-z]{2}$/.test(language.trim())) {
      setProblem('A language is a two-letter code, such as en or el.')
      return
    }
    setProblem(null)

    onSubmit(booking.trim(), {
      source,
      rating: toDecimalString(rating),
      rating_scale: scale,
      review_date: date,
      // Sent only when the field means something. An omitted optional is not an empty
      // string, and `extra="forbid"` makes a wrong shape a 422 rather than a shrug.
      ...(title.trim() !== '' ? { title: title.trim() } : {}),
      ...(body.trim() !== '' ? { body: body.trim() } : {}),
      ...(name.trim() !== '' ? { reviewer_name: name.trim() } : {}),
      // Lower-cased because the constraint is `^[a-z]{2}$` and a language code's canonical
      // form is lower case -- a canonicalisation, not a rewrite of the guest's words.
      ...(language.trim() !== '' ? { language: language.trim().toLowerCase() } : {}),
      ...(external.trim() !== '' ? { external_review_id: external.trim() } : {}),
      // Always sent: the table defaults to true, and an import landing a review already
      // hidden is exactly what the field is for.
      is_published: published,
    })
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <p className={styles.intro}>
        Records a review against a stay &mdash; one received directly, or imported from a
        channel. The reviewer is whoever the booking says stayed; there is no author field,
        because the server takes the author from the booking.
      </p>

      <div className={styles.grid}>
        <div className={`${styles.field} ${styles.wide}`}>
          <label className={styles.label} htmlFor={bookingId}>
            Stay
          </label>
          <input
            id={bookingId}
            className={styles.input}
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="Booking identifier"
            value={booking}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            onChange={(event) => {
              setBooking(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            The identifier from a booking&rsquo;s own page. A stay can have one review; a
            second is refused.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={sourceId}>
            Channel
          </label>
          <select
            id={sourceId}
            className={styles.select}
            value={source}
            disabled={busy}
            onChange={(event) => {
              setSource(event.target.value as ReviewSource)
              touched()
            }}
          >
            {REVIEW_SOURCES.map((option) => (
              <option key={option} value={option}>
                {sourceLabel(option)}
              </option>
            ))}
          </select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={scaleId}>
            Rating scale
          </label>
          <select
            id={scaleId}
            className={styles.select}
            value={scale}
            disabled={busy}
            onChange={(event) => {
              // Compared as a string: `Number()` has no business anywhere near a rating, and
              // the two options are literals this component itself wrote.
              setScale(event.target.value === '10' ? 10 : 5)
              touched()
            }}
          >
            <option value={5}>Out of 5</option>
            <option value={10}>Out of 10</option>
          </select>
          <span className={styles.hint}>
            The channel&rsquo;s own scale. Booking.com scores out of ten, Tripadvisor out of
            five; nothing converts between them.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={ratingId}>
            Rating
          </label>
          <input
            id={ratingId}
            className={styles.input}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            placeholder={scale === 10 ? '8.5' : '4'}
            value={rating}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              setRating(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            {rating.trim() === ''
              ? `Between 0 and ${scale}, to at most two decimal places.`
              : `Will be recorded as ${formatReviewRating(rating.trim(), scale)}.`}
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={dateId}>
            Review date
          </label>
          <input
            id={dateId}
            className={styles.input}
            type="date"
            value={date}
            disabled={busy}
            required
            onChange={(event) => {
              setDate(event.target.value)
              touched()
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={nameId}>
            Reviewer name (optional)
          </label>
          <input
            id={nameId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={200}
            value={name}
            disabled={busy}
            onChange={(event) => {
              setName(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            The display name shown on the review, which need not be the guest&rsquo;s own name.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={languageId}>
            Language (optional)
          </label>
          <input
            id={languageId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            autoComplete="off"
            maxLength={2}
            placeholder="en"
            value={language}
            disabled={busy}
            onChange={(event) => {
              setLanguage(event.target.value)
              touched()
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={externalId}>
            Platform reference (optional)
          </label>
          <input
            id={externalId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={200}
            value={external}
            disabled={busy}
            onChange={(event) => {
              setExternal(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            The channel&rsquo;s own identifier for this review. Re-importing the same one is
            refused, which is what makes an import safe to repeat.
          </span>
        </div>

        <div className={`${styles.field} ${styles.wide}`}>
          <label className={styles.label} htmlFor={titleId}>
            Title (optional)
          </label>
          <input
            id={titleId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={300}
            value={title}
            disabled={busy}
            onChange={(event) => {
              setTitle(event.target.value)
              touched()
            }}
          />
        </div>

        <div className={`${styles.field} ${styles.wide}`}>
          <label className={styles.label} htmlFor={bodyId}>
            Review (optional)
          </label>
          <textarea
            id={bodyId}
            className={styles.textarea}
            rows={3}
            value={body}
            disabled={busy}
            onChange={(event) => {
              setBody(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            A rating with no written review is allowed, and is common.
          </span>
        </div>

        <div className={`${styles.field} ${styles.wide}`}>
          <label className={styles.checkbox} htmlFor={publishedId}>
            <input
              id={publishedId}
              type="checkbox"
              checked={published}
              disabled={busy}
              onChange={(event) => {
                setPublished(event.target.checked)
                touched()
              }}
            />
            Publish this review straight away
          </label>
          <span className={styles.hint}>
            Clear it to record the review hidden. It can be published later, and a published
            one can be hidden.
          </span>
        </div>
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Recording…' : 'Record review'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
