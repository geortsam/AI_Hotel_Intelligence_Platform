import { useEffect, useState, type ReactNode } from 'react'
import { AlertTriangle, Building2, LayoutGrid, Plus, WifiOff } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { Pagination } from '@/features/bookings/Pagination'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { HotelForm } from '@/features/property/HotelForm'
import { RoomTypeForm } from '@/features/property/RoomTypeForm'
import { RoomTypeList } from '@/features/property/RoomTypeList'
import { usePropertyAdmin } from '@/features/property/usePropertyAdmin'
import { formatCount, formatDateTime, UNAVAILABLE } from '@/lib/format'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useHotelContext } from '@/session/HotelProvider'
import type { RoomType } from '@/types/room'

import styles from './PropertyPage.module.css'

/** Every option is inside the router's own `ge=1, le=100`, so none can produce a 422. */
const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

const HOTEL_LOAD_COPY = {
  notFound: {
    title: 'Property not available',
    detail:
      'This property could not be found, or your access to it has been removed. It answers the same way for both, so there is nothing more specific to say.',
    canRetry: false,
  },
  serverFault: {
    title: 'The property is temporarily unavailable',
    detail: 'Its record could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

const TYPES_LOAD_COPY = {
  notFound: {
    title: 'Room types not available',
    detail: 'This property could not be found, or your access to it has been removed.',
    canRetry: false,
  },
  serverFault: {
    title: 'Room types are temporarily unavailable',
    detail: 'They could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

/** Per-write copy. Each names the role the backend actually requires for that verb. */
const WRITE_COPY = {
  'hotel-update': {
    forbidden: {
      title: 'Nothing was saved',
      detail:
        'Editing a property needs the owner role on it, which this account does not have. Managing its room types needs only manager, so that may still be available.',
      canRetry: false,
    },
    notFound: {
      title: 'Nothing was saved',
      detail: 'This property could not be found. Your access to it may have been removed.',
      canRetry: false,
    },
    conflict: {
      title: 'Nothing was saved',
      detail: 'The change conflicts with existing data. The property is unchanged.',
      canRetry: true,
    },
    serverFault: {
      title: 'Nothing was saved',
      detail: 'The change could not be saved. The property is unchanged.',
      canRetry: true,
    },
  },
  'type-create': {
    forbidden: {
      title: 'Nothing was created',
      detail:
        'Adding a room type needs the manager role on this property, which this account does not have.',
      canRetry: false,
    },
    notFound: {
      title: 'Nothing was created',
      detail: 'This property could not be found, so there was nothing to add the room type to.',
      canRetry: false,
    },
    conflict: {
      title: 'Nothing was created',
      detail:
        'This property already has a room type with that code. A code identifies one type within a property and cannot be reused.',
      canRetry: false,
    },
    serverFault: {
      title: 'Nothing was created',
      detail: 'The room type could not be created. Nothing was written.',
      canRetry: true,
    },
  },
  'type-update': {
    forbidden: {
      title: 'Nothing was saved',
      detail: 'Editing a room type needs the manager role on this property.',
      canRetry: false,
    },
    notFound: {
      title: 'Nothing was saved',
      detail: 'That room type no longer exists at this property.',
      canRetry: false,
    },
    conflict: {
      title: 'Nothing was saved',
      detail:
        'The values conflict with what is stored — most often a standard occupancy above the stored maximum. The room type is unchanged.',
      canRetry: false,
    },
    serverFault: {
      title: 'Nothing was saved',
      detail: 'The change could not be saved. The room type is unchanged.',
      canRetry: true,
    },
  },
  'type-delete': {
    forbidden: {
      title: 'The room type was not deleted',
      detail: 'Deleting a room type needs the manager role on this property.',
      canRetry: false,
    },
    notFound: {
      title: 'The room type was not deleted',
      detail: 'That room type no longer exists at this property.',
      canRetry: false,
    },
    conflict: {
      title: 'The room type was not deleted',
      detail:
        'Rooms are still assigned to it, and no cascade is performed. Move or remove those rooms first, or withdraw the type from sale instead — that keeps its rooms and their history.',
      canRetry: false,
    },
    serverFault: {
      title: 'The room type was not deleted',
      detail: 'It could not be removed. It is unchanged.',
      canRetry: true,
    },
  },
} as const

/**
 * Property &mdash; the hotel's own record and its room types.
 *
 * ## Both CRUD surfaces exist, and their authorization differs
 *
 * The hotel's `PATCH` and `DELETE` require the **owner** role; its room types require
 * **manager**. That asymmetry is real and is reflected in the copy: a refusal on the
 * property says the room types may still be editable, because they may be.
 *
 * Hotel `DELETE` exists in the API and is **not offered here**. It is refused whenever any
 * room type, room, guest, booking, revenue line or expense references the property -- all
 * `ON DELETE RESTRICT`, verified against a seeded hotel -- so for any property with
 * operating history it can only ever fail. Deactivating is the operation that actually
 * achieves what deleting is usually reached for, and that is on the form. Hotel *creation*
 * is likewise absent: it is an onboarding action that makes the caller an owner, not
 * something a property's own settings screen does.
 *
 * ## Nothing here computes anything
 *
 * Occupancies, bed counts, sizes and base prices are stored columns shown as they arrive. No
 * availability, no occupancy rate, no revenue. `base_price` is a decimal string formatted
 * for display and never parsed for arithmetic.
 *
 * ## Two requests for the page, and nothing per row
 *
 * The hotel record and one page of room types.
 */
export function PropertyPage() {
  const hotelContext = useHotelContext()
  const selected = hotelContext.selected

  const admin = usePropertyAdmin(selected?.public_id ?? null)
  const [editingHotel, setEditingHotel] = useState(false)
  const [addingType, setAddingType] = useState(false)
  const [editingType, setEditingType] = useState<RoomType | null>(null)
  const [confirmingDelete, setConfirmingDelete] = useState<RoomType | null>(null)

  /* Switching property closes every form and clears its banners: a refusal at one hotel must
   * not sit above a form now aimed at another. */
  useEffect(() => {
    setEditingHotel(false)
    setAddingType(false)
    setEditingType(null)
    setConfirmingDelete(null)
    admin.dismiss()
  }, [selected?.public_id])

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Properties are managed by their own members, and this account is not yet a member of one. An owner can add you to a hotel from its member list."
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

  const hotelFailure =
    admin.hotelError === null ? null : describeFailure(admin.hotelError, HOTEL_LOAD_COPY)
  const typesFailure =
    admin.typesError === null ? null : describeFailure(admin.typesError, TYPES_LOAD_COPY)
  const writeFailure =
    admin.writeError === null || admin.failedWrite === null
      ? null
      : describeFailure(admin.writeError, WRITE_COPY[admin.failedWrite])

  const timeZone = admin.hotel?.timezone ?? selected?.timezone ?? 'UTC'

  return (
    <Frame>
      {admin.saved ? (
        <p className={styles.success} role="status">
          {admin.saved} The records below are the server&rsquo;s own copy.
        </p>
      ) : null}

      {writeFailure ? (
        <div className={styles.failure} role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            <p className={styles.failureTitle}>{writeFailure.title}</p>
            <p className={styles.failureDetail}>{writeFailure.detail}</p>
          </div>
        </div>
      ) : null}

      {/* --- the hotel's own record ------------------------------------------------------ */}

      <section className={styles.block} aria-labelledby="property-record">
        <div className={styles.blockHeader}>
          <h2 className={styles.blockTitle} id="property-record">
            Property details
          </h2>
          {admin.hotel !== null && !editingHotel ? (
            <Button
              variant="secondary"
              size="sm"
              disabled={admin.pending !== null}
              onClick={() => {
                admin.dismiss()
                setEditingHotel(true)
              }}
            >
              Edit property
            </Button>
          ) : null}
        </div>

        {hotelFailure ? (
          <StateMessage
            icon={admin.hotelError?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
            tone="alert"
            title={hotelFailure.title}
            detail={hotelFailure.detail}
            {...(hotelFailure.canRetry ? { onRetry: admin.reload } : {})}
          />
        ) : admin.hotel === null ? (
          <div className={styles.skeleton} role="status" aria-busy="true">
            <Skeleton height="10rem" label="Loading the property" />
          </div>
        ) : editingHotel ? (
          <div className={styles.panel}>
            <HotelForm
              hotel={admin.hotel}
              busy={admin.pending === 'hotel-update'}
              onDirty={admin.dismiss}
              onCancel={() => {
                setEditingHotel(false)
              }}
              onUpdate={(payload) => {
                void admin.updateHotel(payload).then((accepted) => {
                  if (accepted) {
                    setEditingHotel(false)
                  }
                })
              }}
            />
          </div>
        ) : (
          <div className={styles.panel}>
            <div className={styles.recordHeader}>
              <h3 className={styles.recordName}>{admin.hotel.name}</h3>
              <Badge tone={admin.hotel.is_active ? 'success' : 'danger'}>
                {admin.hotel.is_active ? 'Active' : 'Inactive'}
              </Badge>
            </div>
            <dl className={styles.fields}>
              <Field label="Slug">{admin.hotel.slug}</Field>
              <Field label="Address">
                {[
                  admin.hotel.address_line1,
                  admin.hotel.address_line2,
                  admin.hotel.city,
                  admin.hotel.region,
                  admin.hotel.postal_code,
                  admin.hotel.country_code,
                ]
                  .filter((part) => part !== null && part !== '')
                  .join(', ')}
              </Field>
              <Field label="Time zone">{admin.hotel.timezone}</Field>
              <Field label="Currency">{admin.hotel.currency}</Field>
              <Field label="Star rating">
                {admin.hotel.star_rating === null ? (
                  <span className={styles.absent}>{UNAVAILABLE}</span>
                ) : (
                  `${formatCount(admin.hotel.star_rating)} of 5`
                )}
              </Field>
              <Field label="Email">
                {admin.hotel.email ?? <span className={styles.absent}>Not recorded</span>}
              </Field>
              <Field label="Phone">
                {admin.hotel.phone ?? <span className={styles.absent}>Not recorded</span>}
              </Field>
              <Field label="Website">
                {admin.hotel.website ?? <span className={styles.absent}>Not recorded</span>}
              </Field>
              <Field label="Coordinates">
                {admin.hotel.latitude === null || admin.hotel.longitude === null ? (
                  <span className={styles.absent}>Not recorded</span>
                ) : (
                  `${admin.hotel.latitude}, ${admin.hotel.longitude}`
                )}
              </Field>
              <Field label="Created">{formatDateTime(admin.hotel.created_at, timeZone)}</Field>
              <Field label="Last updated">
                {formatDateTime(admin.hotel.updated_at, timeZone)}
              </Field>
            </dl>
          </div>
        )}
      </section>

      {/* --- its room types --------------------------------------------------------------- */}

      <section className={styles.block} aria-labelledby="property-types">
        <div className={styles.blockHeader}>
          <div>
            <h2 className={styles.blockTitle} id="property-types">
              Room types
            </h2>
            <p className={styles.blockNote}>
              A room type defines what a room is and what it lists for. Its code is part of
              every room&rsquo;s address, so it cannot be changed once created.
            </p>
          </div>
          {addingType || editingType !== null ? null : (
            <Button
              variant="primary"
              size="sm"
              disabled={admin.pending !== null}
              onClick={() => {
                admin.dismiss()
                setAddingType(true)
              }}
            >
              <Plus size={16} aria-hidden="true" />
              Add room type
            </Button>
          )}
        </div>

        {addingType ? (
          <div className={styles.panel}>
            <RoomTypeForm
              defaultCurrency={admin.hotel?.currency ?? ''}
              busy={admin.pending === 'type-create'}
              onDirty={admin.dismiss}
              onCancel={() => {
                setAddingType(false)
              }}
              onCreate={(payload) => {
                void admin.createType(payload).then((accepted) => {
                  if (accepted) {
                    setAddingType(false)
                  }
                })
              }}
            />
          </div>
        ) : null}

        {editingType !== null ? (
          <div className={styles.panel}>
            <RoomTypeForm
              roomType={editingType}
              defaultCurrency={admin.hotel?.currency ?? ''}
              busy={admin.pending === 'type-update'}
              onDirty={admin.dismiss}
              onCancel={() => {
                setEditingType(null)
              }}
              onUpdate={(payload) => {
                void admin.updateType(editingType.code, payload).then((accepted) => {
                  if (accepted) {
                    setEditingType(null)
                  }
                })
              }}
            />
          </div>
        ) : null}

        {confirmingDelete !== null ? (
          <div className={styles.confirm} role="group" aria-label="Confirm deleting this room type">
            <p className={styles.confirmQuestion}>
              Delete room type <strong>{confirmingDelete.code}</strong> (
              {confirmingDelete.name})? This cannot be undone, and it is refused while any
              room is still assigned to it.
            </p>
            <div className={styles.confirmActions}>
              <Button
                autoFocus
                variant="danger"
                size="sm"
                disabled={admin.pending !== null}
                onClick={() => {
                  const code = confirmingDelete.code
                  setConfirmingDelete(null)
                  void admin.deleteType(code)
                }}
              >
                Yes, delete this room type
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={admin.pending !== null}
                onClick={() => {
                  setConfirmingDelete(null)
                }}
              >
                Cancel
              </Button>
            </div>
          </div>
        ) : null}

        {typesFailure ? (
          <StateMessage
            icon={admin.typesError?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
            tone="alert"
            title={typesFailure.title}
            detail={typesFailure.detail}
            {...(typesFailure.canRetry ? { onRetry: admin.reload } : {})}
          />
        ) : admin.typesStatus === 'loading' || admin.typesStatus === 'idle' ? (
          <div className={styles.skeleton} role="status" aria-busy="true">
            <Skeleton height="10rem" label="Loading room types" />
          </div>
        ) : admin.types.length === 0 ? (
          <StateMessage
            icon={LayoutGrid}
            tone="status"
            title="No room types yet"
            detail="A property needs at least one room type before it can have rooms — a room is created under the type it belongs to."
          />
        ) : (
          <>
            <p className={styles.count} role="status">
              {formatCount(admin.total)} room {admin.total === 1 ? 'type' : 'types'}. Counted by
              the server across every page.
            </p>
            <RoomTypeList
              types={admin.types}
              timeZone={timeZone}
              busy={admin.pending !== null}
              busyCode={admin.pending === 'type-delete' ? (confirmingDelete?.code ?? null) : null}
              onEdit={(type) => {
                admin.dismiss()
                setAddingType(false)
                setEditingType(type)
              }}
              onDelete={(type) => {
                admin.dismiss()
                setConfirmingDelete(type)
              }}
            />
            <Pagination
              page={admin.page}
              pageSize={admin.pageSize}
              total={admin.total}
              pages={admin.pages}
              onPageChange={admin.setPage}
              onPageSizeChange={admin.setPageSize}
              pageSizeOptions={PAGE_SIZE_OPTIONS}
              itemNoun={{ singular: 'room type', plural: 'room types' }}
            />
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

/** The page heading, shared by every state so the frame never jumps. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Property"
        description="This property’s own record and the room types it sells. Editing the property needs the owner role; its room types need manager."
      />
      <div className={styles.page}>{children}</div>
    </PageContainer>
  )
}
