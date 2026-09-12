import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { formatCount, formatDateTime, formatMoney, UNAVAILABLE } from '@/lib/format'
import { useIsCompact } from '@/lib/useIsCompact'
import type { RoomType } from '@/types/room'

import styles from './RoomTypeList.module.css'

export interface RoomTypeListProps {
  readonly types: readonly RoomType[]
  readonly timeZone: string
  readonly onEdit: (type: RoomType) => void
  readonly onDelete: (type: RoomType) => void
  /** The code currently being written, so only its own controls are disabled. */
  readonly busyCode: string | null
  readonly busy: boolean
}

/**
 * A property's room types.
 *
 * A table above 768px and cards below, the same switch every list in this application makes.
 * Both render the same fields: a type has nine of them and dropping four on a phone drops
 * the ones somebody needed.
 *
 * ## Every figure is a stored column
 *
 * Occupancies, bed count, size and base price are fields of `RoomTypeResponse`. Nothing here
 * prices a stay, counts availability or infers occupancy -- `base_price` is the type's list
 * price and is formatted for display from the decimal string it arrives as.
 *
 * ## Why the delete control is always offered
 *
 * Deleting needs the manager role, and the frontend is not told the caller's role. The
 * established pattern applies: the control is offered and the server's 403 is rendered. The
 * far more common refusal is the **409** from rooms still assigned to the type, which the
 * page explains in the same place.
 */
export function RoomTypeList({
  types,
  timeZone,
  onEdit,
  onDelete,
  busyCode,
  busy,
}: RoomTypeListProps) {
  const isCompact = useIsCompact()

  if (isCompact) {
    return (
      <ul className={styles.cards}>
        {types.map((type) => (
          <li key={type.code} className={styles.card}>
            <div className={styles.cardHeader}>
              <div>
                <h3 className={styles.cardTitle}>{type.name}</h3>
                <span className={styles.code}>{type.code}</span>
              </div>
              <Badge tone={type.is_active ? 'success' : 'danger'}>
                {type.is_active ? 'Bookable' : 'Withdrawn'}
              </Badge>
            </div>
            <dl className={styles.fields}>
              <Field label="Sleeps">
                {formatCount(type.standard_occupancy)} standard, {formatCount(type.max_occupancy)}{' '}
                maximum
              </Field>
              <Field label="Beds">
                {formatCount(type.bed_count)}
                {type.bed_configuration === null ? '' : ` — ${type.bed_configuration}`}
              </Field>
              <Field label="Base price">{formatMoney(type.base_price, type.currency)}</Field>
              {type.size_sqm === null ? null : <Field label="Size">{type.size_sqm} m²</Field>}
              <Field label="Updated">{formatDateTime(type.updated_at, timeZone)}</Field>
            </dl>
            {type.description === null || type.description === '' ? null : (
              <p className={styles.description}>{type.description}</p>
            )}
            <div className={styles.actions}>
              <Actions
                type={type}
                onEdit={onEdit}
                onDelete={onDelete}
                busy={busy}
                busyCode={busyCode}
              />
            </div>
          </li>
        ))}
      </ul>
    )
  }

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          Room types of this property, ordered by code.
        </caption>
        <thead>
          <tr>
            <th scope="col">Code</th>
            <th scope="col">Name</th>
            <th scope="col">Sleeps</th>
            <th scope="col">Beds</th>
            <th scope="col">Size</th>
            <th scope="col">Base price</th>
            <th scope="col">Status</th>
            <th scope="col">Updated</th>
            <th scope="col">
              <span className={styles.srOnly}>Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {types.map((type) => (
            <tr key={type.code}>
              <th scope="row" className={styles.codeCell}>
                <span className={styles.code}>{type.code}</span>
              </th>
              <td className={styles.nameCell}>
                {type.name}
                {type.description === null || type.description === '' ? null : (
                  <span className={styles.rowDescription}>{type.description}</span>
                )}
              </td>
              <td className={styles.numeric}>
                {formatCount(type.standard_occupancy)} / {formatCount(type.max_occupancy)}
              </td>
              <td className={styles.numeric}>{formatCount(type.bed_count)}</td>
              <td className={styles.numeric}>
                {type.size_sqm === null ? (
                  <span className={styles.absent}>{UNAVAILABLE}</span>
                ) : (
                  `${type.size_sqm} m²`
                )}
              </td>
              <td className={styles.numeric}>{formatMoney(type.base_price, type.currency)}</td>
              <td>
                <Badge tone={type.is_active ? 'success' : 'danger'}>
                  {type.is_active ? 'Bookable' : 'Withdrawn'}
                </Badge>
              </td>
              <td className={styles.timestamp}>{formatDateTime(type.updated_at, timeZone)}</td>
              <td>
                <div className={styles.actions}>
                  <Actions
                    type={type}
                    onEdit={onEdit}
                    onDelete={onDelete}
                    busy={busy}
                    busyCode={busyCode}
                  />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * The per-row controls.
 *
 * Each button's accessible name carries the code, so a screen reader hears "Edit DLX" rather
 * than the ninth identical "Edit" on the page.
 */
function Actions({
  type,
  onEdit,
  onDelete,
  busy,
  busyCode,
}: {
  type: RoomType
  onEdit: (type: RoomType) => void
  onDelete: (type: RoomType) => void
  busy: boolean
  busyCode: string | null
}) {
  const thisOne = busyCode === type.code
  return (
    <>
      <Button
        variant="secondary"
        size="sm"
        disabled={busy}
        onClick={() => {
          onEdit(type)
        }}
      >
        Edit {type.code}
      </Button>
      <Button
        variant="danger"
        size="sm"
        disabled={busy}
        onClick={() => {
          onDelete(type)
        }}
      >
        {thisOne ? 'Deleting…' : `Delete ${type.code}`}
      </Button>
    </>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <dt className={styles.fieldLabel}>{label}</dt>
      <dd className={styles.fieldValue}>{children}</dd>
    </div>
  )
}
