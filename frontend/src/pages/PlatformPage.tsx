import { useEffect, useId, useState } from 'react'
import { AlertTriangle, Globe, Plus, ScrollText, ShieldAlert, WifiOff } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { Pagination } from '@/features/bookings/Pagination'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { AuditTrail } from '@/features/platform/AuditTrail'
import { CatalogueForm } from '@/features/platform/CatalogueForm'
import { CatalogueList } from '@/features/platform/CatalogueList'
import { useCatalogue, type CatalogueRow } from '@/features/platform/useCatalogue'
import { usePlatformAudit } from '@/features/platform/usePlatformAudit'
import { formatCount } from '@/lib/format'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useHotelContext } from '@/session/HotelProvider'
import {
  PLATFORM_AUDIT_ACTIONS,
  PLATFORM_AUDIT_RESOURCE_TYPES,
  type CatalogueKind,
} from '@/types/platform'

import styles from './PlatformPage.module.css'

/** Every option is inside each router's own `ge=1, le=100`, so none can produce a 422. */
const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

const TABS: readonly { readonly kind: CatalogueKind; readonly label: string }[] = [
  { kind: 'amenities', label: 'Amenities' },
  { kind: 'revenue-categories', label: 'Revenue categories' },
  { kind: 'expense-categories', label: 'Expense categories' },
]

const NOUN: Readonly<Record<CatalogueKind, string>> = {
  amenities: 'amenity',
  'revenue-categories': 'revenue category',
  'expense-categories': 'expense category',
}

const LIST_COPY = {
  notFound: {
    title: 'Catalogue not available',
    detail: 'This catalogue could not be found.',
    canRetry: false,
  },
  serverFault: {
    title: 'The catalogue is temporarily unavailable',
    detail: 'It could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

/**
 * The audit trail's own failure copy.
 *
 * Its 403 is the one worth writing carefully: it is not a fault, it is the ordinary answer
 * for the great majority of operators, and it must not read as though something broke.
 */
const AUDIT_COPY = {
  forbidden: {
    title: 'The platform audit trail needs a platform administrator grant',
    detail:
      'This is a separate authority from any hotel role — an owner is not a junior platform administrator, and no hotel membership grants it. The catalogues above remain readable.',
    canRetry: false,
  },
  serverFault: {
    title: 'The audit trail is temporarily unavailable',
    detail: 'It could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

/** Per-write copy. Every write on this screen needs the platform administrator grant. */
const WRITE_COPY = {
  create: {
    forbidden: {
      title: 'Nothing was created',
      detail:
        'Writing a shared catalogue needs a platform administrator grant, which this account does not have. It is a separate authority from any hotel role, and no hotel role can grant it.',
      canRetry: false,
    },
    conflict: {
      title: 'Nothing was created',
      detail:
        'That code is already in use. A code identifies one entry across the whole installation and cannot be reused.',
      canRetry: false,
    },
    serverFault: {
      title: 'Nothing was created',
      detail: 'The entry could not be created. Nothing was written.',
      canRetry: true,
    },
  },
  update: {
    forbidden: {
      title: 'Nothing was saved',
      detail:
        'Writing a shared catalogue needs a platform administrator grant, which this account does not have.',
      canRetry: false,
    },
    notFound: {
      title: 'Nothing was saved',
      detail: 'That entry no longer exists in this catalogue.',
      canRetry: false,
    },
    conflict: {
      title: 'Nothing was saved',
      detail: 'The change conflicts with existing data. The entry is unchanged.',
      canRetry: false,
    },
    serverFault: {
      title: 'Nothing was saved',
      detail: 'The change could not be saved. The entry is unchanged.',
      canRetry: true,
    },
  },
  delete: {
    forbidden: {
      title: 'The entry was not deleted',
      detail:
        'Writing a shared catalogue needs a platform administrator grant, which this account does not have.',
      canRetry: false,
    },
    notFound: {
      title: 'The entry was not deleted',
      detail: 'That entry no longer exists in this catalogue.',
      canRetry: false,
    },
    conflict: {
      title: 'The entry was not deleted',
      detail:
        'Something still references it — a room type, or a ledger line — and no cascade is performed. Retire it instead: that keeps every record that already points at it.',
      canRetry: false,
    },
    serverFault: {
      title: 'The entry was not deleted',
      detail: 'It could not be removed. It is unchanged.',
      canRetry: true,
    },
  },
} as const

/**
 * Platform &mdash; the catalogues every property shares, and the trail of changes to them.
 *
 * ## This screen is not hotel administration, and says so in every register
 *
 * Amenities, revenue categories and expense categories have **no `hotel_id`** and their codes
 * are unique across the whole installation. Nothing here is scoped by the hotel picker, and
 * the page states that rather than leaving an operator to infer it from an absence. Stage
 * 5.14's Administration screen is the hotel-scoped one; this is its counterpart.
 *
 * ## The authority is different in kind, not in degree
 *
 * Reading a catalogue needs only a session, because every hotel must be able to reference the
 * vocabulary it is required to use. Writing one needs a **platform administrator grant**,
 * held in its own table, read from the database on every request, and deliberately not ranked
 * against `HotelRole`: a platform administrator is not a very senior owner. The frontend is
 * told neither, so it offers the documented control and renders the server's 403.
 *
 * ## What this screen deliberately does not offer
 *
 * * **Granting or revoking platform administrator.** No endpoint exists -- the repository
 *   behind the check is read-only and the grant is provisioned out of band. A control here
 *   would be a button with nothing behind it.
 * * **Editing or deleting an audit event.** The trail is append-only by design, which is what
 *   makes it worth reading.
 * * **Searching or sorting any catalogue.** Neither exists on any of the three routes, and
 *   the backend ignores an unknown parameter silently, so an invented control would look like
 *   it worked.
 */
export function PlatformPage() {
  const hotelContext = useHotelContext()
  const [kind, setKind] = useState<CatalogueKind>('amenities')
  const [showAudit, setShowAudit] = useState(false)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<CatalogueRow | null>(null)
  const [confirmingDelete, setConfirmingDelete] = useState<CatalogueRow | null>(null)

  const catalogue = useCatalogue(kind)
  const audit = usePlatformAudit(showAudit)

  const activeFilterId = useId()
  const actionFilterId = useId()
  const resourceFilterId = useId()

  /* Switching catalogue closes every form: a create aimed at amenities must not submit
   * itself against expense categories because a tab changed underneath it. */
  useEffect(() => {
    setCreating(false)
    setEditing(null)
    setConfirmingDelete(null)
  }, [kind])

  const listFailure =
    catalogue.error === null ? null : describeFailure(catalogue.error, LIST_COPY)
  const writeFailure =
    catalogue.writeError === null || catalogue.failedWrite === null
      ? null
      : describeFailure(catalogue.writeError, WRITE_COPY[catalogue.failedWrite])
  const auditFailure = audit.error === null ? null : describeFailure(audit.error, AUDIT_COPY)

  /* Timestamps in the trail belong to no property, so there is no property timezone to
   * resolve them in. The selected hotel's zone is used as the operator's own working zone --
   * it is the one every other screen in this session shows times in -- and the column says
   * so rather than implying the event happened "at" that hotel. */
  const timeZone = hotelContext.selected?.timezone ?? 'UTC'

  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Platform"
        description="The catalogues every property shares, and the record of changes to them. Nothing on this page is scoped to a hotel; reading needs only an account, and writing needs a platform administrator grant."
      />

      <div className={styles.page}>
        <section className={styles.block} aria-labelledby="platform-catalogues">
          <div className={styles.blockHeader}>
            <div>
              <h2 className={styles.blockTitle} id="platform-catalogues">
                Shared catalogues
              </h2>
              <p className={styles.blockNote}>
                These entries belong to no property. A code means the same thing across the
                whole installation, which is why it cannot be changed once created.
              </p>
            </div>
            {creating || editing !== null ? null : (
              <Button
                variant="primary"
                size="sm"
                disabled={catalogue.pending !== null}
                onClick={() => {
                  catalogue.dismiss()
                  setCreating(true)
                }}
              >
                <Plus size={16} aria-hidden="true" />
                Add {NOUN[kind]}
              </Button>
            )}
          </div>

          <div className={styles.tabs} role="tablist" aria-label="Shared catalogues">
            {TABS.map((tab) => (
              <button
                key={tab.kind}
                type="button"
                role="tab"
                id={`tab-${tab.kind}`}
                aria-selected={kind === tab.kind}
                aria-controls={`panel-${tab.kind}`}
                className={`${styles.tab} ${kind === tab.kind ? styles.tabCurrent : ''}`}
                onClick={() => {
                  setKind(tab.kind)
                }}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <div
            className={styles.panelBody}
            role="tabpanel"
            id={`panel-${kind}`}
            aria-labelledby={`tab-${kind}`}
          >
            {catalogue.saved ? (
              <p className={styles.success} role="status">
                {catalogue.saved} The list below is the server&rsquo;s own copy.
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

            {creating || editing !== null ? (
              <div className={styles.panel}>
                <CatalogueForm
                  kind={kind}
                  {...(editing === null ? {} : { entry: editing })}
                  busy={catalogue.pending === (editing === null ? 'create' : 'update')}
                  onDirty={catalogue.dismiss}
                  onCancel={() => {
                    setCreating(false)
                    setEditing(null)
                  }}
                  onSubmit={(payload) => {
                    const write =
                      editing === null
                        ? catalogue.create(payload)
                        : catalogue.update(editing.code, payload)
                    void write.then((accepted) => {
                      if (accepted) {
                        setCreating(false)
                        setEditing(null)
                      }
                    })
                  }}
                />
              </div>
            ) : null}

            {confirmingDelete !== null ? (
              <div className={styles.confirm} role="group" aria-label="Confirm deleting this entry">
                <p className={styles.confirmQuestion}>
                  Delete <strong>{confirmingDelete.code}</strong> ({confirmingDelete.name}) from
                  the shared catalogue? It is removed for <strong>every property</strong>, and
                  the request is refused while anything still references it.
                </p>
                <div className={styles.confirmActions}>
                  <Button
                    autoFocus
                    variant="danger"
                    size="sm"
                    disabled={catalogue.pending !== null}
                    onClick={() => {
                      const code = confirmingDelete.code
                      setConfirmingDelete(null)
                      void catalogue.remove(code)
                    }}
                  >
                    Yes, delete from every property
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={catalogue.pending !== null}
                    onClick={() => {
                      setConfirmingDelete(null)
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            ) : null}

            {/* The server's own filter, offered only where the server has one. */}
            {catalogue.supportsActiveFilter ? (
              <div className={styles.filters}>
                <label className={styles.filterLabel} htmlFor={activeFilterId}>
                  Filter by status
                </label>
                <select
                  id={activeFilterId}
                  className={styles.select}
                  value={
                    catalogue.activeFilter === undefined
                      ? 'all'
                      : catalogue.activeFilter
                        ? 'active'
                        : 'retired'
                  }
                  onChange={(event) => {
                    const value = event.target.value
                    catalogue.setActiveFilter(
                      value === 'all' ? undefined : value === 'active',
                    )
                  }}
                >
                  <option value="all">All</option>
                  <option value="active">Active</option>
                  <option value="retired">Retired</option>
                </select>
              </div>
            ) : null}

            {listFailure ? (
              <StateMessage
                icon={catalogue.error?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
                tone="alert"
                title={listFailure.title}
                detail={listFailure.detail}
                {...(listFailure.canRetry ? { onRetry: catalogue.reload } : {})}
              />
            ) : catalogue.status === 'loading' || catalogue.status === 'idle' ? (
              <div className={styles.skeleton} role="status" aria-busy="true">
                <Skeleton height="10rem" label="Loading the catalogue" />
              </div>
            ) : catalogue.rows.length === 0 ? (
              <StateMessage
                icon={Globe}
                tone="status"
                title="This catalogue is empty"
                detail={`No ${NOUN[kind]} has been created yet. Entries here are shared by every property, and creating one needs a platform administrator grant.`}
              />
            ) : (
              <>
                <p className={styles.count} role="status">
                  {formatCount(catalogue.total)}{' '}
                  {catalogue.total === 1 ? 'entry' : 'entries'}. Counted by the server across
                  every page.
                </p>
                <CatalogueList
                  kind={kind}
                  rows={catalogue.rows}
                  busy={catalogue.pending !== null}
                  busyCode={catalogue.pendingCode}
                  onEdit={(row) => {
                    catalogue.dismiss()
                    setCreating(false)
                    setEditing(row)
                  }}
                  onDelete={(row) => {
                    catalogue.dismiss()
                    setConfirmingDelete(row)
                  }}
                />
                <Pagination
                  page={catalogue.page}
                  pageSize={catalogue.pageSize}
                  total={catalogue.total}
                  pages={catalogue.pages}
                  onPageChange={catalogue.setPage}
                  onPageSizeChange={catalogue.setPageSize}
                  pageSizeOptions={PAGE_SIZE_OPTIONS}
                  itemNoun={{ singular: 'entry', plural: 'entries' }}
                />
              </>
            )}
          </div>
        </section>

        {/* --- the platform audit trail ----------------------------------------------- */}

        <section className={styles.block} aria-labelledby="platform-audit">
          <div className={styles.blockHeader}>
            <div>
              <h2 className={styles.blockTitle} id="platform-audit">
                Platform audit trail
              </h2>
              <p className={styles.blockNote}>
                Password changes, and every change to the three catalogues above. Append-only:
                no endpoint edits or deletes an event. Reading it needs a platform
                administrator grant.
              </p>
            </div>
            {showAudit ? null : (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setShowAudit(true)
                }}
              >
                <ScrollText size={16} aria-hidden="true" />
                Load audit trail
              </Button>
            )}
          </div>

          {!showAudit ? (
            <p className={styles.blockNote}>
              Not loaded yet. It is fetched on request rather than with the page, because the
              account viewing this may hold no grant and the answer would only be a refusal.
            </p>
          ) : auditFailure ? (
            <StateMessage
              icon={audit.error?.isForbidden ? ShieldAlert : AlertTriangle}
              tone={audit.error?.isForbidden ? 'status' : 'alert'}
              title={auditFailure.title}
              detail={auditFailure.detail}
              {...(auditFailure.canRetry ? { onRetry: audit.reload } : {})}
            />
          ) : audit.status === 'loading' || audit.status === 'idle' ? (
            <div className={styles.skeleton} role="status" aria-busy="true">
              <Skeleton height="10rem" label="Loading the audit trail" />
            </div>
          ) : (
            <>
              <div className={styles.filters}>
                <label className={styles.filterLabel} htmlFor={actionFilterId}>
                  Filter by action
                </label>
                <select
                  id={actionFilterId}
                  className={styles.select}
                  value={audit.action ?? 'all'}
                  onChange={(event) => {
                    const value = event.target.value
                    audit.setAction(value === 'all' ? undefined : value)
                  }}
                >
                  <option value="all">All actions</option>
                  {PLATFORM_AUDIT_ACTIONS.map((action) => (
                    <option key={action} value={action}>
                      {action}
                    </option>
                  ))}
                </select>

                <label className={styles.filterLabel} htmlFor={resourceFilterId}>
                  Filter by resource
                </label>
                <select
                  id={resourceFilterId}
                  className={styles.select}
                  value={audit.resourceType ?? 'all'}
                  onChange={(event) => {
                    const value = event.target.value
                    audit.setResourceType(value === 'all' ? undefined : value)
                  }}
                >
                  <option value="all">All resources</option>
                  {PLATFORM_AUDIT_RESOURCE_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
              </div>

              {audit.events.length === 0 ? (
                <StateMessage
                  icon={ScrollText}
                  tone="status"
                  title="No events match"
                  detail="Nothing platform-scoped has been recorded for this filter. Password changes and catalogue edits appear here as they happen."
                />
              ) : (
                <>
                  <p className={styles.count} role="status">
                    {formatCount(audit.total)} {audit.total === 1 ? 'event' : 'events'}. Times
                    are shown in {timeZone}, your working time zone &mdash; these events belong
                    to no property.
                  </p>
                  <AuditTrail events={audit.events} timeZone={timeZone} />
                  <Pagination
                    page={audit.page}
                    pageSize={audit.pageSize}
                    total={audit.total}
                    pages={audit.pages}
                    onPageChange={audit.setPage}
                    onPageSizeChange={audit.setPageSize}
                    pageSizeOptions={PAGE_SIZE_OPTIONS}
                    itemNoun={{ singular: 'event', plural: 'events' }}
                  />
                </>
              )}
            </>
          )}
        </section>
      </div>
    </PageContainer>
  )
}
