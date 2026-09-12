import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import type { Hotel, HotelUpdateRequest } from '@/types/hotel'

import styles from './PropertyForms.module.css'

export interface HotelFormProps {
  readonly hotel: Hotel
  readonly onUpdate: (payload: HotelUpdateRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
  readonly onDirty?: () => void
}

interface Draft {
  name: string
  address_line1: string
  address_line2: string
  city: string
  region: string
  postal_code: string
  country_code: string
  timezone: string
  currency: string
  star_rating: string
  email: string
  phone: string
  website: string
  is_active: boolean
}

function draftFrom(hotel: Hotel): Draft {
  return {
    name: hotel.name,
    address_line1: hotel.address_line1,
    address_line2: hotel.address_line2 ?? '',
    city: hotel.city,
    region: hotel.region ?? '',
    postal_code: hotel.postal_code ?? '',
    country_code: hotel.country_code,
    timezone: hotel.timezone,
    currency: hotel.currency,
    star_rating: hotel.star_rating === null ? '' : String(hotel.star_rating),
    email: hotel.email ?? '',
    phone: hotel.phone ?? '',
    website: hotel.website ?? '',
    is_active: hotel.is_active,
  }
}

/**
 * Editing a property's own record.
 *
 * ## The slug is shown and cannot be changed
 *
 * `HotelUpdate` has no `slug` -- sending one is a 422, verified -- because it is the hotel's
 * stable URL identity and renaming it would break every link already pointing at the
 * property. It is displayed read-only rather than hidden: someone looking for "rename the
 * URL" should find the answer on the screen rather than conclude the field was forgotten.
 *
 * ## `is_active` is here and not on creation
 *
 * The mirror image of the slug: create refuses it (422, verified), update accepts it. A
 * property is created active and withdrawn later.
 *
 * ## Latitude and longitude are not offered
 *
 * They are in the contract and are typed, and they are deliberately **not** edited here: a
 * pair of six-decimal coordinates typed into two text boxes is a worse way to move a pin
 * than any map, and getting them wrong silently relocates the property. They are displayed
 * on the record; changing them is not something this screen claims to do well.
 *
 * ## Case is the server's to fix
 *
 * `country_code` and `currency` are upper-cased by the schema -- `gr` and `eur` were both
 * accepted and stored canonically, verified -- so they are sent as typed rather than
 * normalised here a second time.
 */
export function HotelForm({ hotel, onUpdate, onCancel, busy, onDirty }: HotelFormProps) {
  const slugId = useId()
  const nameId = useId()
  const address1Id = useId()
  const address2Id = useId()
  const cityId = useId()
  const regionId = useId()
  const postalId = useId()
  const countryId = useId()
  const timezoneId = useId()
  const currencyId = useId()
  const starsId = useId()
  const emailId = useId()
  const phoneId = useId()
  const websiteId = useId()
  const activeId = useId()
  const errorId = useId()

  const [draft, setDraft] = useState<Draft>(() => draftFrom(hotel))
  const [problem, setProblem] = useState<string | null>(null)

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
    setProblem(null)
    onDirty?.()
  }

  const validate = (): string | null => {
    if (draft.name.trim() === '') {
      return 'A property needs a name.'
    }
    if (draft.address_line1.trim() === '') {
      return 'A property needs a first address line.'
    }
    if (draft.city.trim() === '') {
      return 'A property needs a city.'
    }
    if (!/^[A-Za-z]{2}$/.test(draft.country_code.trim())) {
      return 'A country is a two-letter code, such as GR.'
    }
    if (!/^[A-Za-z]{3}$/.test(draft.currency.trim())) {
      return 'A currency is a three-letter code, such as EUR.'
    }
    if (draft.timezone.trim() === '') {
      return 'A property needs a time zone. Dates across the platform are resolved in it.'
    }
    if (draft.star_rating !== '' && !/^[1-5]$/.test(draft.star_rating)) {
      return 'A star rating is between 1 and 5, or blank.'
    }
    return null
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (busy) {
      return
    }
    const failure = validate()
    if (failure !== null) {
      setProblem(failure)
      return
    }
    setProblem(null)

    onUpdate({
      name: draft.name.trim(),
      address_line1: draft.address_line1.trim(),
      // An emptied optional becomes an explicit null: that is how the API clears a column.
      address_line2: draft.address_line2.trim() === '' ? null : draft.address_line2.trim(),
      city: draft.city.trim(),
      region: draft.region.trim() === '' ? null : draft.region.trim(),
      postal_code: draft.postal_code.trim() === '' ? null : draft.postal_code.trim(),
      country_code: draft.country_code.trim(),
      timezone: draft.timezone.trim(),
      currency: draft.currency.trim(),
      star_rating: draft.star_rating === '' ? null : Number(draft.star_rating),
      email: draft.email.trim() === '' ? null : draft.email.trim(),
      phone: draft.phone.trim() === '' ? null : draft.phone.trim(),
      website: draft.website.trim() === '' ? null : draft.website.trim(),
      is_active: draft.is_active,
    })
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <p className={styles.intro}>
        Editing a property needs the owner role &mdash; a higher permission than its room
        types, which need manager.
      </p>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={slugId}>
            Slug
          </label>
          <input
            id={slugId}
            className={styles.input}
            type="text"
            value={hotel.slug}
            disabled
            readOnly
          />
          <span className={styles.hint}>
            The property&rsquo;s stable URL identity. The API does not allow changing it.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={nameId}>
            Name
          </label>
          <input
            id={nameId}
            className={styles.input}
            type="text"
            maxLength={200}
            value={draft.name}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              set('name', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={address1Id}>
            Address line 1
          </label>
          <input
            id={address1Id}
            className={styles.input}
            type="text"
            maxLength={200}
            value={draft.address_line1}
            disabled={busy}
            required
            onChange={(event) => {
              set('address_line1', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={address2Id}>
            Address line 2 (optional)
          </label>
          <input
            id={address2Id}
            className={styles.input}
            type="text"
            maxLength={200}
            value={draft.address_line2}
            disabled={busy}
            onChange={(event) => {
              set('address_line2', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={cityId}>
            City
          </label>
          <input
            id={cityId}
            className={styles.input}
            type="text"
            maxLength={100}
            value={draft.city}
            disabled={busy}
            required
            onChange={(event) => {
              set('city', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={regionId}>
            Region (optional)
          </label>
          <input
            id={regionId}
            className={styles.input}
            type="text"
            maxLength={100}
            value={draft.region}
            disabled={busy}
            onChange={(event) => {
              set('region', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={postalId}>
            Postal code (optional)
          </label>
          <input
            id={postalId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            maxLength={20}
            value={draft.postal_code}
            disabled={busy}
            onChange={(event) => {
              set('postal_code', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={countryId}>
            Country
          </label>
          <input
            id={countryId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            maxLength={2}
            value={draft.country_code}
            disabled={busy}
            required
            onChange={(event) => {
              set('country_code', event.target.value)
            }}
          />
          <span className={styles.hint}>Two letters. Stored upper-case by the server.</span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={timezoneId}>
            Time zone
          </label>
          <input
            id={timezoneId}
            className={styles.input}
            type="text"
            maxLength={64}
            value={draft.timezone}
            disabled={busy}
            required
            onChange={(event) => {
              set('timezone', event.target.value)
            }}
          />
          <span className={styles.hint}>
            An IANA name, such as Europe/Athens. Every dated figure on this platform is
            resolved in it &mdash; changing it changes what &ldquo;today&rdquo; means here.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={currencyId}>
            Currency
          </label>
          <input
            id={currencyId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            maxLength={3}
            value={draft.currency}
            disabled={busy}
            required
            onChange={(event) => {
              set('currency', event.target.value)
            }}
          />
          <span className={styles.hint}>
            Three letters. The property&rsquo;s own currency; ledger lines may still be
            recorded in others.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={starsId}>
            Star rating (optional)
          </label>
          <input
            id={starsId}
            className={`${styles.input} ${styles.short}`}
            type="number"
            inputMode="numeric"
            min={1}
            max={5}
            step={1}
            value={draft.star_rating}
            disabled={busy}
            onChange={(event) => {
              set('star_rating', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={emailId}>
            Email (optional)
          </label>
          <input
            id={emailId}
            className={styles.input}
            type="email"
            maxLength={254}
            value={draft.email}
            disabled={busy}
            onChange={(event) => {
              set('email', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={phoneId}>
            Phone (optional)
          </label>
          <input
            id={phoneId}
            className={styles.input}
            type="tel"
            maxLength={50}
            value={draft.phone}
            disabled={busy}
            onChange={(event) => {
              set('phone', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={websiteId}>
            Website (optional)
          </label>
          <input
            id={websiteId}
            className={styles.input}
            type="url"
            maxLength={255}
            value={draft.website}
            disabled={busy}
            onChange={(event) => {
              set('website', event.target.value)
            }}
          />
        </div>

        <div className={`${styles.field} ${styles.wide}`}>
          <span className={styles.label}>Status</span>
          <label className={styles.checkbox} htmlFor={activeId}>
            <input
              id={activeId}
              type="checkbox"
              checked={draft.is_active}
              disabled={busy}
              onChange={(event) => {
                set('is_active', event.target.checked)
              }}
            />
            This property is active
          </label>
        </div>
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Saving…' : 'Save property'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
