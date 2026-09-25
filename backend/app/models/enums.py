"""Status vocabularies.

Approved decision 3: statuses are stored as ``TEXT`` guarded by a ``CHECK`` constraint rather
than as native PostgreSQL ``ENUM`` types. Adding a value is then a one-line constraint change
in a migration, where altering a native enum -- especially removing or reordering values --
is considerably more painful.

These classes are the single source of truth for both the Python side and the generated CHECK
constraints, so the two can never drift apart.
"""

from __future__ import annotations

from enum import StrEnum


class _Vocabulary(StrEnum):
    """A string enum that can render itself as a SQL ``IN`` list."""

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(member.value for member in cls)

    @classmethod
    def sql_in_list(cls) -> str:
        """``'a', 'b', 'c'`` -- for embedding in a CHECK constraint."""
        return ", ".join(f"'{value}'" for value in cls.values())


class BookingStatus(_Vocabulary):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


#: The statuses that hold physical room inventory (approved decisions 6-8).
#:
#: ``pending`` does NOT hold inventory, so an abandoned checkout never blocks a room.
#: ``cancelled``, ``no_show`` and ``checked_out`` all release it. This tuple is the single
#: definition used by the exclusion constraint's WHERE clause; changing it here changes the
#: constraint in the next migration.
INVENTORY_HOLDING_STATUSES: tuple[str, ...] = (
    BookingStatus.CONFIRMED.value,
    BookingStatus.CHECKED_IN.value,
)

#: Statuses that mean a room was physically occupied, for occupancy analytics.
#:
#: Deliberately WIDER than INVENTORY_HOLDING_STATUSES: a completed stay no longer blocks
#: future bookings, but it certainly counted as occupied on the nights it covered. Conflating
#: the two would erase every completed stay from historical occupancy.
OCCUPANCY_STATUSES: tuple[str, ...] = (
    BookingStatus.CONFIRMED.value,
    BookingStatus.CHECKED_IN.value,
    BookingStatus.CHECKED_OUT.value,
)


#: The booking lifecycle, as an explicit transition graph (Stage 4.5.7).
#:
#: The authoritative answer to "given current status X, may it become Y?". It lives beside the
#: vocabulary it governs and beside the two groupings above, because all three are facts about
#: what a booking status MEANS -- and a rule kept anywhere else would drift from the enum it
#: constrains. Enforcement is the service's job; deciding is this table's.
#:
#: Read as: pending is the only non-committal state; confirmed is the hinge, from which a stay
#: either happens (checked_in) or does not (cancelled, no_show); a stay that happened ends at
#: checked_out. Three states are terminal.
#:
#: Every status maps to itself. A PATCH is expected to be idempotent, and a client retrying a
#: request it already made -- a dropped response, a proxy retry -- must not receive a conflict
#: for asking for the state the booking is already in. Idempotence is not a transition; nothing
#: about the booking changes.
#:
#: Two deliberate refusals worth naming, because both are reasonable to revisit:
#:
#: * ``confirmed -> checked_out`` is refused. A guest who checked out first checked in, and
#:   letting the API skip the arrival would put a stay in the occupancy figures that the
#:   property never recorded anyone arriving for.
#: * ``pending -> no_show`` is refused. A no-show is a guest who held a reservation and did not
#:   arrive; a pending booking holds no room (see INVENTORY_HOLDING_STATUSES) and was never
#:   promised anything, so the honest disposal of one is ``cancelled``.
BOOKING_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    BookingStatus.PENDING.value: frozenset(
        {
            BookingStatus.PENDING.value,
            BookingStatus.CONFIRMED.value,
            BookingStatus.CANCELLED.value,
        }
    ),
    BookingStatus.CONFIRMED.value: frozenset(
        {
            BookingStatus.CONFIRMED.value,
            BookingStatus.CHECKED_IN.value,
            BookingStatus.CANCELLED.value,
            BookingStatus.NO_SHOW.value,
        }
    ),
    BookingStatus.CHECKED_IN.value: frozenset(
        {
            BookingStatus.CHECKED_IN.value,
            BookingStatus.CHECKED_OUT.value,
        }
    ),
    # --- terminal: each accepts only itself ---------------------------------------------
    BookingStatus.CHECKED_OUT.value: frozenset({BookingStatus.CHECKED_OUT.value}),
    BookingStatus.CANCELLED.value: frozenset({BookingStatus.CANCELLED.value}),
    BookingStatus.NO_SHOW.value: frozenset({BookingStatus.NO_SHOW.value}),
}

#: The statuses whose stay may still be rewritten (Stage 4.5.11).
#:
#: ``pending`` holds no inventory and nothing has happened yet. ``confirmed`` is the case the
#: feature exists for: a guest who wants different dates before they arrive.
#:
#: **``checked_in`` is deliberately excluded**, and this is a refusal rather than an oversight.
#: A checked-in stay is physically in progress: its check-in date is in the past, some of its
#: nights have already been consumed, and moving its room means moving a person. Extending a
#: departing guest by two nights is a real and common operation, but it is a DIFFERENT one --
#: it may only push ``check_out`` outward, may not touch nights already stayed, and may not
#: reassign the room. Answering it with a general "replace the whole stay" operation would let
#: a client rewrite history that already happened. It is named in the backlog instead.
#:
#: The three terminal statuses are excluded for the reason they are terminal: a stay that was
#: cancelled, no-showed or checked out is a record of what happened, and editing it would
#: reopen a booking the state machine deliberately closed.
MODIFIABLE_BOOKING_STATUSES: frozenset[str] = frozenset(
    {BookingStatus.PENDING.value, BookingStatus.CONFIRMED.value}
)

#: The one status whose stay may be EXTENDED in place (Stage 4.5.27).
#:
#: Deliberately disjoint from MODIFIABLE_BOOKING_STATUSES, which is the whole point.
#: That set answers "may this stay be replaced?" and excludes ``checked_in`` because a
#: guest is living in it. This one answers a narrower question -- "may this stay be made
#: LONGER at the far end?" -- and admits exactly the case the other refuses.
#:
#: The two are alternatives, not a hierarchy. A checked-in stay may only be pushed
#: outward: its check-in is in the past, some of its nights have already been slept, and
#: its room holds a person's belongings. So an extension may not move ``check_in``, may
#: not reassign a room, may not shorten, and may not touch a rate already charged. Every
#: one of those is a thing the replace-the-whole-stay operation can do, which is why this
#: is a different operation with a different status policy rather than a wider one.
#:
#: ``pending`` and ``confirmed`` are absent because they are not in-house: a guest who has
#: not arrived can have the whole stay restated, which is strictly more capable. The three
#: terminal statuses are absent for the reason they are terminal.
EXTENDABLE_BOOKING_STATUSES: frozenset[str] = frozenset({BookingStatus.CHECKED_IN.value})

#: A status a booking can never leave. Derived from the table rather than restated, so the two
#: cannot disagree: a state is terminal exactly when its only permitted target is itself.
TERMINAL_BOOKING_STATUSES: frozenset[str] = frozenset(
    status for status, allowed in BOOKING_STATUS_TRANSITIONS.items() if allowed == {status}
)


def is_booking_transition_allowed(current: str, requested: str) -> bool:
    """Whether a booking in *current* status may be moved to *requested*.

    An unknown *current* is refused rather than waved through. The CHECK constraint on the
    column makes one unreachable today, but a policy that defaults to "allow" when it does not
    recognise the input is the wrong default for the thing standing between a client and the
    booking lifecycle.
    """
    return requested in BOOKING_STATUS_TRANSITIONS.get(current, frozenset())


class RoomStatus(_Vocabulary):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    CLEANING = "cleaning"
    MAINTENANCE = "maintenance"
    OUT_OF_ORDER = "out_of_order"


#: Room statuses that take a room off sale entirely (Stage 4.5.10).
#:
#: ``RoomStatus`` is HOUSEKEEPING state -- what is true of the room right now -- and carries no
#: dates, which makes using it for a future stay window a judgement call rather than a lookup.
#: The partition drawn here is the conservative one:
#:
#: * ``maintenance`` and ``out_of_order`` mean the room is not sellable, and a search that
#:   offered one would oversell. Excluded.
#: * ``cleaning`` and ``occupied`` are same-day transients that say nothing about next March,
#:   and ``occupied`` is in any case implied by the allocation the search already checks.
#:   Not excluded.
#:
#: The limitation this leaves is real and deliberate rather than hidden: a room out of order
#: today is excluded from a search for next year, because the schema records no date range for
#: a room being out of service. Erring towards refusing to sell is the safe direction; the fix
#: is a room-status history, which is a schema change and not this stage's.
OUT_OF_SERVICE_ROOM_STATUSES: tuple[str, ...] = (
    RoomStatus.MAINTENANCE.value,
    RoomStatus.OUT_OF_ORDER.value,
)


class BookingSource(_Vocabulary):
    DIRECT = "direct"
    WEBSITE = "website"
    PHONE = "phone"
    WALK_IN = "walk_in"
    BOOKING_COM = "booking_com"
    EXPEDIA = "expedia"
    AIRBNB = "airbnb"
    AGODA = "agoda"
    OTHER = "other"


class PaymentKind(_Vocabulary):
    CHARGE = "charge"
    REFUND = "refund"


class PaymentMethod(_Vocabulary):
    CARD = "card"
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    ONLINE_GATEWAY = "online_gateway"
    OTA_COLLECT = "ota_collect"
    VOUCHER = "voucher"


class PaymentStatus(_Vocabulary):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    CANCELLED = "cancelled"


#: Statuses at which a payment moved no money (Stage 4.5.8).
#:
#: The refund cap has to know which rows represent real money, and the answer has to come from
#: the existing vocabulary rather than a new one. These two are the only statuses that mean the
#: attempt is over and nothing was transferred; every other status -- pending, authorized,
#: captured, refunded, partially_refunded -- represents money either taken or in flight.
#:
#: Counting in-flight rows is the conservative direction and, here, the only safe one:
#: payments are append-only and this application exposes no way to change a payment's status
#: after it is written, so a refund posted as ``pending`` will be ``pending`` forever. If
#: pending refunds did not count against the cap, posting a hundred of them would refund a
#: charge a hundred times over without ever tripping it.
VOIDED_PAYMENT_STATUSES: tuple[str, ...] = (
    PaymentStatus.FAILED.value,
    PaymentStatus.CANCELLED.value,
)


class PaymentState(_Vocabulary):
    """How a booking stands against its ledger (Stage 4.5.9).

    DERIVED, never stored. There is no ``payment_state`` column and this stage does not add
    one: the value is a function of the nightly rates and the payment rows, both of which are
    already authoritative, and persisting it would create a second source of truth that could
    disagree with them the moment either changed.

    Unlike every other vocabulary in this module it constrains no column, so it has no
    matching CHECK.
    """

    UNPAID = "unpaid"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    OVERPAID = "overpaid"


class ReviewSource(_Vocabulary):
    DIRECT = "direct"
    BOOKING_COM = "booking_com"
    TRIPADVISOR = "tripadvisor"
    GOOGLE = "google"
    EXPEDIA = "expedia"
    AIRBNB = "airbnb"
    OTHER = "other"


class HotelRole(_Vocabulary):
    """What a member may do at one hotel (Stage 4.2).

    Totally ordered, which is what makes authorization a rank comparison rather than a
    permission matrix. Four roles over 71 operations is a model a person can hold in their
    head and audit; an explicit permission set would be far more surface to get wrong.

    The ordering is the design's main assumption. If a genuinely non-hierarchical need
    appears -- a night auditor who reads finance but not guests -- the ordering breaks, and
    that is the moment to introduce permissions rather than bend a role into place.
    """

    VIEWER = "viewer"
    STAFF = "staff"
    MANAGER = "manager"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        """Position in the hierarchy. Higher outranks lower."""
        return ROLE_RANK[self]

    def outranks_or_equals(self, required: HotelRole) -> bool:
        return self.rank >= required.rank


#: Declared once, beside the enum, so a new role cannot be added without deciding where it
#: sits. `HotelRole.rank` reads through this rather than relying on declaration order, which
#: would be an invisible dependency.
ROLE_RANK: dict[HotelRole, int] = {
    HotelRole.VIEWER: 0,
    HotelRole.STAFF: 1,
    HotelRole.MANAGER: 2,
    HotelRole.OWNER: 3,
}


class PlatformRole(_Vocabulary):
    """Authority over the resources that belong to no hotel (Stage 4.3).

    Deliberately NOT part of :class:`HotelRole`, and deliberately not ranked against it. The
    two answer different questions -- "what may you do at this property?" against "may you
    maintain what every property shares?" -- and there is no ordering between them: a
    platform administrator is not a very senior owner, and an owner is not a junior platform
    administrator. Putting both in one ordered enum would invite exactly that comparison, and
    the first `>=` written against it would silently grant cross-tenant access.

    One member, because one capability exists. A second would need a decision about whether
    platform roles are ordered at all, which is the moment to make it rather than now.
    """

    PLATFORM_ADMIN = "platform_admin"


class AuditAction(_Vocabulary):
    """What an audit event says happened (Stage 4.5.12).

    A **closed** vocabulary, for the same reason every other vocabulary here is closed: the
    CHECK constraint on ``audit_events.action`` is generated from it, so a value the
    application would not recognise cannot be stored -- not by this code, not by a fixture
    script, and not by an out-of-band INSERT.

    **No public API accepts an action.** Audit rows are written by the services that perform
    the mutations, from the literals below; the only place a client names an action is the
    read filter, where FastAPI validates it against this enum and answers 422 for anything
    else. There is no path by which a caller can invent one.

    Named ``resource.verb`` in the past tense, because an audit event is a record of
    something that already happened and committed. See :data:`PLATFORM_AUDIT_ACTIONS` for the
    three groups that belong to no hotel.
    """

    BOOKING_CREATED = "booking.created"
    BOOKING_STATUS_CHANGED = "booking.status_changed"
    BOOKING_STAY_MODIFIED = "booking.stay_modified"
    #: Stage 4.5.15. The one destructive operation the booking domain offers, and until now the
    #: only one that left no trace. Cancelling a booking is a status change and is already
    #: recorded; DELETE removes the row, its allocations and its priced nights outright, so the
    #: audit event is the only thing that survives to say the stay ever existed.
    #:
    #: Adding it required migration 0009: `ck_audit_events_action_valid` is a closed list, and
    #: the database refused this value with SQLSTATE 23514 until that migration widened it.
    BOOKING_DELETED = "booking.deleted"

    PAYMENT_CREATED = "payment.created"
    PAYMENT_REFUND_CREATED = "payment.refund_created"

    MEMBERSHIP_CREATED = "membership.created"
    MEMBERSHIP_ROLE_CHANGED = "membership.role_changed"
    MEMBERSHIP_REMOVED = "membership.removed"

    AUTH_PASSWORD_CHANGED = "auth.password_changed"

    AMENITY_CREATED = "amenity.created"
    AMENITY_UPDATED = "amenity.updated"
    AMENITY_DELETED = "amenity.deleted"

    REVENUE_CATEGORY_CREATED = "revenue_category.created"
    REVENUE_CATEGORY_UPDATED = "revenue_category.updated"
    REVENUE_CATEGORY_DELETED = "revenue_category.deleted"

    EXPENSE_CATEGORY_CREATED = "expense_category.created"
    EXPENSE_CATEGORY_UPDATED = "expense_category.updated"
    EXPENSE_CATEGORY_DELETED = "expense_category.deleted"

    #: Stage 7.6. A copilot tool was called against one hotel -- successfully or not. The first
    #: action that records a READ rather than a committed change, and the only one: §4.3 of the
    #: V2 architecture requires every tool invocation to be audited, and none of the nineteen
    #: above describes one. Resource type ``tool``, reference the tool's registered name.
    #:
    #: Adding it required migration 0012, which widened `ck_audit_events_action_valid` and
    #: `ck_audit_events_resource_type_valid` by one value each.
    TOOL_INVOKED = "tool.invoked"

    #: Stage 7.9. The knowledge base's three writes: a first version uploaded, a new version
    #: superseding the current one, and the current version withdrawn from retrieval. Resource
    #: type ``document``, reference the affected version's public UUID. Migration 0014 widened
    #: both vocabulary CHECKs, exactly as 0009 and 0012 did.
    DOCUMENT_CREATED = "document.created"
    DOCUMENT_VERSION_CREATED = "document.version_created"
    DOCUMENT_WITHDRAWN = "document.withdrawn"


class AuditResourceType(_Vocabulary):
    """What kind of thing an audit event is about (Stage 4.5.12).

    Separate from :class:`AuditAction` rather than derived from its prefix: the two answer
    different questions, and ``auth.password_changed`` is about a ``user`` -- a prefix split
    would have called it an "auth". A closed vocabulary for the same reason, with its own
    CHECK constraint.
    """

    BOOKING = "booking"
    PAYMENT = "payment"
    MEMBERSHIP = "membership"
    USER = "user"
    AMENITY = "amenity"
    REVENUE_CATEGORY = "revenue_category"
    EXPENSE_CATEGORY = "expense_category"
    #: Stage 7.6, with ``tool.invoked``. The reference is the tool's registered name -- a
    #: literal from the static registry, never the name a model supplied.
    TOOL = "tool"
    #: Stage 7.9, with the three ``document.*`` actions. One version of a hotel document.
    DOCUMENT = "document"


class DocumentStatus(_Vocabulary):
    """Where one document VERSION stands (Stage 7.9). Only ``active`` is retrievable.

    ``active -> superseded`` when a new version is uploaded; ``active -> withdrawn`` when the
    current version is withdrawn. Nothing else, enforced by a trigger as well as the service:
    a version is never edited back to life, and its chunks stay addressable for citations.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


class DocumentLanguage(_Vocabulary):
    """The PostgreSQL text-search configuration a document is indexed and searched under.

    Chosen per document at upload (Stage 7.9 decision): ``english`` and ``greek`` stem, so
    "room" finds "rooms"; ``simple`` does not stem and treats every language alike. Closed,
    because each value is cast to ``regconfig`` inside SQL -- an open set would let a request
    name any configuration the server happens to have. All seven ship with PostgreSQL 18.
    """

    SIMPLE = "simple"
    ENGLISH = "english"
    GREEK = "greek"
    FRENCH = "french"
    GERMAN = "german"
    ITALIAN = "italian"
    SPANISH = "spanish"


#: The actions that belong to NO hotel, and therefore store ``hotel_id IS NULL``.
#:
#: Changing your own password is account state: a user may belong to no property, or to
#: several, and attributing the change to one of them would be an invention. The three global
#: catalogues have no ``hotel_id`` column at all -- that is precisely why only a platform
#: administrator may write them -- so an event about one belongs to every hotel and to none.
#:
#: **The consequence is deliberate and is not hidden**: the hotel-scoped retrieval API cannot
#: return these rows, because there is no hotel to ask under. They are recorded as evidence;
#: exposing them needs a platform-scoped read surface, which is a separate authorization
#: surface and is not this stage's.
PLATFORM_AUDIT_ACTIONS: frozenset[str] = frozenset(
    {
        AuditAction.AUTH_PASSWORD_CHANGED.value,
        AuditAction.AMENITY_CREATED.value,
        AuditAction.AMENITY_UPDATED.value,
        AuditAction.AMENITY_DELETED.value,
        AuditAction.REVENUE_CATEGORY_CREATED.value,
        AuditAction.REVENUE_CATEGORY_UPDATED.value,
        AuditAction.REVENUE_CATEGORY_DELETED.value,
        AuditAction.EXPENSE_CATEGORY_CREATED.value,
        AuditAction.EXPENSE_CATEGORY_UPDATED.value,
        AuditAction.EXPENSE_CATEGORY_DELETED.value,
    }
)

#: The actions a hotel's own audit history is made of. Derived, not restated, so the two
#: cannot disagree about which side of the line an action falls on.
HOTEL_AUDIT_ACTIONS: frozenset[str] = frozenset(AuditAction.values()) - PLATFORM_AUDIT_ACTIONS

#: The keys an audit event's ``details`` payload is allowed to carry.
#:
#: This is the data-minimisation rule, expressed once and enforced at the only place details
#: are written (:meth:`app.services.audit.AuditTrail.record`). Every value stored under these
#: keys is written from a literal in a service, never copied from a request body -- so the
#: set below is the complete list of what an audit row can ever say about a change.
#:
#: What is NOT here is the point: no password, no digest, no token, no Authorization header,
#: no cookie, no card fragment, no processor reference, no guest name, email, phone or
#: address, no request body, no SQL, and no internal BIGINT key.
SAFE_AUDIT_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        # What kind of change, when the action alone does not say.
        "changed_fields",
        # Booking lifecycle. Both values are public API vocabulary the client already sees.
        "old_status",
        "new_status",
        # Booking shape. Counts and dates, never a guest.
        "check_in_date",
        "check_out_date",
        "nights",
        "rooms",
        "reference",
        "status",
        # Money, in the two fields that make an amount meaningful. Rendered as a decimal
        # STRING, never a float, so the audited figure is the figure that was posted.
        "amount",
        "currency",
        "method",
        # Stage 4.5.24. What a stay modification did to the money: two authoritative
        # sums and their difference, as decimal STRINGS. No payment detail -- what was
        # charged or refunded belongs to the payment events, not to this one.
        "previous_amount",
        "new_amount",
        "difference",
        # Stage 4.5.27. The far end of the stay BEFORE an in-house extension moved it.
        # A date, like the two above it, and the only way an audit row can say how much
        # longer the guest stayed rather than merely when they now leave.
        "previous_check_out_date",
        # The charge a refund reverses, named by its public identifier.
        "refunds_public_id",
        # The booking a payment settles, likewise.
        "booking_public_id",
        # Membership. Role names are the four-value public vocabulary.
        "old_role",
        "new_role",
        "role",
        # Catalogue entries, named by the code that is already their public identifier.
        "code",
        # Stage 7.6, tool invocations. `outcome` is one of a closed set of literals written by
        # the invocation service; `error_code` is an AppError's public machine code, the same
        # string an ErrorResponse already shows a client; `duration_ms` is an integer.
        "outcome",
        "error_code",
        "duration_ms",
        # SHA-256 of the canonical JSON of the arguments the model supplied: §4.4's "a hash of
        # the arguments". A fingerprint that lets two identical calls be recognised as such,
        # NOT the arguments themselves -- model output is never stored verbatim. Unrelated to
        # the credential digests the note above excludes: it is not derived from any secret.
        "arguments_sha256",
        # Stage 7.9, document events: the version number the event concerns. An integer the
        # service writes; the document's title and content are never recorded.
        "version",
    }
)


class RecurrenceInterval(_Vocabulary):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
