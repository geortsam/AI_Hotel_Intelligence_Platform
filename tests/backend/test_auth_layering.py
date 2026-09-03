"""Authentication: cryptography, layering, and the line this stage must not cross.

Two halves. The first exercises ``app.core.security`` directly -- hashing, verification and
token handling are testable without a database, and if the arithmetic there is wrong nothing
downstream can save it. The second pins the structure: no plaintext storage, no hash in any
response schema, no hard-coded secret, and **no authorization**, which belongs to Stage 4.2.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import uuid
from types import ModuleType

import jwt
import pytest

from app.api import deps
from app.api.v1.endpoints import auth as auth_router
from app.core import security
from app.core.config import Settings
from app.core.security import (
    ACCESS_TOKEN_TTL,
    ALGORITHM,
    TOKEN_TYPE_ACCESS,
    SecretNotConfiguredError,
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    require_secret,
    verify_password,
)
from app.main import create_app
from app.models.user import User
from app.repositories import user as user_repository_module
from app.schemas.auth import (
    LoginRequest,
    RegistrationRequest,
    TokenResponse,
    UserResponse,
)
from app.services import auth as auth_service_module
from app.services.auth import AuthenticationError, AuthService, InvalidTokenError

SECRET = "a-test-signing-secret-that-is-not-in-production"
OTHER_SECRET = "a-completely-different-signing-secret"
PASSWORD = "correct horse battery staple"

AUTH_MODULES: list[ModuleType] = [security, auth_service_module, auth_router]


def settings(secret: str | None = SECRET) -> Settings:
    return Settings(environment="test", secret_key=secret)


def code_of(module: ModuleType) -> str:
    """Module source with docstrings removed, so prose about an absent construct cannot read
    as that construct being present."""
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def identifiers(module: ModuleType) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[-1])
    return found


# --- password hashing --------------------------------------------------------------------------


def test_the_algorithm_is_argon2id() -> None:
    """Not argon2i or argon2d: the id variant is the one resistant to both side-channel and
    GPU attacks, and it is what the digest must say."""
    assert hash_password(PASSWORD).startswith("$argon2id$")


def test_hashing_is_non_deterministic() -> None:
    """Each digest carries its own random salt, so two hashes of one password differ --
    which is what stops a stolen table being attacked with one rainbow table."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_the_digest_does_not_contain_the_password() -> None:
    digest = hash_password(PASSWORD)

    assert PASSWORD not in digest
    for word in PASSWORD.split():
        assert word not in digest


def test_the_correct_password_verifies() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


def test_an_incorrect_password_fails() -> None:
    assert verify_password("wrong password entirely", hash_password(PASSWORD)) is False


def test_a_near_miss_fails_like_any_other_miss() -> None:
    """No partial credit: one character off is simply wrong."""
    assert verify_password(PASSWORD + "!", hash_password(PASSWORD)) is False
    assert verify_password(PASSWORD[:-1], hash_password(PASSWORD)) is False


@pytest.mark.parametrize("corrupt", ["", "not-a-hash", "$argon2id$broken", "$2b$12$bcryptish"])
def test_a_corrupt_or_foreign_digest_returns_false_rather_than_raising(corrupt: str) -> None:
    """A stored value that is not one of our digests must be a failed login, not a 500 --
    and must not be distinguishable from a wrong password."""
    assert verify_password(PASSWORD, corrupt) is False


def test_verification_is_case_and_whitespace_sensitive() -> None:
    digest = hash_password(PASSWORD)

    assert verify_password(PASSWORD.upper(), digest) is False
    assert verify_password(" " + PASSWORD, digest) is False


# --- the signing secret ------------------------------------------------------------------------


def test_no_secret_is_hard_coded_anywhere_in_the_auth_code() -> None:
    """A default in source control is worse than a missing one: it fails silently, in
    production, with a key anybody can read."""
    for module in AUTH_MODULES:
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id.lower() for t in targets if isinstance(t, ast.Name)}
            if not any(("secret" in n or "key" in n or "password" in n) for n in names):
                continue
            # A literal bound to a credential-shaped name is the bug. Reading one from
            # configuration -- `secret = settings.secret_key` -- is the fix.
            assert not isinstance(node.value, ast.Constant) or not isinstance(
                node.value.value, str
            ), f"{module.__name__} binds a literal to {names}"


def test_a_missing_secret_raises_rather_than_falling_back() -> None:
    with pytest.raises(SecretNotConfiguredError):
        require_secret(settings(None))


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_secret_is_treated_as_missing(blank: str) -> None:
    with pytest.raises(SecretNotConfiguredError):
        require_secret(settings(blank))


def test_signing_without_a_secret_raises_rather_than_signing() -> None:
    """The failure must happen instead of producing a token, not alongside one."""
    with pytest.raises(SecretNotConfiguredError):
        create_access_token(settings(None), uuid.uuid4())


def test_production_refuses_to_start_without_a_secret() -> None:
    """Failing at startup beats failing at the first login."""
    with pytest.raises(ValueError, match="SECRET_KEY must be set in production"):
        Settings(environment="production", secret_key=None)


def test_production_starts_when_the_secret_is_present() -> None:
    assert Settings(environment="production", secret_key=SECRET).secret_key == SECRET


def test_non_production_may_omit_the_secret_but_still_cannot_sign() -> None:
    """Omitting it is allowed for local work; it just means no tokens."""
    configured = Settings(environment="development", secret_key=None)

    with pytest.raises(SecretNotConfiguredError):
        create_access_token(configured, uuid.uuid4())


# --- token claims ------------------------------------------------------------------------------


def claims_of(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM])


def test_the_claims_are_exactly_the_five_that_were_specified() -> None:
    """A JWT is signed but NOT encrypted -- every claim is readable by anyone holding it."""
    subject = uuid.uuid4()

    assert set(claims_of(create_access_token(settings(), subject))) == {
        "sub",
        "iat",
        "exp",
        "jti",
        "typ",
    }


def test_the_subject_is_the_public_id() -> None:
    subject = uuid.uuid4()

    assert claims_of(create_access_token(settings(), subject))["sub"] == str(subject)


def test_no_email_name_permission_or_hotel_appears_in_the_token() -> None:
    """Identity only. Permissions are Stage 4.2's, and a claim duplicating application state
    is stale the moment that state changes."""
    claims = claims_of(create_access_token(settings(), uuid.uuid4()))

    for forbidden in [
        "email",
        "name",
        "full_name",
        "password",
        "password_hash",
        "role",
        "roles",
        "permissions",
        "scopes",
        "hotel",
        "hotel_id",
        "hotels",
        "is_active",
    ]:
        assert forbidden not in claims, f"token carries {forbidden!r}"


def test_no_password_material_survives_into_the_token_text() -> None:
    token = create_access_token(settings(), uuid.uuid4())

    assert PASSWORD not in token
    assert SECRET not in token


def test_each_token_has_a_unique_identifier() -> None:
    """jti gives a future revocation list something to name."""
    subject = uuid.uuid4()
    first = claims_of(create_access_token(settings(), subject))
    second = claims_of(create_access_token(settings(), subject))

    assert first["jti"] != second["jti"]


def test_the_token_type_marks_it_as_an_access_token() -> None:
    assert claims_of(create_access_token(settings(), uuid.uuid4()))["typ"] == TOKEN_TYPE_ACCESS


def test_the_token_is_short_lived() -> None:
    claims = claims_of(create_access_token(settings(), uuid.uuid4()))

    assert claims["exp"] - claims["iat"] == int(ACCESS_TOKEN_TTL.total_seconds())
    assert dt.timedelta(hours=1) >= ACCESS_TOKEN_TTL


# --- token verification ------------------------------------------------------------------------


def test_a_valid_token_resolves_to_its_subject() -> None:
    """Stage 4.5.2 widened the return value from the subject to the claims the application
    reads. ``iat`` was already in the token; nothing was added to it."""
    subject = uuid.uuid4()

    claims = decode_access_token(settings(), create_access_token(settings(), subject))

    assert claims.subject == subject
    assert isinstance(claims.issued_at, int)


def test_a_token_signed_with_another_secret_is_rejected() -> None:
    """The signature is the whole point; a different key must not verify."""
    token = create_access_token(settings(OTHER_SECRET), uuid.uuid4())

    with pytest.raises(TokenError):
        decode_access_token(settings(SECRET), token)


def test_a_tampered_payload_is_rejected() -> None:
    """Flipping a byte of the payload invalidates the signature over it."""
    token = create_access_token(settings(), uuid.uuid4())
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-2]}XY.{signature}"

    with pytest.raises(TokenError):
        decode_access_token(settings(), tampered)


def test_a_stripped_signature_is_rejected() -> None:
    token = create_access_token(settings(), uuid.uuid4())
    header, payload, _ = token.split(".")

    with pytest.raises(TokenError):
        decode_access_token(settings(), f"{header}.{payload}.")


@pytest.mark.parametrize(
    "malformed", ["", "   ", "not-a-token", "a.b", "a.b.c.d", "....", "Bearer x.y.z"]
)
def test_malformed_tokens_are_rejected(malformed: str) -> None:
    with pytest.raises(TokenError):
        decode_access_token(settings(), malformed)


def test_an_expired_token_is_rejected() -> None:
    """Issued an hour ago with a thirty-minute life."""
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    token = create_access_token(settings(), uuid.uuid4(), now=past)

    with pytest.raises(TokenError):
        decode_access_token(settings(), token)


def test_a_token_expiring_in_a_second_still_works() -> None:
    """The boundary is not off by one."""
    subject = uuid.uuid4()
    token = create_access_token(settings(), subject, expires_in=dt.timedelta(seconds=30))

    assert decode_access_token(settings(), token).subject == subject


def test_the_none_algorithm_is_rejected() -> None:
    """Algorithm confusion, the classic JWT attack: a token whose header claims no signature.
    Pinning algorithms=["HS256"] on decode is the entire defence."""
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iat": int(dt.datetime.now(dt.UTC).timestamp()),
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
            "jti": str(uuid.uuid4()),
            "typ": TOKEN_TYPE_ACCESS,
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(TokenError):
        decode_access_token(settings(), forged)


def test_the_decoder_pins_the_algorithm_explicitly() -> None:
    """Without the pin PyJWT would trust the token's own `alg` header."""
    source = inspect.getsource(security.decode_access_token)

    assert "algorithms=[ALGORITHM]" in source
    assert ALGORITHM == "HS256"


def test_a_token_with_no_subject_is_rejected() -> None:
    token = jwt.encode(
        {
            "iat": int(dt.datetime.now(dt.UTC).timestamp()),
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
            "typ": TOKEN_TYPE_ACCESS,
        },
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(TokenError):
        decode_access_token(settings(), token)


def test_a_subject_that_is_not_a_uuid_is_rejected() -> None:
    token = jwt.encode(
        {
            "sub": "1",  # a sequential id would be an enumeration handle
            "iat": int(dt.datetime.now(dt.UTC).timestamp()),
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
            "typ": TOKEN_TYPE_ACCESS,
        },
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(TokenError):
        decode_access_token(settings(), token)


def test_a_token_of_the_wrong_type_is_rejected() -> None:
    """A future refresh token must not be usable as an access token."""
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iat": int(dt.datetime.now(dt.UTC).timestamp()),
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
            "typ": "refresh",
        },
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(TokenError):
        decode_access_token(settings(), token)


def test_every_rejection_raises_the_same_exception_type() -> None:
    """The caller turns all of them into one identical 401, so an attacker cannot tell
    expired from forged from malformed."""
    expired = create_access_token(
        settings(), uuid.uuid4(), now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    )
    forged = create_access_token(settings(OTHER_SECRET), uuid.uuid4())

    for bad in (expired, forged, "garbage", ""):
        with pytest.raises(TokenError):
            decode_access_token(settings(), bad)


# --- no password or hash in any contract -------------------------------------------------------


def test_no_response_schema_has_a_password_or_hash_field() -> None:
    for schema in (UserResponse, TokenResponse):
        fields = set(schema.model_fields)
        for forbidden in ["password", "password_hash", "hash", "salt", "secret"]:
            assert forbidden not in fields, f"{schema.__name__} exposes {forbidden!r}"


def test_the_plaintext_password_appears_only_in_request_schemas() -> None:
    assert "password" in RegistrationRequest.model_fields
    assert "password" in LoginRequest.model_fields
    assert "password" not in UserResponse.model_fields


def test_no_openapi_component_exposes_a_password_hash() -> None:
    """Audited across every generated schema, not the two we happen to remember."""
    schemas = create_app(Settings(environment="test")).openapi()["components"]["schemas"]

    offenders = {
        f"{name}.{field}"
        for name, schema in schemas.items()
        for field in schema.get("properties", {})
        if field in {"password_hash", "hash", "salt", "secret_key"}
    }
    assert offenders == set()


def test_the_user_response_is_built_field_by_field() -> None:
    """Not from_attributes over the ORM row: a column added to `users` later must not start
    appearing in a payload by default."""
    assert UserResponse.model_config.get("from_attributes") is not True
    assert "password_hash" not in UserResponse.model_fields


def test_registration_cannot_set_privileged_state() -> None:
    """No self-activation, no self-promotion, and nothing hotel-shaped."""
    fields = set(RegistrationRequest.model_fields)

    assert fields == {"email", "password", "full_name"}
    for forbidden in ["is_active", "role", "permissions", "hotel_id", "public_id", "id"]:
        assert forbidden not in fields


def test_request_schemas_forbid_unknown_fields() -> None:
    for schema in (RegistrationRequest, LoginRequest):
        assert schema.model_config.get("extra") == "forbid", schema.__name__


def test_login_does_not_impose_a_password_length_floor() -> None:
    """Rejecting a short password at login would tell an attacker their guess was too short
    to be this account's password."""
    parsed = LoginRequest.model_validate({"email": "a@b.co", "password": "x"})

    assert parsed.password == "x"


# --- layering ---------------------------------------------------------------------------------


def test_the_security_module_touches_no_database_or_http() -> None:
    """Pure cryptography: it turns a password into a digest and an identity into a token."""
    names = identifiers(security)

    for forbidden in ["Session", "select", "session", "HTTPException", "Request", "Response"]:
        assert forbidden not in names, f"security references {forbidden!r}"


def test_the_repository_neither_hashes_nor_verifies() -> None:
    """It stores whatever digest it is handed. Keeping Argon2id out of the data layer means
    the algorithm can change without touching persistence."""
    names = identifiers(user_repository_module)

    for forbidden in ["hash_password", "verify_password", "PasswordHasher", "jwt", "argon2"]:
        assert forbidden not in names, f"repository references {forbidden!r}"


def test_the_repository_never_commits() -> None:
    names = identifiers(user_repository_module)

    assert "commit" not in names
    assert "rollback" not in names


def test_the_repository_raises_no_domain_errors() -> None:
    names = identifiers(user_repository_module)

    for forbidden in ["AuthenticationError", "InvalidTokenError", "ConflictError", "AppError"]:
        assert forbidden not in names


def test_the_service_owns_the_transaction_boundary() -> None:
    source = code_of(auth_service_module)

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


def test_the_router_contains_no_database_logic() -> None:
    names = identifiers(auth_router)

    for forbidden in ["select", "Session", "commit", "rollback", "User", "UserRepository"]:
        assert forbidden not in names, f"router references {forbidden!r}"


def test_the_router_does_not_hash_or_decode_tokens_itself() -> None:
    names = identifiers(auth_router)

    for forbidden in ["hash_password", "verify_password", "decode_access_token", "jwt"]:
        assert forbidden not in names


def test_the_service_never_logs_a_driver_exception() -> None:
    """The driver renders the offending row, which for this table means the email address."""
    assert "exc_info" not in code_of(auth_service_module)


def test_no_module_logs_the_password_or_the_hash() -> None:
    credential_names = {"password", "password_hash", "secret", "secret_key", "token"}

    for module in AUTH_MODULES:
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            is_log = (
                isinstance(target, ast.Attribute)
                and target.attr in {"debug", "info", "warning", "error", "critical", "exception"}
            ) or (isinstance(target, ast.Name) and target.id == "print")
            if not is_log:
                continue
            referenced = {
                inner.id
                for arg in [*node.args, *(kw.value for kw in node.keywords)]
                for inner in ast.walk(arg)
                if isinstance(inner, ast.Name)
            } | {
                inner.attr
                for arg in [*node.args, *(kw.value for kw in node.keywords)]
                for inner in ast.walk(arg)
                if isinstance(inner, ast.Attribute)
            }
            leaked = referenced & credential_names
            assert leaked == set(), f"{module.__name__} logs {leaked}"


# --- authorization is NOT in this stage ---------------------------------------------------------


def test_no_authorization_decision_exists_anywhere_in_authentication() -> None:
    """Stage 4.1 establishes WHO. Stage 4.2 decides WHAT. Mixing them is how an
    authorization model ends up half-implemented in two places."""
    for module in AUTH_MODULES:
        names = identifiers(module)
        for forbidden in [
            "hotel_id",
            "hotel_public_id",
            "require_hotel",
            "HotelScopeResolver",
            "role",
            "roles",
            "permissions",
            "scopes",
            "can_access",
            "authorize",
            "is_admin",
        ]:
            assert forbidden not in names, f"{module.__name__} references {forbidden!r}"


def test_the_current_user_dependency_takes_no_hotel() -> None:
    """It cannot quietly grow an authorization argument if it never had one."""
    parameters = set(inspect.signature(deps.get_current_user).parameters)

    assert parameters == {"service", "credentials"}


def test_the_service_surface_is_exactly_the_declared_operations() -> None:
    """Pinned so a fifth operation is a decision rather than a drift.

    Stage 4.5.1 added ``change_password``: account-credential state, which belongs beside
    the other two operations that read or write a credential and nowhere else.
    """
    methods = {name for name in dir(AuthService) if not name.startswith("_")}

    assert methods == {"register", "authenticate", "resolve_token", "change_password"}


def test_the_user_model_carries_no_authorization_column() -> None:
    """The migration was authorised for identity only."""
    columns = set(User.metadata.tables["users"].columns.keys())

    assert columns == {
        "id",
        "public_id",
        "email",
        "password_hash",
        "full_name",
        "is_active",
        "last_login_at",
        "created_at",
        "updated_at",
        # Stage 4.5.2. A security TIMESTAMP, not an authorization column: it says when the
        # credential was last set, and answers exactly one question -- "was this token minted
        # before the password it authenticates?". It grants nothing and names no hotel.
        "password_changed_at",
    }
    for forbidden in ["hotel_id", "role", "permissions", "scopes", "guest_id"]:
        assert forbidden not in columns


def test_the_user_table_references_nothing() -> None:
    """No foreign key at all -- identity is not owned by a tenant."""
    assert User.metadata.tables["users"].foreign_keys == set()


#: Every operation the published contract offers without a token.
#:
#: Registration and login cannot require the token they exist to obtain; the probes are read by
#: infrastructure that holds no account; ``GET /api/v1/`` is the version banner. Everything else
#: is authenticated. Adding a line here widens the anonymous surface, which is why it is a
#: literal rather than a rule with an escape hatch.
PUBLIC_OPERATIONS = {
    "GET /api/v1/",
    "GET /health",
    "GET /health/db",
    "POST /api/v1/auth/login",
    "POST /api/v1/auth/register",
}


def public_operations() -> set[str]:
    paths = create_app(Settings(environment="test")).openapi()["paths"]
    return {
        f"{verb.upper()} {path}"
        for path, operations in paths.items()
        for verb, operation in operations.items()
        if "security" not in operation
    }


def test_the_published_contract_is_open_only_where_it_must_be() -> None:
    """Stage 4.1 asserted the exact opposite of this, on purpose.

    That stage was scoped to build authentication WITHOUT protecting the domain, so that the
    authorization stage would be a design exercise rather than a debugging one; it therefore
    asserted every domain operation was still open, and that only ``/auth/me`` was protected.
    Stage 4.2 was authorised to close them, so the assertion inverts rather than disappears --
    the contract is still pinned, just to the other side of that decision.
    """
    assert public_operations() == PUBLIC_OPERATIONS


def test_the_contract_actually_protects_the_domain() -> None:
    """Guards the guard: an OpenAPI generator that emitted no security at all would make the
    test above pass by making every operation public."""
    paths = create_app(Settings(environment="test")).openapi()["paths"]
    secured = [
        f"{verb.upper()} {path}"
        for path, operations in paths.items()
        for verb, operation in operations.items()
        if "security" in operation
    ]

    assert len(secured) > 60
    assert "GET /api/v1/auth/me" in secured
    assert "POST /api/v1/hotels" in secured


# --- the same failure, whatever the reason -------------------------------------------------------


def test_authentication_failures_share_one_message() -> None:
    """Unknown address, wrong password and disabled account must be indistinguishable."""
    assert AuthenticationError().message == "Invalid email or password."
    assert AuthenticationError.status_code == 401


def test_the_message_names_neither_the_address_nor_the_reason() -> None:
    message = AuthenticationError().message

    for leak in ["not found", "does not exist", "disabled", "inactive", "unknown", "no such"]:
        assert leak not in message.lower()


def test_the_service_raises_the_identical_error_on_every_path() -> None:
    """Three rejection sites, one exception, no arguments -- so none of them can accidentally
    grow a distinguishing message."""
    source = inspect.getsource(AuthService.authenticate)

    assert source.count("raise AuthenticationError") == 3
    assert "AuthenticationError(" not in source


def test_an_unknown_address_still_pays_for_a_verification() -> None:
    """Skipping the Argon2id computation on a miss would make it measurably faster, which is
    an account-enumeration oracle by timing."""
    source = inspect.getsource(AuthService.authenticate)

    assert "verify_password(password, _DUMMY_HASH)" in source


def test_the_dummy_hash_is_a_real_argon2id_digest() -> None:
    assert auth_service_module._DUMMY_HASH.startswith("$argon2id$")


def test_the_invalid_token_error_says_nothing_about_why() -> None:
    message = InvalidTokenError().message

    assert InvalidTokenError.status_code == 401
    for leak in ["expired", "signature", "malformed", "algorithm", "secret"]:
        assert leak not in message.lower()
