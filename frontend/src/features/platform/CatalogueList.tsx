import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { UNAVAILABLE } from '@/lib/format'
import { useIsCompact } from '@/lib/useIsCompact'
import type { CatalogueRow } from '@/features/platform/useCatalogue'
import type { CatalogueKind } from '@/types/platform'

import styles from './CatalogueList.module.css'

export interface CatalogueListProps {
  readonly kind: CatalogueKind
  readonly rows: readonly CatalogueRow[]
  readonly onEdit: (row: CatalogueRow) => void
  readonly onDelete: (row: CatalogueRow) => void
  /** The code currently being written, so only its own controls report progress. */
  readonly busyCode: string | null
  readonly busy: boolean
}

/** What the third column is called, per catalogue. Each is a real column, not a synonym. */
const SECOND_COLUMN: Readonly<Record<CatalogueKind, string>> = {
  amenities: 'Group',
  'revenue-categories': 'Room revenue',
  'expense-categories': 'Fixed cost',
}

/**
 * One shared catalogue's entries.
 *
 * A table above 768px and cards below, the same switch every list in this application makes.
 * These rows are three or four fields wide, so nothing is dropped on a phone.
 *
 * ## The flag column differs per catalogue, and says so
 *
 * A revenue category carries `is_room_revenue`; an expense category carries `is_fixed_cost`;
 * an amenity carries a free-text `category` and neither boolean. The header changes with the
 * catalogue rather than being called something vague enough to cover all three, because
 * "Room revenue" and "Fixed cost" are the words the schema uses and the words an operator
 * needs in order to know what the tick means.
 *
 * ## Retired entries are shown, not hidden
 *
 * `is_active: false` is how a category is retired -- it stays in the table because ledger
 * lines already reference it. This list shows retired entries with a word, and the page
 * offers the server's own `is_active` filter for the two catalogues that have one.
 */
export function CatalogueList({
  kind,
  rows,
  onEdit,
  onDelete,
  busyCode,
  busy,
}: CatalogueListProps) {
  const isCompact = useIsCompact()
  const hasActive = kind !== 'amenities'

  if (isCompact) {
    return (
      <ul className={styles.cards}>
        {rows.map((row) => (
          <li key={row.code} className={styles.card}>
            <div className={styles.cardHeader}>
              <div className={styles.identity}>
                <h3 className={styles.cardTitle}>{row.name}</h3>
                <span className={styles.code}>{row.code}</span>
              </div>
              {hasActive ? (
                <Badge tone={row.isActive ? 'success' : 'danger'}>
                  {row.isActive ? 'Active' : 'Retired'}
                </Badge>
              ) : null}
            </div>
            <dl className={styles.fields}>
              <div className={styles.field}>
                <dt className={styles.fieldLabel}>{SECOND_COLUMN[kind]}</dt>
                <dd className={styles.fieldValue}>
                  {kind === 'amenities'
                    ? (row.category ?? <span className={styles.absent}>{UNAVAILABLE}</span>)
                    : row.flag
                      ? 'Yes'
                      : 'No'}
                </dd>
              </div>
            </dl>
            <div className={styles.actions}>
              <Actions
                row={row}
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
          Entries in this shared catalogue, ordered by code. They belong to no property.
        </caption>
        <thead>
          <tr>
            <th scope="col">Code</th>
            <th scope="col">Name</th>
            <th scope="col">{SECOND_COLUMN[kind]}</th>
            {hasActive ? <th scope="col">Status</th> : null}
            <th scope="col">
              <span className={styles.srOnly}>Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.code}>
              <th scope="row" className={styles.codeCell}>
                <span className={styles.code}>{row.code}</span>
              </th>
              <td className={styles.nameCell}>{row.name}</td>
              <td>
                {kind === 'amenities' ? (
                  (row.category ?? <span className={styles.absent}>{UNAVAILABLE}</span>)
                ) : row.flag ? (
                  'Yes'
                ) : (
                  'No'
                )}
              </td>
              {hasActive ? (
                <td>
                  <Badge tone={row.isActive ? 'success' : 'danger'}>
                    {row.isActive ? 'Active' : 'Retired'}
                  </Badge>
                </td>
              ) : null}
              <td>
                <div className={styles.rowActions}>
                  <Actions
                    row={row}
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
 * Each button's accessible name carries the code, so a screen reader hears "Edit WIFI"
 * rather than the eleventh identical "Edit" on the page.
 */
function Actions({
  row,
  onEdit,
  onDelete,
  busy,
  busyCode,
}: {
  row: CatalogueRow
  onEdit: (row: CatalogueRow) => void
  onDelete: (row: CatalogueRow) => void
  busy: boolean
  busyCode: string | null
}) {
  const thisOne = busyCode === row.code
  return (
    <>
      <Button
        variant="secondary"
        size="sm"
        disabled={busy}
        onClick={() => {
          onEdit(row)
        }}
      >
        Edit {row.code}
      </Button>
      <Button
        variant="danger"
        size="sm"
        disabled={busy}
        onClick={() => {
          onDelete(row)
        }}
      >
        {thisOne ? 'Deleting…' : `Delete ${row.code}`}
      </Button>
    </>
  )
}
