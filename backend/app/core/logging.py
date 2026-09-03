"""Application logging configuration.

Standard library only -- ``logging.config.dictConfig``, a formatter, a handler and a filter.
No third-party logging framework: thirteen modules already call ``getLogger(__name__)`` and
log short, deliberately-worded messages, and what was missing was configuration, not an
abstraction.

**The filter is what makes correlation free.** It puts the current request id on every record
as it passes a handler, so none of the thirteen existing call sites had to change and none of
them has to remember to pass it. A record emitted outside a request gets :data:`NO_REQUEST_ID`
rather than raising.

**Two decisions that look like details and are not:**

``disable_existing_loggers`` is False. Leaving it at its default is precisely the defect this
stage fixed in Alembic's ``env.py``: it would silence every ``app.*`` logger created before
this call, which -- since this runs at startup, after the modules are imported -- is all of
them.

The handler is attached to the ROOT logger, and ``app`` gets a level but no handler of its
own. Records therefore propagate, which keeps ``caplog`` working: pytest captures through a
handler on root, and giving ``app`` ``propagate=False`` would make every existing log-content
assertion in the suite silently vacuous.
"""

from __future__ import annotations

import logging
import logging.config
from typing import Any

from app.core.config import Settings
from app.core.request_id import NO_REQUEST_ID, current_request_id

#: One line per record, with the correlation id in a fixed column so a human scanning output
#: can group a request by eye and a machine can split on it.
LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    """Stamp the current request id onto every record.

    A ``Filter`` that never filters -- it always returns True. Filters are the only hook that
    runs per record and may mutate it, which is what this needs; rejecting a record for having
    no request id would throw away exactly the startup and background logs most worth keeping.

    Attached to the HANDLER rather than to a logger, because a logger's filters apply only to
    records logged directly to it. A record from ``app.services.auth`` propagating to root
    would never see a filter installed on root, but it does pass through root's handlers.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or NO_REQUEST_ID
        return True


def logging_config(settings: Settings) -> dict[str, Any]:
    """The dictConfig this application uses.

    Returned rather than applied so it can be inspected and asserted without mutating the
    logging state of the process doing the asserting.
    """
    return {
        "version": 1,
        # See the module docstring: the same trap Alembic fell into.
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {"()": f"{__name__}.RequestIdFilter"},
        },
        "formatters": {
            "standard": {"format": LOG_FORMAT},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "standard",
                "filters": ["request_id"],
            },
        },
        # The handler lives here so records from every logger reach it by propagation.
        "root": {
            "handlers": ["console"],
            "level": "WARNING",
        },
        "loggers": {
            # The application's own level is the one `Settings.log_level` governs. Third-party
            # loggers keep the root level, so turning the application up to DEBUG does not also
            # turn on SQLAlchemy's statement logging -- which would print bound parameters and
            # undo `hide_parameters`.
            "app": {"level": settings.log_level},
        },
    }


def configure_logging(settings: Settings) -> None:
    """Apply the configuration. Called once, from the application lifespan.

    **Not from ``create_app``.** Logging is process-global while applications are not: the test
    suite builds hundreds of them, and reconfiguring global logging per instance would replace
    root's handlers underneath whatever was already capturing -- a test-isolation bug of
    exactly the kind this stage was opened to fix.
    """
    logging.config.dictConfig(logging_config(settings))


__all__ = ["LOG_FORMAT", "RequestIdFilter", "configure_logging", "logging_config"]
