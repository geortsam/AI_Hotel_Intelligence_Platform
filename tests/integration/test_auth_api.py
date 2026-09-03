"""Authentication against real PostgreSQL.

What only a database can show: that the plaintext password is nowhere in the stored row, that
``uq_users_email`` -- not a prior lookup -- decides a duplicate registration, and that a
failed login writes nothing at all.

The recurring theme is that **every rejection looks the same from outside**. An unknown
address, a wrong password and a disabled account produce one identical 401, and the tests
compare the responses byte for byte rather than merely checking the status code.

SQLite is not substituted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import jwt
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_db
from app.core.config import Settings
from app.core.security import ALGORITHM, TOKEN_TYPE_ACCESS, create_access_token
from app.main import create_app
from app.models import User
from tests.integration.conftest import requires_postgres

pytestmark = requires_postgres

SECRET = "an-integration-test-signing-secret"
OTHER_SECRET = "a-different-signing-secret-entirely"
EMAIL = "ada.lovelace@example.test"
PASSWORD = "correct horse battery staple"

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"


def registration(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "email": EMAIL,
        "password": PASSWORD,
        "full_name": "Ada Lovelace",
    }
    body.update(overrides)
    return body


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", secret_key=SECRET)


@pytest.fixture
def api(engine: Engine, settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)

    with factory() as cleanup:
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


@pytest.fixture
def account(api: TestClient) -> dict:
    """A registered, active account."""
    response = api.post(REGISTER, json=registration())
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def sign_in(api: TestClient, **overrides: str) -> str:
    body: dict[str, str] = {"email": EMAIL, "password": PASSWORD}
    body.update(overrides)
    response = api.post(LOGIN, json=body)
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


# --- registration ------------------------------------------------------------------------------


def test_registration_returns_201_and_the_identity(api: TestClient) -> None:
    response = api.post(REGISTER, json=registration())
    body = response.json()

    assert response.status_code == 201
    assert body["email"] == EMAIL
    assert body["full_name"] == "Ada Lovelace"
    assert body["is_active"] is True
    assert body["last_login_at"] is None
    assert uuid.UUID(body["public_id"])


def test_the_response_contract_is_exactly_as_declared(api: TestClient) -> None:
    body = api.post(REGISTER, json=registration()).json()

    assert set(body) == {
        "public_id",
        "email",
        "full_name",
        "is_active",
        "created_at",
        "last_login_at",
    }


def test_the_plaintext_password_is_never_stored(
    api: TestClient, account: dict, session: Session
) -> None:
    """The single most important assertion in this file."""
    stored = session.scalars(sa.select(User)).one()

    assert stored.password_hash != PASSWORD
    assert PASSWORD not in stored.password_hash
    assert stored.password_hash.startswith("$argon2id$")


def test_no_column_of_the_row_contains_the_password(
    api: TestClient, account: dict, session: Session
) -> None:
    """Not just password_hash: no column at all."""
    row = session.execute(sa.text("SELECT * FROM users")).mappings().one()

    for column, value in row.items():
        assert PASSWORD not in str(value), f"password leaked into {column}"


def test_two_accounts_with_the_same_password_have_different_hashes(
    api: TestClient, session: Session
) -> None:
    """Per-hash salting, observed through the API rather than the library."""
    api.post(REGISTER, json=registration(email="one@example.test"))
    api.post(REGISTER, json=registration(email="two@example.test"))

    hashes = list(session.scalars(sa.select(User.password_hash)).all())

    assert len(hashes) == 2
    assert hashes[0] != hashes[1]


def test_the_password_is_not_echoed_in_the_response(api: TestClient) -> None:
    text = api.post(REGISTER, json=registration()).text

    assert PASSWORD not in text
    assert "password" not in text
    assert "argon2" not in text


def test_registration_leaks_nothing_into_the_logs(
    api: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        api.post(REGISTER, json=registration())
        api.post(REGISTER, json=registration())  # the duplicate, which does log

    captured = "\n".join(
        record.getMessage() + str(record.exc_info or "") for record in caplog.records
    )
    assert PASSWORD not in captured
    assert "argon2" not in captured
    assert EMAIL not in captured  # not even the address


def test_a_duplicate_email_returns_409(api: TestClient, account: dict) -> None:
    """uq_users_email decides, not a prior lookup."""
    response = api.post(REGISTER, json=registration(full_name="Someone Else"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_the_duplicate_conflict_does_not_echo_the_address(api: TestClient, account: dict) -> None:
    """The person typing it knows it; repeating it into logs and proxies is gratuitous."""
    text = api.post(REGISTER, json=registration()).text

    assert EMAIL not in text
    for leak in ["uq_users_email", "psycopg", "sqlalchemy", "DETAIL:", "23505", "INSERT"]:
        assert leak.lower() not in text.lower(), f"leaked {leak!r}"


def test_the_duplicate_writes_no_second_row(
    api: TestClient, account: dict, session: Session
) -> None:
    api.post(REGISTER, json=registration(full_name="Someone Else"))

    assert session.scalar(sa.select(sa.func.count()).select_from(User)) == 1
    assert session.scalars(sa.select(User.full_name)).one() == "Ada Lovelace"


def test_the_email_is_stored_lowercased(api: TestClient, session: Session) -> None:
    """So that ADA@... and ada@... are one account rather than two."""
    api.post(REGISTER, json=registration(email="ADA.Lovelace@Example.Test"))

    assert session.scalars(sa.select(User.email)).one() == "ada.lovelace@example.test"


def test_a_differently_cased_duplicate_is_still_a_duplicate(api: TestClient, account: dict) -> None:
    response = api.post(REGISTER, json=registration(email=EMAIL.upper()))

    assert response.status_code == 409


@pytest.mark.parametrize(
    "email", ["not-an-email", "no@domain", "@example.test", "a b@example.test", ""]
)
def test_a_malformed_email_is_rejected(api: TestClient, email: str) -> None:
    response = api.post(REGISTER, json=registration(email=email))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


#: 11 characters -- one below the floor. ("eleven chars" is 12 and correctly passes.)
@pytest.mark.parametrize("password", ["", "short", "elevenchars"])
def test_a_password_below_the_floor_is_rejected(api: TestClient, password: str) -> None:
    assert api.post(REGISTER, json=registration(password=password)).status_code == 422


def test_the_rejected_password_is_not_echoed_in_the_validation_error(api: TestClient) -> None:
    """A 422 that quotes the value writes the password into logs and error trackers."""
    weak = "hunter2hunt"

    text = api.post(REGISTER, json=registration(password=weak)).text

    assert weak not in text


@pytest.mark.parametrize(
    "field", ["is_active", "role", "permissions", "hotel_id", "public_id", "id", "password_hash"]
)
def test_registration_rejects_privileged_or_server_assigned_fields(
    api: TestClient, field: str
) -> None:
    """extra="forbid": no self-activation, no self-promotion, no identity injection."""
    response = api.post(REGISTER, json=registration(**{field: "x"}))

    assert response.status_code == 422


def test_a_rejected_registration_writes_nothing(api: TestClient, session: Session) -> None:
    api.post(REGISTER, json=registration(email="bad"))
    api.post(REGISTER, json=registration(password="short"))

    assert session.scalar(sa.select(sa.func.count()).select_from(User)) == 0


# --- login -------------------------------------------------------------------------------------


def test_valid_credentials_return_a_bearer_token(api: TestClient, account: dict) -> None:
    response = api.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})
    body = response.json()

    assert response.status_code == 200
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800
    assert body["access_token"].count(".") == 2


def test_the_token_carries_only_the_five_claims(api: TestClient, account: dict) -> None:
    """A JWT is readable by anyone holding it."""
    token = sign_in(api)

    claims = jwt.decode(token, SECRET, algorithms=[ALGORITHM])

    assert set(claims) == {"sub", "iat", "exp", "jti", "typ"}
    assert claims["sub"] == account["public_id"]
    assert claims["typ"] == TOKEN_TYPE_ACCESS


def test_no_identifying_detail_leaks_into_the_token(api: TestClient, account: dict) -> None:
    token = sign_in(api)

    claims = jwt.decode(token, SECRET, algorithms=[ALGORITHM])
    rendered = str(claims)
    assert EMAIL not in rendered
    assert "Ada" not in rendered
    assert PASSWORD not in rendered


def test_the_subject_is_a_uuid_not_a_sequential_id(api: TestClient, account: dict) -> None:
    """The first user in an empty table has id=1; that must not be the token's subject."""
    claims = jwt.decode(sign_in(api), SECRET, algorithms=[ALGORITHM])

    assert uuid.UUID(claims["sub"])
    assert claims["sub"] != "1"


def test_login_records_the_sign_in_time(api: TestClient, account: dict) -> None:
    assert account["last_login_at"] is None
    sign_in(api)

    assert api.get(ME, headers=bearer(sign_in(api))).json()["last_login_at"] is not None


def test_a_wrong_password_is_rejected(api: TestClient, account: dict) -> None:
    response = api.post(LOGIN, json={"email": EMAIL, "password": "the wrong password"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"


def test_an_unknown_address_and_a_wrong_password_are_indistinguishable(
    api: TestClient, account: dict
) -> None:
    """Compared byte for byte, not merely by status code. A different body would enumerate
    which addresses have accounts here."""
    unknown = api.post(LOGIN, json={"email": "nobody@example.test", "password": PASSWORD})
    wrong = api.post(LOGIN, json={"email": EMAIL, "password": "the wrong password"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_a_disabled_account_is_indistinguishable_from_a_wrong_password(
    api: TestClient, account: dict, session: Session
) -> None:
    """Telling an attacker the account exists but is disabled confirms they found a real
    one."""
    wrong = api.post(LOGIN, json={"email": EMAIL, "password": "the wrong password"}).json()
    session.execute(sa.text("UPDATE users SET is_active = false"))
    session.commit()

    disabled = api.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})

    assert disabled.status_code == 401
    assert disabled.json() == wrong


def test_the_failure_message_names_neither_the_address_nor_the_reason(
    api: TestClient, account: dict
) -> None:
    body = api.post(LOGIN, json={"email": EMAIL, "password": "wrong"}).json()

    message = body["error"]["message"]
    assert EMAIL not in message
    for leak in ["not found", "does not exist", "disabled", "inactive", "no such", "unknown"]:
        assert leak not in message.lower()


def test_login_never_echoes_the_password(api: TestClient, account: dict) -> None:
    attempt = "a wrong but memorable password"

    text = api.post(LOGIN, json={"email": EMAIL, "password": attempt}).text

    assert attempt not in text
    assert PASSWORD not in text


def test_a_failed_login_leaks_no_database_detail(api: TestClient, account: dict) -> None:
    text = api.post(LOGIN, json={"email": EMAIL, "password": "wrong"}).text

    for leak in ["psycopg", "sqlalchemy", "SELECT", "users", "argon2", "Traceback", "23505"]:
        assert leak.lower() not in text.lower(), f"leaked {leak!r}"


def test_a_failed_login_writes_nothing(api: TestClient, account: dict, session: Session) -> None:
    """A login that touches the database on every guess is a denial-of-service surface."""
    before = session.execute(sa.text("SELECT * FROM users")).mappings().one()

    for _ in range(3):
        api.post(LOGIN, json={"email": EMAIL, "password": "wrong"})

    session.expire_all()
    after = session.execute(sa.text("SELECT * FROM users")).mappings().one()
    assert dict(after) == dict(before)


def test_the_email_is_matched_case_insensitively_at_login(api: TestClient, account: dict) -> None:
    assert api.post(LOGIN, json={"email": EMAIL.upper(), "password": PASSWORD}).status_code == 200
    assert api.post(LOGIN, json={"email": f"  {EMAIL}  ", "password": PASSWORD}).status_code == 200


def test_an_empty_credential_is_a_validation_error_not_an_authentication_one(
    api: TestClient, account: dict
) -> None:
    """A missing field is a malformed request; a wrong value is a failed login."""
    assert api.post(LOGIN, json={"email": EMAIL}).status_code == 422
    assert api.post(LOGIN, json={"email": "", "password": PASSWORD}).status_code == 422


# --- authenticated requests ---------------------------------------------------------------------


def test_a_valid_token_reaches_the_authenticated_dependency(api: TestClient, account: dict) -> None:
    response = api.get(ME, headers=bearer(sign_in(api)))
    body = response.json()

    assert response.status_code == 200
    assert body["public_id"] == account["public_id"]
    assert body["email"] == EMAIL


def test_the_me_response_carries_no_hash(api: TestClient, account: dict) -> None:
    body = api.get(ME, headers=bearer(sign_in(api))).json()

    assert "password" not in body
    assert "password_hash" not in body
    assert "argon2" not in str(body)


def test_no_token_at_all_is_401(api: TestClient, account: dict) -> None:
    response = api.get(ME)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic abc"},
        {"Authorization": "not-a-scheme token"},
        {"Authorization": ""},
    ],
)
def test_a_malformed_authorization_header_is_401(
    api: TestClient, account: dict, header: dict[str, str]
) -> None:
    assert api.get(ME, headers=header).status_code == 401


@pytest.mark.parametrize("token", ["garbage", "a.b", "a.b.c", "...", "eyJhbGciOiJub25lIn0.e30."])
def test_a_malformed_token_is_401(api: TestClient, account: dict, token: str) -> None:
    assert api.get(ME, headers=bearer(token)).status_code == 401


def test_a_token_signed_with_the_wrong_secret_is_401(api: TestClient, account: dict) -> None:
    forged = create_access_token(
        Settings(environment="test", secret_key=OTHER_SECRET),
        uuid.UUID(account["public_id"]),
    )

    assert api.get(ME, headers=bearer(forged)).status_code == 401


def test_an_expired_token_is_401(api: TestClient, account: dict, settings: Settings) -> None:
    expired = create_access_token(
        settings,
        uuid.UUID(account["public_id"]),
        now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
    )

    assert api.get(ME, headers=bearer(expired)).status_code == 401


def test_an_alg_none_token_is_401(api: TestClient, account: dict) -> None:
    """Algorithm confusion, attempted against the live endpoint."""
    now = dt.datetime.now(dt.UTC)
    forged = jwt.encode(
        {
            "sub": account["public_id"],
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(hours=1)).timestamp()),
            "jti": str(uuid.uuid4()),
            "typ": TOKEN_TYPE_ACCESS,
        },
        key="",
        algorithm="none",
    )

    assert api.get(ME, headers=bearer(forged)).status_code == 401


def test_a_token_for_a_nonexistent_user_is_401(
    api: TestClient, account: dict, settings: Settings
) -> None:
    """Correctly signed, but names nobody."""
    orphan = create_access_token(settings, uuid.uuid4())

    assert api.get(ME, headers=bearer(orphan)).status_code == 401


def test_a_token_stops_working_the_moment_the_account_is_disabled(
    api: TestClient, account: dict, session: Session
) -> None:
    """The user is re-read on every request rather than trusted from the token, so a
    disabled account loses access now instead of when its token happens to expire."""
    token = sign_in(api)
    assert api.get(ME, headers=bearer(token)).status_code == 200

    session.execute(sa.text("UPDATE users SET is_active = false"))
    session.commit()

    assert api.get(ME, headers=bearer(token)).status_code == 401


def test_every_authentication_failure_looks_identical(api: TestClient, account: dict) -> None:
    """Missing, malformed, expired, forged and orphaned all produce one response."""
    settings = Settings(environment="test", secret_key=SECRET)
    responses = [
        api.get(ME),
        api.get(ME, headers=bearer("garbage")),
        api.get(
            ME,
            headers=bearer(
                create_access_token(
                    settings,
                    uuid.UUID(account["public_id"]),
                    now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
                )
            ),
        ),
        api.get(
            ME,
            headers=bearer(
                create_access_token(
                    Settings(environment="test", secret_key=OTHER_SECRET),
                    uuid.UUID(account["public_id"]),
                )
            ),
        ),
        api.get(ME, headers=bearer(create_access_token(settings, uuid.uuid4()))),
    ]

    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1


def test_the_401_uses_the_shared_error_envelope(api: TestClient) -> None:
    body = api.get(ME).json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_the_401_leaks_no_implementation_detail(api: TestClient, account: dict) -> None:
    texts = [
        api.get(ME).text,
        api.get(ME, headers=bearer("garbage")).text,
    ]

    for text in texts:
        for leak in ["jwt", "PyJWT", "signature", "algorithm", "argon2", "psycopg", "Traceback"]:
            assert leak.lower() not in text.lower(), f"leaked {leak!r}"


def test_the_secret_never_appears_in_any_response(api: TestClient, account: dict) -> None:
    for response in (
        api.post(REGISTER, json=registration(email="other@example.test")),
        api.post(LOGIN, json={"email": EMAIL, "password": PASSWORD}),
        api.get(ME, headers=bearer(sign_in(api))),
        api.get(ME),
    ):
        assert SECRET not in response.text


# --- the rest of the API is closed, and closed the right way --------------------------------------
#
# Stage 4.1 asserted the OPPOSITE of both tests below: that the domain was still open, and that
# a token bought no access decision. That was deliberate -- protecting the domain before the
# authorization stage designed it would have turned 4.2 into a debugging exercise. Stage 4.2
# was authorised to close it, so these invert rather than disappear. What they pin now is the
# ORDER of the two failures, which is the part that is easy to get subtly wrong.


@pytest.mark.parametrize(
    "path", ["/api/v1/hotels", "/api/v1/amenities", "/api/v1/revenue-categories"]
)
def test_the_domain_endpoints_require_authentication(api: TestClient, path: str) -> None:
    """Including the global catalogues: readable by any account, by no anonymous caller."""
    assert api.get(path).status_code == 401


def test_a_token_is_demanded_before_a_hotel_is_looked_up(api: TestClient, account: dict) -> None:
    """401 before 404, and 404 after.

    An anonymous caller must be told to authenticate rather than told the hotel does not
    exist -- otherwise the unauthenticated surface becomes an existence oracle for every UUID
    someone cares to try. An authenticated non-member gets the 404, which is Stage 4.2's wall
    and is asserted in depth in ``test_authorization_api.py``.
    """
    headers = bearer(sign_in(api))
    unknown = str(uuid.uuid4())

    assert api.get(f"/api/v1/hotels/{unknown}").status_code == 401
    assert api.get(f"/api/v1/hotels/{unknown}", headers=headers).status_code == 404


def test_authentication_touches_no_domain_table(api: TestClient, session: Session) -> None:
    """Registering and signing in must not create a hotel, a guest or anything else."""
    from app.models import Booking, Guest, Hotel

    api.post(REGISTER, json=registration())
    sign_in(api)

    for model in (Hotel, Guest, Booking):
        assert session.scalar(sa.select(sa.func.count()).select_from(model)) == 0


def test_users_has_no_relationship_to_any_hotel(session: Session) -> None:
    """Verified against the live catalogue: identity is not owned by a tenant, and Stage 4.2
    will add membership as its own thing rather than finding it half-built here."""
    foreign_keys = session.execute(
        sa.text(
            "SELECT count(*) FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
            "WHERE c.relname = 'users' AND con.contype = 'f'"
        )
    ).scalar_one()

    assert foreign_keys == 0
