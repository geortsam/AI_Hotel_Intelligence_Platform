"""Grant or revoke platform administration, audited (R1.3a).

    docker compose run --rm api python -m app.jobs.platform_admin grant ops@example.com
    docker compose run --rm api python -m app.jobs.platform_admin revoke ops@example.com

The operator's way to perform the grant that was an out-of-band INSERT into
``platform_admins`` (docs/deployment/first-run-bootstrap.md), now inside the application, so
that each change is recorded in ``audit_events`` -- ``platform_admin.granted`` or
``platform_admin.revoked``, about the ``user``, in the same transaction as the change.

**Not an API, on purpose.** Who may grant or revoke through the API is an open decision (R1.3b).
This command is reachable only by someone who can run code against the database, which is the
trust boundary the INSERT had; it adds no new way to become an administrator. The events have
no actor, because no authenticated user caused them, and no hotel, because a platform grant
belongs to none; they appear in ``/api/v1/platform/audit-events``.

**What it refuses.**

- An email no registered user has: register first. Nothing is created here but the grant.
- A grant to a deactivated account.
- Revoking the last administrator, unless ``--allow-none`` says that is intended: an
  installation with none cannot write its shared catalogues until someone runs ``grant``.

**Repeating is harmless.** Granting to an administrator, or revoking from someone who is not one,
changes nothing and records nothing, and exits 0.

**What it prints.** One line saying what changed, and how many administrators there now are.
It never prints the email, an identifier or a connection string; on failure only the
exception's type.

Exit status: 0 when the change was made or was not needed; 1 when it was refused or failed
(nothing is changed: the grant and its event commit together or not at all); 2 for a usage
error.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_db_engine, create_session_factory
from app.models.enums import AuditAction, AuditResourceType, PlatformRole
from app.models.platform import PlatformAdmin
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.services.audit import AuditTrail


class RefusedError(Exception):
    """The change was not made, for a reason the operator can act on. The message is safe."""


def administrators(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(PlatformAdmin)) or 0


def find_user(session: Session, email: str) -> User:
    user = session.scalars(select(User).where(User.email == email.strip().lower())).one_or_none()
    if user is None:
        raise RefusedError("no registered user has that email; register the account first")
    return user


def grant(session: Session, email: str) -> str:
    user = find_user(session, email)
    if session.get(PlatformAdmin, user.id) is not None:
        return "already a platform administrator; nothing changed"
    if not user.is_active:
        raise RefusedError("the account is deactivated; a grant to it was refused")
    session.add(PlatformAdmin(user_id=user.id, role=PlatformRole.PLATFORM_ADMIN.value))
    AuditTrail(AuditRepository(session)).record(
        AuditAction.PLATFORM_ADMIN_GRANTED,
        AuditResourceType.USER,
        str(user.public_id),
        details={"role": PlatformRole.PLATFORM_ADMIN.value},
    )
    session.flush()
    return "platform administration granted"


def revoke(session: Session, email: str, *, allow_none: bool) -> str:
    user = find_user(session, email)
    # Every grant row is locked before counting, so two revocations running at once cannot
    # each see another administrator and together leave none.
    granted = session.scalars(select(PlatformAdmin).with_for_update()).all()
    held = next((row for row in granted if row.user_id == user.id), None)
    if held is None:
        return "not a platform administrator; nothing changed"
    if len(granted) == 1 and not allow_none:
        raise RefusedError(
            "that is the last platform administrator; pass --allow-none to leave the "
            "installation with none"
        )
    session.delete(held)
    AuditTrail(AuditRepository(session)).record(
        AuditAction.PLATFORM_ADMIN_REVOKED,
        AuditResourceType.USER,
        str(user.public_id),
        details={"role": held.role},
    )
    session.flush()
    return "platform administration revoked"


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    configure_logs: bool = False,
) -> int:
    """Parse the arguments and make one change. ``settings`` is injectable for tests."""
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.platform_admin",
        description="Grant or revoke platform administration, recorded in the audit trail.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    granting = commands.add_parser("grant", help="make a registered user a platform administrator")
    granting.add_argument("email")
    revoking = commands.add_parser("revoke", help="remove a user's platform administration")
    revoking.add_argument("email")
    revoking.add_argument(
        "--allow-none",
        action="store_true",
        help="allow revoking the last administrator, leaving the installation with none",
    )
    args = parser.parse_args(argv)

    try:
        settings = settings or get_settings()
        if configure_logs:
            configure_logging(settings)
        engine = create_db_engine(settings)
        try:
            session = create_session_factory(engine)()
            try:
                if args.command == "grant":
                    outcome = grant(session, args.email)
                else:
                    outcome = revoke(session, args.email, allow_none=args.allow_none)
                remaining = administrators(session)
                session.commit()
            finally:
                session.close()
        finally:
            engine.dispose()
    except RefusedError as refusal:
        print(f"platform administration: refused: {refusal}; nothing changed", file=sys.stderr)
        return 1
    except Exception as exc:
        # The type only: a driver message can carry SQL, and a configuration error the URL.
        print(
            f"platform administration FAILED ({type(exc).__name__}); nothing changed",
            file=sys.stderr,
        )
        return 1

    print(f"platform administration: {outcome}; {remaining} administrator(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the tests
    raise SystemExit(main(configure_logs=True))
