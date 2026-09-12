import { useEffect, useState, type ReactNode } from 'react'
import { AlertTriangle, Building2, Plus, ShieldAlert, UserPlus, WifiOff } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { AddMemberForm } from '@/features/administration/AddMemberForm'
import { ChangePasswordForm } from '@/features/administration/ChangePasswordForm'
import { MemberList } from '@/features/administration/MemberList'
import { useMembers } from '@/features/administration/useMembers'
import { Pagination } from '@/features/bookings/Pagination'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { formatCount, formatDateTime } from '@/lib/format'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useAuth } from '@/session/AuthProvider'
import { useHotelContext } from '@/session/HotelProvider'
import { ROLE_DESCRIPTIONS, type HotelRole, type Member } from '@/types/member'

import styles from './AdministrationPage.module.css'

/** Every option is inside the router's own `ge=1, le=100`, so none can produce a 422. */
const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

const LIST_COPY = {
  forbidden: {
    title: 'You cannot see this property’s members',
    detail:
      'Listing who has access needs the manager role on this property, which this account does not have. Your own account details above are unaffected.',
    canRetry: false,
  },
  notFound: {
    title: 'Members not available',
    detail:
      'This property could not be found, or your access to it has been removed. It answers the same way for both, so there is nothing more specific to say.',
    canRetry: false,
  },
  serverFault: {
    title: 'The member list is temporarily unavailable',
    detail: 'It could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

/** Per-write copy. Every write on this screen needs the OWNER role; only listing needs manager. */
const WRITE_COPY = {
  'member-add': {
    forbidden: {
      title: 'Nobody was added',
      detail:
        'Granting access to a property needs the owner role on it, which this account does not have. Seeing the list needs only manager, which is why it is visible.',
      canRetry: false,
    },
    notFound: {
      title: 'Nobody was added',
      detail:
        'No account exists for that email address. This screen cannot create one — the person registers first, then they can be added.',
      canRetry: false,
    },
    conflict: {
      title: 'Nobody was added',
      detail:
        'That account is already a member of this property. Change their role instead of adding them again.',
      canRetry: false,
    },
    serverFault: {
      title: 'Nobody was added',
      detail: 'The member could not be added. Nothing was written.',
      canRetry: true,
    },
  },
  'member-role': {
    forbidden: {
      title: 'The role was not changed',
      detail: 'Changing what a member may do needs the owner role on this property.',
      canRetry: false,
    },
    notFound: {
      title: 'The role was not changed',
      detail: 'That account is no longer a member of this property.',
      canRetry: false,
    },
    conflict: {
      title: 'The role was not changed',
      detail:
        'This property would be left with no owner. A property must always keep at least one — grant the owner role to somebody else first, then change this one.',
      canRetry: false,
    },
    serverFault: {
      title: 'The role was not changed',
      detail: 'The change could not be saved. The member is unchanged.',
      canRetry: true,
    },
  },
  'member-remove': {
    forbidden: {
      title: 'Nobody was removed',
      detail: 'Revoking access needs the owner role on this property.',
      canRetry: false,
    },
    notFound: {
      title: 'Nobody was removed',
      detail: 'That account is no longer a member of this property.',
      canRetry: false,
    },
    conflict: {
      title: 'Nobody was removed',
      detail:
        'This property would be left with no owner. A property must always keep at least one — grant the owner role to somebody else first, then remove this one.',
      canRetry: false,
    },
    serverFault: {
      title: 'Nobody was removed',
      detail: 'Access could not be revoked. Nothing was changed.',
      canRetry: true,
    },
  },
} as const

/**
 * Administration &mdash; your account, and who may reach this property.
 *
 * ## Two subjects on one screen, kept apart
 *
 * The top section is **account-level**: global identity, the same person at every property,
 * changed only by the person themselves. The bottom section is **hotel-level**: one
 * membership list belonging to the selected property. They are deliberately not merged,
 * because the backend does not merge them either -- identity is global, membership is not,
 * and the same account holds a different role at each hotel.
 *
 * ## The authorization here is asymmetric, and the copy says which half failed
 *
 * Listing members needs **manager**; adding, changing a role and removing all need
 * **owner**. So a manager sees the list and is refused every control on it, and the refusal
 * says exactly that rather than implying they should not be looking.
 *
 * ## What this screen deliberately does not offer
 *
 * * **Creating an account.** `POST .../members` names an account that must already exist;
 *   registration is a public, unauthenticated endpoint and not an administrative act.
 * * **Editing a colleague's name or email.** No endpoint exists: `PATCH /auth/me` is a 405.
 * * **Disabling or deleting an account.** No endpoint exists either. `is_active` is readable
 *   and not writable, and revoking access means removing the membership.
 * * **Resetting somebody else's password.** The change-password route takes the *current*
 *   password and is aimed by the token, so it can only ever change your own.
 * * **A cross-property view of one colleague.** `GET /users/{id}/memberships` does not exist,
 *   by design: one property's administrator is not entitled to learn where else a colleague
 *   works.
 *
 * Each of those is a real gap rather than an oversight, and is recorded as one.
 */
export function AdministrationPage() {
  const { user } = useAuth()
  const hotelContext = useHotelContext()
  const selected = hotelContext.selected

  const members = useMembers(selected?.public_id ?? null)
  const [adding, setAdding] = useState(false)
  const [confirmingRemoval, setConfirmingRemoval] = useState<Member | null>(null)

  /* Switching property closes the form and clears its banners: a refusal at one hotel must
   * not sit above a form now aimed at another. */
  useEffect(() => {
    setAdding(false)
    setConfirmingRemoval(null)
    members.dismiss()
  }, [selected?.public_id])

  const listFailure =
    members.error === null ? null : describeFailure(members.error, LIST_COPY)
  const writeFailure =
    members.writeError === null || members.failedWrite === null
      ? null
      : describeFailure(members.writeError, WRITE_COPY[members.failedWrite])

  const timeZone = selected?.timezone ?? 'UTC'

  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Administration"
        description="Your own account, and who may reach the selected property. Seeing the member list needs the manager role; changing it needs owner."
      />

      <div className={styles.page}>
        {/* --- account level ------------------------------------------------------------ */}

        <section className={styles.block} aria-labelledby="admin-account">
          <div className={styles.blockHeader}>
            <div>
              <h2 className={styles.blockTitle} id="admin-account">
                Your account
              </h2>
              <p className={styles.blockNote}>
                One identity across every property. Your role is per property and is shown in
                the member list below.
              </p>
            </div>
          </div>

          <div className={styles.panel}>
            {user === null ? (
              <div className={styles.skeleton} role="status" aria-busy="true">
                <Skeleton height="6rem" label="Loading your account" />
              </div>
            ) : (
              <>
                <div className={styles.recordHeader}>
                  <h3 className={styles.recordName}>{user.full_name}</h3>
                  <Badge tone={user.is_active ? 'success' : 'warning'}>
                    {user.is_active ? 'Enabled' : 'Disabled'}
                  </Badge>
                </div>
                <dl className={styles.fields}>
                  <Field label="Email">{user.email}</Field>
                  <Field label="Account created">
                    {formatDateTime(user.created_at, timeZone)}
                  </Field>
                  <Field label="Last signed in">
                    {user.last_login_at === null ? (
                      <span className={styles.absent}>Never</span>
                    ) : (
                      formatDateTime(user.last_login_at, timeZone)
                    )}
                  </Field>
                </dl>
                <p className={styles.note}>
                  Your name and email address are not editable here: the API has no endpoint
                  that changes them.
                </p>
              </>
            )}
          </div>

          <div className={styles.panel}>
            <h3 className={styles.panelTitle}>Change your password</h3>
            <ChangePasswordForm />
          </div>
        </section>

        {/* --- hotel level -------------------------------------------------------------- */}

        <section className={styles.block} aria-labelledby="admin-members">
          <div className={styles.blockHeader}>
            <div>
              <h2 className={styles.blockTitle} id="admin-members">
                Members of {selected?.name ?? 'this property'}
              </h2>
              <p className={styles.blockNote}>
                Access is per property. Removing somebody here revokes their access to this
                property only &mdash; their account, and any access they have elsewhere, is
                untouched.
              </p>
            </div>
            {adding || hotelContext.status === 'empty' ? null : (
              <Button
                variant="primary"
                size="sm"
                disabled={members.pending !== null}
                onClick={() => {
                  members.dismiss()
                  setAdding(true)
                }}
              >
                <Plus size={16} aria-hidden="true" />
                Add member
              </Button>
            )}
          </div>

          {members.saved ? (
            <p className={styles.success} role="status">
              {members.saved} The list below is the server&rsquo;s own copy.
            </p>
          ) : null}

          {writeFailure ? (
            <div className={styles.failure} role="alert">
              <ShieldAlert size={16} aria-hidden="true" />
              <div>
                <p className={styles.failureTitle}>{writeFailure.title}</p>
                <p className={styles.failureDetail}>{writeFailure.detail}</p>
              </div>
            </div>
          ) : null}

          {adding ? (
            <div className={styles.panel}>
              <AddMemberForm
                busy={members.pending === 'member-add'}
                onDirty={members.dismiss}
                onCancel={() => {
                  setAdding(false)
                }}
                onAdd={(payload) => {
                  void members.addMember(payload).then((accepted) => {
                    if (accepted) {
                      setAdding(false)
                    }
                  })
                }}
              />
            </div>
          ) : null}

          {confirmingRemoval !== null ? (
            <div className={styles.confirm} role="group" aria-label="Confirm revoking access">
              <p className={styles.confirmQuestion}>
                Revoke <strong>{confirmingRemoval.email}</strong>&rsquo;s access to this
                property? Their account is not deleted, and any access they have at other
                properties is unaffected.
              </p>
              {confirmingRemoval.user_public_id === user?.public_id ? (
                <p className={styles.confirmWarning}>
                  This is your own membership. Removing it means losing access to this
                  property, including this page &mdash; and you could not restore it yourself.
                </p>
              ) : null}
              <div className={styles.confirmActions}>
                <Button
                  autoFocus
                  variant="danger"
                  size="sm"
                  disabled={members.pending !== null}
                  onClick={() => {
                    const target = confirmingRemoval.user_public_id
                    setConfirmingRemoval(null)
                    void members.removeMember(target)
                  }}
                >
                  Yes, revoke access
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={members.pending !== null}
                  onClick={() => {
                    setConfirmingRemoval(null)
                  }}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : null}

          {hotelContext.status === 'empty' ? (
            <StateMessage
              icon={Building2}
              tone="status"
              title="No hotel is linked to your account"
              detail="Membership is administered per property, and this account is not yet a member of one. An owner can add you to a hotel from its member list."
            />
          ) : listFailure ? (
            <StateMessage
              icon={members.error?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
              tone="alert"
              title={listFailure.title}
              detail={listFailure.detail}
              {...(listFailure.canRetry ? { onRetry: members.reload } : {})}
            />
          ) : members.status === 'loading' || members.status === 'idle' ? (
            <div className={styles.skeleton} role="status" aria-busy="true">
              <Skeleton height="10rem" label="Loading the member list" />
            </div>
          ) : members.members.length === 0 ? (
            <StateMessage
              icon={UserPlus}
              tone="status"
              title="No members on this page"
              detail="A property always keeps at least one owner, so an empty list means this page is past the end. Return to the first page."
            />
          ) : (
            <>
              <p className={styles.count} role="status">
                {formatCount(members.total)}{' '}
                {members.total === 1 ? 'member' : 'members'}. Counted by the server across
                every page.
              </p>
              <MemberList
                members={members.members}
                timeZone={timeZone}
                currentUserPublicId={user?.public_id ?? null}
                busy={members.pending !== null}
                busyMember={members.pendingMember}
                onRoleChange={(member: Member, role: HotelRole) => {
                  members.dismiss()
                  setConfirmingRemoval(null)
                  void members.changeRole(member.user_public_id, { role })
                }}
                onRemove={(member: Member) => {
                  members.dismiss()
                  setConfirmingRemoval(member)
                }}
              />
              <Pagination
                page={members.page}
                pageSize={members.pageSize}
                total={members.total}
                pages={members.pages}
                onPageChange={members.setPage}
                onPageSizeChange={members.setPageSize}
                pageSizeOptions={PAGE_SIZE_OPTIONS}
                itemNoun={{ singular: 'member', plural: 'members' }}
              />
              <dl className={styles.legend}>
                {(Object.keys(ROLE_DESCRIPTIONS) as HotelRole[]).map((role) => (
                  <div key={role} className={styles.legendRow}>
                    <dt className={styles.legendTerm}>{role}</dt>
                    <dd className={styles.legendDetail}>{ROLE_DESCRIPTIONS[role]}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
        </section>
      </div>
    </PageContainer>
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
