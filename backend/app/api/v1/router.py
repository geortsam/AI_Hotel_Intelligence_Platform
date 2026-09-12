"""Version 1 router.

Aggregates every v1 endpoint module into one router, which the application factory mounts at
``Settings.api_v1_prefix``. Domain routers (hotels, rooms, bookings, ...) are added here as
each is built; none exists yet.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    amenities,
    analytics,
    audit,
    auth,
    availability,
    bookings,
    expense_categories,
    expenses,
    guests,
    hotels,
    intelligence,
    members,
    meta,
    payments,
    platform_audit,
    revenue,
    revenue_categories,
    reviews,
    room_type_amenities,
    room_types,
    rooms,
)

api_router = APIRouter()

# Authentication. Not hotel-scoped: a user is not owned by a property, and identity precedes
# tenancy. Registered first so it reads as the entry point it is.
api_router.include_router(auth.router)
api_router.include_router(meta.router)
api_router.include_router(hotels.router)
# Who may reach a hotel. Nested under it because a membership belongs to one property even
# though the account it names is global; a flat /users/{id}/memberships route would answer a
# question no single hotel is entitled to ask.
api_router.include_router(members.router)
# Nested under hotels: a room type is identified by its owning hotel plus its code.
api_router.include_router(room_types.router)
# Nested one level deeper: hotel -> room type -> physical room.
api_router.include_router(rooms.router)
# The amenity catalogue is global: it has no hotel_id, and its code is unique across the
# whole table, so nesting it under a hotel would imply ownership the schema does not model.
api_router.include_router(amenities.router)
# Assignments, by contrast, ARE hierarchy-scoped.
api_router.include_router(room_type_amenities.router)
# Guests hang off a hotel directly: they are hotel-scoped, with no portfolio-wide identity.
api_router.include_router(guests.router)
# Bookings hang off the hotel. Their room allocations and priced nights are part of the
# booking payload, not sub-resources: the deferred night-completeness trigger requires them
# to be written in the same transaction, and one request is one transaction.
api_router.include_router(bookings.router)
# Payments settle a booking, so they nest beneath it. Append-only: POST and GET only.
api_router.include_router(payments.router)
# Reviews are listed per hotel and addressed per stay -- the schema gives them no public_id.
api_router.include_router(reviews.router)
# The financial ledger. The two category lookups are GLOBAL -- no hotel_id on either table --
# so they sit beside /amenities rather than under a hotel. The journals are hotel-scoped.
api_router.include_router(revenue_categories.router)
api_router.include_router(expense_categories.router)
api_router.include_router(revenue.router)
api_router.include_router(expenses.router)
# Read-only analytics over everything above. Hotel-scoped: there is no portfolio-wide route,
# because the hotel segment is where tenant isolation is established.
api_router.include_router(availability.router)
api_router.include_router(analytics.router)
# Statistical forecasting, trend and anomaly detection over the analytics series. Read-only
# and hotel-scoped, like analytics itself; it adds no metric definitions of its own.
api_router.include_router(intelligence.router)
# The audit trail. Hotel-scoped like everything else that is a property's own data, and
# read-only: there is no route that writes, edits or deletes an event, because a history a
# client can append to is not evidence. Registered last because it observes every domain
# above it rather than belonging to any of them.
api_router.include_router(audit.router)
# Stage 4.5.13. The other half of the audit trail: the events that belong to no property, and
# therefore to no hotel-scoped route. NOT nested under /hotels, because there is no hotel to
# nest it under -- these rows have hotel_id IS NULL by definition. Guarded by the platform
# grant alone, so no hotel role reaches it and no membership is consulted.
api_router.include_router(platform_audit.router)

# Further domain routers land here in later stages, one include_router() per aggregate.

__all__ = ["api_router"]
