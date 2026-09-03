"""initial schema

Creates the approved 16-table schema together with the PostgreSQL objects the design depends
on and Alembic cannot autogenerate:

* the ``btree_gist`` extension, required by the room-overlap exclusion constraint;
* a shared ``set_updated_at`` trigger, so ``updated_at`` is maintained by the database on
  every write path rather than by whichever application code happens to remember;
* the deferred ``booking_room_nights`` completeness constraint trigger;
* ``historical_room_overlaps``, a detection view for approved decision 20.

The CREATE TABLE and CREATE INDEX statements were rendered directly from the SQLAlchemy
models, so this migration and the ORM cannot disagree about the initial state. They are
frozen as literal SQL: a later model change produces a NEW revision and never silently
alters the meaning of this one.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- extensions ------------------------------------------------------------------
    # btree_gist supplies the `=` operator for GiST, which lets one EXCLUDE constraint
    # combine equality on room_id with range overlap on the stay dates.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # --- tables and indexes ----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE amenities (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT,
            CONSTRAINT pk_amenities PRIMARY KEY (id),
            CONSTRAINT uq_amenities_code UNIQUE (code)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE expense_categories (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            is_fixed_cost BOOLEAN DEFAULT false NOT NULL,
            is_active BOOLEAN DEFAULT true NOT NULL,
            CONSTRAINT pk_expense_categories PRIMARY KEY (id),
            CONSTRAINT uq_expense_categories_code UNIQUE (code)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE hotels (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            name TEXT NOT NULL,
            slug TEXT NOT NULL,
            address_line1 TEXT NOT NULL,
            address_line2 TEXT,
            city TEXT NOT NULL,
            region TEXT,
            postal_code TEXT,
            country_code VARCHAR(2) NOT NULL,
            latitude NUMERIC(9, 6),
            longitude NUMERIC(9, 6),
            email TEXT,
            phone TEXT,
            website TEXT,
            timezone TEXT DEFAULT 'UTC' NOT NULL,
            currency VARCHAR(3) NOT NULL,
            star_rating SMALLINT,
            is_active BOOLEAN DEFAULT true NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_hotels PRIMARY KEY (id),
            CONSTRAINT uq_hotels_slug UNIQUE (slug),
            CONSTRAINT uq_hotels_public_id UNIQUE (public_id),
            CONSTRAINT ck_hotels_name_not_blank CHECK (length(btrim(name)) > 0),
            CONSTRAINT ck_hotels_country_code_format CHECK (country_code ~ '^[A-Z]{2}$'),
            CONSTRAINT ck_hotels_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
            CONSTRAINT ck_hotels_latitude_range CHECK (latitude IS NULL OR (latitude >= -90 AND latitude <= 90)),
            CONSTRAINT ck_hotels_longitude_range CHECK (longitude IS NULL OR (longitude >= -180 AND longitude <= 180)),
            CONSTRAINT ck_hotels_star_rating_range CHECK (star_rating IS NULL OR (star_rating >= 1 AND star_rating <= 5))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_hotels_country_code_city ON hotels (country_code, city)
        """
    )
    op.execute(
        """
        CREATE TABLE revenue_categories (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            is_room_revenue BOOLEAN DEFAULT false NOT NULL,
            is_active BOOLEAN DEFAULT true NOT NULL,
            CONSTRAINT pk_revenue_categories PRIMARY KEY (id),
            CONSTRAINT uq_revenue_categories_code UNIQUE (code)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE daily_hotel_metrics (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            hotel_id BIGINT NOT NULL,
            metric_date DATE NOT NULL,
            available_rooms INTEGER NOT NULL,
            occupied_rooms INTEGER NOT NULL,
            out_of_order_rooms INTEGER DEFAULT 0 NOT NULL,
            occupancy_rate NUMERIC(5, 4) GENERATED ALWAYS AS (occupied_rooms::numeric / NULLIF(available_rooms, 0)) STORED,
            room_revenue NUMERIC(14, 2) DEFAULT 0 NOT NULL,
            other_revenue NUMERIC(14, 2) DEFAULT 0 NOT NULL,
            total_revenue NUMERIC(14, 2) GENERATED ALWAYS AS (room_revenue + other_revenue) STORED NOT NULL,
            adr NUMERIC(14, 2) GENERATED ALWAYS AS (room_revenue / NULLIF(occupied_rooms, 0)) STORED,
            revpar NUMERIC(14, 2) GENERATED ALWAYS AS (room_revenue / NULLIF(available_rooms, 0)) STORED,
            total_expenses NUMERIC(14, 2) DEFAULT 0 NOT NULL,
            bookings_created INTEGER DEFAULT 0 NOT NULL,
            cancellations INTEGER DEFAULT 0 NOT NULL,
            no_shows INTEGER DEFAULT 0 NOT NULL,
            arrivals INTEGER DEFAULT 0 NOT NULL,
            departures INTEGER DEFAULT 0 NOT NULL,
            currency VARCHAR(3) NOT NULL,
            computed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_daily_hotel_metrics PRIMARY KEY (id),
            CONSTRAINT uq_daily_hotel_metrics_hotel_id_metric_date UNIQUE (hotel_id, metric_date),
            CONSTRAINT ck_daily_hotel_metrics_available_rooms_non_negative CHECK (available_rooms >= 0),
            CONSTRAINT ck_daily_hotel_metrics_occupied_rooms_within_available CHECK (occupied_rooms >= 0 AND occupied_rooms <= available_rooms),
            CONSTRAINT ck_daily_hotel_metrics_out_of_order_rooms_non_negative CHECK (out_of_order_rooms >= 0),
            CONSTRAINT ck_daily_hotel_metrics_room_revenue_non_negative CHECK (room_revenue >= 0),
            CONSTRAINT ck_daily_hotel_metrics_bookings_created_non_negative CHECK (bookings_created >= 0),
            CONSTRAINT ck_daily_hotel_metrics_cancellations_non_negative CHECK (cancellations >= 0),
            CONSTRAINT ck_daily_hotel_metrics_no_shows_non_negative CHECK (no_shows >= 0),
            CONSTRAINT ck_daily_hotel_metrics_arrivals_non_negative CHECK (arrivals >= 0),
            CONSTRAINT ck_daily_hotel_metrics_departures_non_negative CHECK (departures >= 0),
            CONSTRAINT ck_daily_hotel_metrics_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
            CONSTRAINT fk_daily_hotel_metrics_hotel_id_hotels FOREIGN KEY(hotel_id) REFERENCES hotels (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE expenses (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            hotel_id BIGINT NOT NULL,
            category_id BIGINT NOT NULL,
            expense_date DATE NOT NULL,
            amount NUMERIC(14, 2) NOT NULL,
            tax_amount NUMERIC(14, 2) DEFAULT 0 NOT NULL,
            currency VARCHAR(3) NOT NULL,
            description TEXT,
            vendor TEXT,
            invoice_reference TEXT,
            is_recurring BOOLEAN DEFAULT false NOT NULL,
            recurrence_interval TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_expenses PRIMARY KEY (id),
            CONSTRAINT ck_expenses_tax_amount_non_negative CHECK (tax_amount >= 0),
            CONSTRAINT ck_expenses_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
            CONSTRAINT ck_expenses_recurrence_consistent CHECK (is_recurring = (recurrence_interval IS NOT NULL)),
            CONSTRAINT ck_expenses_recurrence_interval_valid CHECK (recurrence_interval IS NULL OR recurrence_interval IN ('monthly', 'quarterly', 'annual')),
            CONSTRAINT fk_expenses_hotel_id_hotels FOREIGN KEY(hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT,
            CONSTRAINT fk_expenses_category_id_expense_categories FOREIGN KEY(category_id) REFERENCES expense_categories (id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_expenses_hotel_id_expense_date_category_id ON expenses (hotel_id, expense_date, category_id)
        """
    )
    op.execute(
        """
        CREATE TABLE guests (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            hotel_id BIGINT NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            country_code VARCHAR(2),
            preferred_language VARCHAR(2),
            date_of_birth DATE,
            marketing_opt_in BOOLEAN DEFAULT false NOT NULL,
            notes TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_guests PRIMARY KEY (id),
            CONSTRAINT uq_guests_public_id UNIQUE (public_id),
            CONSTRAINT uq_guests_id_hotel_id UNIQUE (id, hotel_id),
            CONSTRAINT ck_guests_country_code_format CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$'),
            CONSTRAINT fk_guests_hotel_id_hotels FOREIGN KEY(hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_guests_hotel_id_last_name ON guests (hotel_id, last_name)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_guests_hotel_id_email ON guests (hotel_id, email) WHERE email IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TABLE room_types (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            hotel_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            code TEXT NOT NULL,
            description TEXT,
            max_occupancy SMALLINT NOT NULL,
            standard_occupancy SMALLINT NOT NULL,
            bed_count SMALLINT NOT NULL,
            bed_configuration TEXT,
            size_sqm NUMERIC(6, 2),
            base_price NUMERIC(14, 2) NOT NULL,
            currency VARCHAR(3) NOT NULL,
            is_active BOOLEAN DEFAULT true NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_room_types PRIMARY KEY (id),
            CONSTRAINT uq_room_types_hotel_id_code UNIQUE (hotel_id, code),
            CONSTRAINT uq_room_types_id_hotel_id UNIQUE (id, hotel_id),
            CONSTRAINT ck_room_types_max_occupancy_positive CHECK (max_occupancy > 0),
            CONSTRAINT ck_room_types_standard_occupancy_range CHECK (standard_occupancy > 0 AND standard_occupancy <= max_occupancy),
            CONSTRAINT ck_room_types_bed_count_positive CHECK (bed_count > 0),
            CONSTRAINT ck_room_types_size_sqm_positive CHECK (size_sqm IS NULL OR size_sqm > 0),
            CONSTRAINT ck_room_types_base_price_non_negative CHECK (base_price >= 0),
            CONSTRAINT ck_room_types_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
            CONSTRAINT fk_room_types_hotel_id_hotels FOREIGN KEY(hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE bookings (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            hotel_id BIGINT NOT NULL,
            guest_id BIGINT NOT NULL,
            reference TEXT NOT NULL,
            check_in_date DATE NOT NULL,
            check_out_date DATE NOT NULL,
            status TEXT DEFAULT 'pending' NOT NULL,
            adults SMALLINT DEFAULT 1 NOT NULL,
            children SMALLINT DEFAULT 0 NOT NULL,
            source TEXT DEFAULT 'direct' NOT NULL,
            channel_reference TEXT,
            total_amount NUMERIC(14, 2) NOT NULL,
            currency VARCHAR(3) NOT NULL,
            special_requests TEXT,
            cancelled_at TIMESTAMP WITH TIME ZONE,
            cancellation_reason TEXT,
            booked_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_bookings PRIMARY KEY (id),
            CONSTRAINT fk_bookings_guest_id_hotel_id_guests FOREIGN KEY(guest_id, hotel_id) REFERENCES guests (id, hotel_id) ON DELETE RESTRICT,
            CONSTRAINT uq_bookings_public_id UNIQUE (public_id),
            CONSTRAINT uq_bookings_hotel_id_reference UNIQUE (hotel_id, reference),
            CONSTRAINT uq_bookings_id_hotel_id UNIQUE (id, hotel_id),
            CONSTRAINT uq_bookings_id_stay_status UNIQUE (id, check_in_date, check_out_date, status),
            CONSTRAINT ck_bookings_stay_dates_ordered CHECK (check_out_date > check_in_date),
            CONSTRAINT ck_bookings_adults_positive CHECK (adults >= 1),
            CONSTRAINT ck_bookings_children_non_negative CHECK (children >= 0),
            CONSTRAINT ck_bookings_total_amount_non_negative CHECK (total_amount >= 0),
            CONSTRAINT ck_bookings_status_valid CHECK (status IN ('pending', 'confirmed', 'checked_in', 'checked_out', 'cancelled', 'no_show')),
            CONSTRAINT ck_bookings_source_valid CHECK (source IN ('direct', 'website', 'phone', 'walk_in', 'booking_com', 'expedia', 'airbnb', 'agoda', 'other')),
            CONSTRAINT ck_bookings_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
            CONSTRAINT ck_bookings_cancellation_consistent CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL)),
            CONSTRAINT fk_bookings_hotel_id_hotels FOREIGN KEY(hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_bookings_guest_id ON bookings (guest_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_bookings_hotel_id_booked_at ON bookings (hotel_id, booked_at)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_bookings_hotel_id_check_in_date ON bookings (hotel_id, check_in_date)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_bookings_hotel_id_status_check_in_date ON bookings (hotel_id, status, check_in_date)
        """
    )
    op.execute(
        """
        CREATE TABLE room_type_amenities (
            room_type_id BIGINT NOT NULL,
            amenity_id BIGINT NOT NULL,
            CONSTRAINT pk_room_type_amenities PRIMARY KEY (room_type_id, amenity_id),
            CONSTRAINT fk_room_type_amenities_room_type_id_room_types FOREIGN KEY(room_type_id) REFERENCES room_types (id) ON DELETE CASCADE,
            CONSTRAINT fk_room_type_amenities_amenity_id_amenities FOREIGN KEY(amenity_id) REFERENCES amenities (id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_room_type_amenities_amenity_id ON room_type_amenities (amenity_id)
        """
    )
    op.execute(
        """
        CREATE TABLE rooms (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            hotel_id BIGINT NOT NULL,
            room_type_id BIGINT NOT NULL,
            room_number TEXT NOT NULL,
            floor SMALLINT,
            status TEXT DEFAULT 'available' NOT NULL,
            notes TEXT,
            is_active BOOLEAN DEFAULT true NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_rooms PRIMARY KEY (id),
            CONSTRAINT fk_rooms_room_type_id_hotel_id_room_types FOREIGN KEY(room_type_id, hotel_id) REFERENCES room_types (id, hotel_id) ON DELETE RESTRICT,
            CONSTRAINT uq_rooms_hotel_id_room_number UNIQUE (hotel_id, room_number),
            CONSTRAINT uq_rooms_id_hotel_id UNIQUE (id, hotel_id),
            CONSTRAINT ck_rooms_status_valid CHECK (status IN ('available', 'occupied', 'cleaning', 'maintenance', 'out_of_order')),
            CONSTRAINT fk_rooms_hotel_id_hotels FOREIGN KEY(hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_rooms_hotel_id_room_type_id ON rooms (hotel_id, room_type_id)
        """
    )
    op.execute(
        """
        CREATE TABLE booking_rooms (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            booking_id BIGINT NOT NULL,
            room_id BIGINT NOT NULL,
            hotel_id BIGINT NOT NULL,
            check_in_date DATE NOT NULL,
            check_out_date DATE NOT NULL,
            booking_status TEXT NOT NULL,
            nights INTEGER GENERATED ALWAYS AS (check_out_date - check_in_date) STORED NOT NULL,
            adults SMALLINT DEFAULT 1 NOT NULL,
            children SMALLINT DEFAULT 0 NOT NULL,
            guest_name TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_booking_rooms PRIMARY KEY (id),
            CONSTRAINT fk_booking_rooms_booking_stay_status_bookings FOREIGN KEY(booking_id, check_in_date, check_out_date, booking_status) REFERENCES bookings (id, check_in_date, check_out_date, status) ON DELETE CASCADE ON UPDATE CASCADE,
            CONSTRAINT fk_booking_rooms_room_id_hotel_id_rooms FOREIGN KEY(room_id, hotel_id) REFERENCES rooms (id, hotel_id) ON DELETE RESTRICT,
            CONSTRAINT uq_booking_rooms_booking_id_room_id UNIQUE (booking_id, room_id),
            CONSTRAINT uq_booking_rooms_id_stay UNIQUE (id, check_in_date, check_out_date),
            CONSTRAINT uq_booking_rooms_id_hotel_id UNIQUE (id, hotel_id),
            CONSTRAINT ck_booking_rooms_stay_dates_ordered CHECK (check_out_date > check_in_date),
            CONSTRAINT ck_booking_rooms_adults_positive CHECK (adults >= 1),
            CONSTRAINT ck_booking_rooms_children_non_negative CHECK (children >= 0),
            CONSTRAINT excl_booking_rooms_room_no_overlap EXCLUDE USING gist (room_id WITH =, daterange(check_in_date, check_out_date, '[)') WITH &&) WHERE (booking_status IN ('confirmed', 'checked_in'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_booking_rooms_booking_id ON booking_rooms (booking_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_booking_rooms_room_id_check_in_date ON booking_rooms (room_id, check_in_date)
        """
    )
    op.execute(
        """
        CREATE TABLE payments (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            booking_id BIGINT NOT NULL,
            hotel_id BIGINT NOT NULL,
            kind TEXT DEFAULT 'charge' NOT NULL,
            amount NUMERIC(14, 2) NOT NULL,
            currency VARCHAR(3) NOT NULL,
            method TEXT NOT NULL,
            status TEXT DEFAULT 'pending' NOT NULL,
            paid_at TIMESTAMP WITH TIME ZONE,
            provider TEXT,
            transaction_reference TEXT,
            refunded_payment_id BIGINT,
            failure_reason TEXT,
            card_last_four VARCHAR(4),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_payments PRIMARY KEY (id),
            CONSTRAINT fk_payments_booking_id_hotel_id_bookings FOREIGN KEY(booking_id, hotel_id) REFERENCES bookings (id, hotel_id) ON DELETE RESTRICT,
            CONSTRAINT ck_payments_amount_positive CHECK (amount > 0),
            CONSTRAINT ck_payments_kind_valid CHECK (kind IN ('charge', 'refund')),
            CONSTRAINT ck_payments_method_valid CHECK (method IN ('card', 'cash', 'bank_transfer', 'online_gateway', 'ota_collect', 'voucher')),
            CONSTRAINT ck_payments_status_valid CHECK (status IN ('pending', 'authorized', 'captured', 'failed', 'refunded', 'partially_refunded', 'cancelled')),
            CONSTRAINT ck_payments_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
            CONSTRAINT ck_payments_refund_references_charge CHECK ((kind = 'refund') = (refunded_payment_id IS NOT NULL)),
            CONSTRAINT ck_payments_captured_has_paid_at CHECK (status <> 'captured' OR paid_at IS NOT NULL),
            CONSTRAINT ck_payments_card_last_four_format CHECK (card_last_four IS NULL OR card_last_four ~ '^[0-9]{4}$'),
            CONSTRAINT fk_payments_refunded_payment_id_payments FOREIGN KEY(refunded_payment_id) REFERENCES payments (id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_payments_booking_id ON payments (booking_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_payments_hotel_id_paid_at ON payments (hotel_id, paid_at) WHERE status = 'captured'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_payments_provider_transaction_reference ON payments (provider, transaction_reference) WHERE transaction_reference IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TABLE revenue (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            hotel_id BIGINT NOT NULL,
            category_id BIGINT NOT NULL,
            booking_id BIGINT,
            revenue_date DATE NOT NULL,
            amount NUMERIC(14, 2) NOT NULL,
            tax_amount NUMERIC(14, 2) DEFAULT 0 NOT NULL,
            currency VARCHAR(3) NOT NULL,
            description TEXT,
            reference TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_revenue PRIMARY KEY (id),
            CONSTRAINT fk_revenue_booking_id_hotel_id_bookings FOREIGN KEY(booking_id, hotel_id) REFERENCES bookings (id, hotel_id) ON DELETE SET NULL,
            CONSTRAINT ck_revenue_tax_amount_non_negative CHECK (tax_amount >= 0),
            CONSTRAINT ck_revenue_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
            CONSTRAINT fk_revenue_hotel_id_hotels FOREIGN KEY(hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT,
            CONSTRAINT fk_revenue_category_id_revenue_categories FOREIGN KEY(category_id) REFERENCES revenue_categories (id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_revenue_booking_id ON revenue (booking_id) WHERE booking_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_revenue_hotel_id_revenue_date_category_id ON revenue (hotel_id, revenue_date, category_id)
        """
    )
    op.execute(
        """
        CREATE TABLE reviews (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            hotel_id BIGINT NOT NULL,
            guest_id BIGINT,
            booking_id BIGINT,
            source TEXT DEFAULT 'direct' NOT NULL,
            external_review_id TEXT,
            rating NUMERIC(4, 2) NOT NULL,
            rating_scale SMALLINT DEFAULT 5 NOT NULL,
            rating_normalized NUMERIC(5, 4) GENERATED ALWAYS AS (rating / rating_scale) STORED NOT NULL,
            title TEXT,
            body TEXT,
            language VARCHAR(2),
            reviewer_name TEXT,
            review_date DATE NOT NULL,
            is_published BOOLEAN DEFAULT true NOT NULL,
            responded_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_reviews PRIMARY KEY (id),
            CONSTRAINT fk_reviews_guest_id_hotel_id_guests FOREIGN KEY(guest_id, hotel_id) REFERENCES guests (id, hotel_id) ON DELETE SET NULL,
            CONSTRAINT fk_reviews_booking_id_hotel_id_bookings FOREIGN KEY(booking_id, hotel_id) REFERENCES bookings (id, hotel_id) ON DELETE SET NULL,
            CONSTRAINT ck_reviews_source_valid CHECK (source IN ('direct', 'booking_com', 'tripadvisor', 'google', 'expedia', 'airbnb', 'other')),
            CONSTRAINT ck_reviews_rating_scale_valid CHECK (rating_scale IN (5, 10)),
            CONSTRAINT ck_reviews_rating_in_scale CHECK (rating >= 0 AND rating <= rating_scale),
            CONSTRAINT ck_reviews_language_format CHECK (language IS NULL OR language ~ '^[a-z]{2}$'),
            CONSTRAINT fk_reviews_hotel_id_hotels FOREIGN KEY(hotel_id) REFERENCES hotels (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_reviews_hotel_id_review_date ON reviews (hotel_id, review_date DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_reviews_hotel_id_source ON reviews (hotel_id, source)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_reviews_booking_id ON reviews (booking_id) WHERE booking_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_reviews_source_external_review_id ON reviews (source, external_review_id) WHERE external_review_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TABLE booking_room_nights (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            booking_room_id BIGINT NOT NULL,
            hotel_id BIGINT NOT NULL,
            check_in_date DATE NOT NULL,
            check_out_date DATE NOT NULL,
            stay_date DATE NOT NULL,
            rate NUMERIC(14, 2) NOT NULL,
            rate_plan_code TEXT,
            is_complimentary BOOLEAN DEFAULT false NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_booking_room_nights PRIMARY KEY (id),
            CONSTRAINT fk_brn_booking_room_stay_booking_rooms FOREIGN KEY(booking_room_id, check_in_date, check_out_date) REFERENCES booking_rooms (id, check_in_date, check_out_date) ON DELETE CASCADE ON UPDATE CASCADE,
            CONSTRAINT fk_brn_booking_room_id_hotel_id_booking_rooms FOREIGN KEY(booking_room_id, hotel_id) REFERENCES booking_rooms (id, hotel_id) ON DELETE CASCADE,
            CONSTRAINT uq_booking_room_nights_room_stay_date UNIQUE (booking_room_id, stay_date),
            CONSTRAINT ck_booking_room_nights_stay_date_within_stay CHECK (stay_date >= check_in_date AND stay_date < check_out_date),
            CONSTRAINT ck_booking_room_nights_rate_non_negative CHECK (rate >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_booking_room_nights_hotel_id_stay_date ON booking_room_nights (hotel_id, stay_date)
        """
    )

    # --- updated_at maintenance ------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_hotels_set_updated_at BEFORE UPDATE ON hotels "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_daily_hotel_metrics_set_updated_at BEFORE UPDATE ON daily_hotel_metrics "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_expenses_set_updated_at BEFORE UPDATE ON expenses "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_guests_set_updated_at BEFORE UPDATE ON guests "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_room_types_set_updated_at BEFORE UPDATE ON room_types "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_bookings_set_updated_at BEFORE UPDATE ON bookings "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_rooms_set_updated_at BEFORE UPDATE ON rooms "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_booking_rooms_set_updated_at BEFORE UPDATE ON booking_rooms "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_payments_set_updated_at BEFORE UPDATE ON payments "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_revenue_set_updated_at BEFORE UPDATE ON revenue "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_reviews_set_updated_at BEFORE UPDATE ON reviews "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_booking_room_nights_set_updated_at BEFORE UPDATE ON booking_room_nights "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # --- night-set completeness (approved decision 17) --------------------------------
    #
    # Invariant: booking_rooms.nights = COUNT(booking_room_nights) for that booking_room.
    #
    # This CANNOT be a CHECK constraint -- a check cannot aggregate a child table -- and it
    # cannot be an immediate trigger either: at the instant a booking_room is inserted it
    # legitimately has zero night rows, and the nights are inserted moments later in the
    # same transaction. An immediate trigger would make correct code impossible.
    #
    # A CONSTRAINT TRIGGER declared DEFERRABLE INITIALLY DEFERRED fires at COMMIT instead,
    # so intermediate states inside the transaction are legal and only the final state is
    # judged. That is exactly the semantics wanted: the transaction may build the stay in
    # any order, but it cannot commit an incomplete one.
    #
    # It is attached to BOTH tables because either side can break the invariant:
    #   * booking_rooms  INSERT/UPDATE -- a new stay, or a date change that alters `nights`
    #   * booking_room_nights INSERT/UPDATE/DELETE -- adding, moving or removing a night
    #
    # When the parent has been deleted in the same transaction (ON DELETE CASCADE removes
    # its nights too) there is nothing left to check, so the lookup simply finds no row and
    # the trigger returns without raising.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assert_booking_room_nights_complete() RETURNS trigger AS $$
        DECLARE
            v_booking_room_id bigint;
            v_expected        integer;
            v_actual          integer;
        BEGIN
            IF TG_TABLE_NAME = 'booking_rooms' THEN
                v_booking_room_id := NEW.id;
            ELSE
                v_booking_room_id := COALESCE(NEW.booking_room_id, OLD.booking_room_id);
            END IF;

            SELECT nights INTO v_expected
            FROM booking_rooms
            WHERE id = v_booking_room_id;

            IF NOT FOUND THEN
                -- Parent gone in this transaction; the invariant is vacuously satisfied.
                RETURN NULL;
            END IF;

            SELECT count(*) INTO v_actual
            FROM booking_room_nights
            WHERE booking_room_id = v_booking_room_id;

            IF v_actual <> v_expected THEN
                RAISE EXCEPTION
                    'booking_room % has % night row(s) but its stay is % night(s)',
                    v_booking_room_id, v_actual, v_expected
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_booking_rooms_nights_complete
        AFTER INSERT OR UPDATE ON booking_rooms
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION assert_booking_room_nights_complete()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_booking_room_nights_complete
        AFTER INSERT OR UPDATE OR DELETE ON booking_room_nights
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION assert_booking_room_nights_complete()
        """
    )

    # --- historical overlap detection (approved decision 20) --------------------------
    #
    # The exclusion constraint guards only inventory-holding statuses ('confirmed', 'checked_in'),
    # so once a stay is checked out it no longer blocks that room. Overlapping HISTORICAL
    # stays therefore become representable -- harmless for forward booking, but back-dated
    # corrections or a PMS migration could introduce silent double-occupancy.
    #
    # Per decision 20 this is DETECTED, never auto-corrected: the view reports conflicts and
    # a human decides. It deliberately uses the wider occupancy status set.
    op.execute(
        """
        CREATE VIEW historical_room_overlaps AS
        SELECT a.hotel_id,
               a.room_id,
               a.id         AS booking_room_id_a,
               b.id         AS booking_room_id_b,
               a.booking_id AS booking_id_a,
               b.booking_id AS booking_id_b,
               GREATEST(a.check_in_date, b.check_in_date) AS overlap_start,
               LEAST(a.check_out_date, b.check_out_date)  AS overlap_end
        FROM booking_rooms a
        JOIN booking_rooms b
          ON a.room_id = b.room_id
         AND a.id < b.id
         AND daterange(a.check_in_date, a.check_out_date, '[)')
             && daterange(b.check_in_date, b.check_out_date, '[)')
        WHERE a.booking_status IN ('confirmed', 'checked_in', 'checked_out')
          AND b.booking_status IN ('confirmed', 'checked_in', 'checked_out')
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS historical_room_overlaps")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_booking_room_nights_complete ON booking_room_nights"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_booking_rooms_nights_complete ON booking_rooms")
    op.execute("DROP FUNCTION IF EXISTS assert_booking_room_nights_complete()")

    # CASCADE drops each table's own updated_at trigger along with it.
    op.execute("DROP TABLE IF EXISTS booking_room_nights CASCADE")
    op.execute("DROP TABLE IF EXISTS reviews CASCADE")
    op.execute("DROP TABLE IF EXISTS revenue CASCADE")
    op.execute("DROP TABLE IF EXISTS payments CASCADE")
    op.execute("DROP TABLE IF EXISTS booking_rooms CASCADE")
    op.execute("DROP TABLE IF EXISTS rooms CASCADE")
    op.execute("DROP TABLE IF EXISTS room_type_amenities CASCADE")
    op.execute("DROP TABLE IF EXISTS bookings CASCADE")
    op.execute("DROP TABLE IF EXISTS room_types CASCADE")
    op.execute("DROP TABLE IF EXISTS guests CASCADE")
    op.execute("DROP TABLE IF EXISTS expenses CASCADE")
    op.execute("DROP TABLE IF EXISTS daily_hotel_metrics CASCADE")
    op.execute("DROP TABLE IF EXISTS revenue_categories CASCADE")
    op.execute("DROP TABLE IF EXISTS hotels CASCADE")
    op.execute("DROP TABLE IF EXISTS expense_categories CASCADE")
    op.execute("DROP TABLE IF EXISTS amenities CASCADE")

    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")

    # btree_gist is deliberately NOT dropped: other schemas in the same database may rely
    # on it, and dropping a shared extension during a downgrade is a wider blast radius
    # than the migration is entitled to.
