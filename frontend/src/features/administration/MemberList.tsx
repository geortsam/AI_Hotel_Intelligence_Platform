import { useId } from 'react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { formatDateTime } from '@/lib/format'
import { useIsCompact } from '@/lib/useIsCompact'
import { HOTEL_ROLES, type HotelRole, type Member } from '@/types/member'

import styles from './MemberList.module.css'

export interface MemberListProps {
  readonly members: readonly Member[]
  readonly timeZone: string
  /** The signed-in account's public id, so their own row can be marked. */
  readonly currentUserPublicId: string | null
  readonly onRoleChange: (member: Member, role: HotelRole) => void
  readonly onRemove: (member: Member) => void
  /** The member a write is currently acting on, so only their controls are disabled. */
  readonly busyMember: string | null
  readonly busy: boolean
}

/**
 * Who may reach this property.
 *
 * A table above 768px and cards below, the same switch every list in this application makes.
 * Nothing is dropped on a phone: a member list has five facts and the one most likely to be
 * needed away from a desk -- who has owner access -- is not the one worth hiding.
 *
 * ## The role control is a select, not four buttons
 *
 * `PATCH` takes exactly one field with exactly four values, so the control that matches the
 * contract is a single choice. Changing it submits immediately; there is no separate save
 * button, because there is nothing else on the row to save alongside it.
 *
 * ## "Account disabled" is about the ACCOUNT
 *
 * `is_active` is the user's, not the membership's -- the backend's schema says so in as many
 * words. A disabled account keeps every membership it had and simply cannot sign in, so the
 * badge says *account*. Labelling it "Inactive" next to a role would read as though access
 * had been revoked, which is a different thing entirely and is done by removing the row.
 *
 * ## Why every control is offered on every row
 *
 * Writes need the owner role and the frontend is not told the caller's. The established
 * pattern applies: offer the documented control, render the server's 403. The refusals that
 * actually matter here are the **409s** -- the last owner can be neither demoted nor removed --
 * and the page explains those in the same place.
 */
export function MemberList({
  members,
  timeZone,
  currentUserPublicId,
  onRoleChange,
  onRemove,
  busyMember,
  busy,
}: MemberListProps) {
  const isCompact = useIsCompact()

  if (isCompact) {
    return (
      <ul className={styles.cards}>
        {members.map((member) => (
          <li key={member.user_public_id} className={styles.card}>
            <div className={styles.cardHeader}>
              <div className={styles.identity}>
                <h3 className={styles.cardTitle}>
                  {member.full_name}
                  {member.user_public_id === currentUserPublicId ? (
                    <span className={styles.you}> (you)</span>
                  ) : null}
                </h3>
                <span className={styles.email}>{member.email}</span>
              </div>
              {member.is_active ? null : <Badge tone="warning">Account disabled</Badge>}
            </div>
            <dl className={styles.fields}>
              <div className={styles.field}>
                <dt className={styles.fieldLabel}>Joined</dt>
                <dd className={styles.fieldValue}>
                  {formatDateTime(member.joined_at, timeZone)}
                </dd>
              </div>
            </dl>
            <div className={styles.actions}>
              <RoleControl
                member={member}
                onRoleChange={onRoleChange}
                disabled={busy}
                busy={busyMember === member.user_public_id}
              />
              <Button
                variant="danger"
                size="sm"
                disabled={busy}
                onClick={() => {
                  onRemove(member)
                }}
              >
                Remove {member.email}
              </Button>
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
          Members of this property, ordered by email address.
        </caption>
        <thead>
          <tr>
            <th scope="col">Member</th>
            <th scope="col">Account</th>
            <th scope="col">Role</th>
            <th scope="col">Joined</th>
            <th scope="col">
              <span className={styles.srOnly}>Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {members.map((member) => (
            <tr key={member.user_public_id}>
              <th scope="row" className={styles.memberCell}>
                <span className={styles.name}>
                  {member.full_name}
                  {member.user_public_id === currentUserPublicId ? (
                    <span className={styles.you}> (you)</span>
                  ) : null}
                </span>
                <span className={styles.email}>{member.email}</span>
              </th>
              <td>
                {member.is_active ? (
                  <Badge tone="success">Enabled</Badge>
                ) : (
                  <Badge tone="warning">Disabled</Badge>
                )}
              </td>
              <td>
                <RoleControl
                  member={member}
                  onRoleChange={onRoleChange}
                  disabled={busy}
                  busy={busyMember === member.user_public_id}
                />
              </td>
              <td className={styles.timestamp}>{formatDateTime(member.joined_at, timeZone)}</td>
              <td>
                <div className={styles.rowActions}>
                  <Button
                    variant="danger"
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      onRemove(member)
                    }}
                  >
                    {busyMember === member.user_public_id && busy
                      ? 'Working…'
                      : `Remove ${member.email}`}
                  </Button>
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
 * The per-row role control.
 *
 * Its accessible name carries the member's email, so a screen reader hears "Role for
 * dana@example.test" rather than the fourth identical "Role" on the page. The visible
 * label is hidden rather than absent: a column header is not an accessible name for a
 * control inside the cell.
 */
function RoleControl({
  member,
  onRoleChange,
  disabled,
  busy,
}: {
  member: Member
  onRoleChange: (member: Member, role: HotelRole) => void
  disabled: boolean
  busy: boolean
}) {
  const id = useId()
  return (
    <span className={styles.roleControl}>
      <label className={styles.srOnly} htmlFor={id}>
        Role for {member.email}
      </label>
      <select
        id={id}
        className={styles.select}
        value={member.role}
        disabled={disabled}
        onChange={(event) => {
          const next = event.target.value as HotelRole
          if (next !== member.role) {
            // Reasserting the same role is a documented no-op on the backend; not sending it
            // keeps a request that cannot change anything off the wire.
            onRoleChange(member, next)
          }
        }}
      >
        {HOTEL_ROLES.map((role) => (
          <option key={role} value={role}>
            {role}
          </option>
        ))}
      </select>
      {busy ? <span className={styles.working}>Saving…</span> : null}
    </span>
  )
}
