"""Request counts and latencies, rendered in the Prometheus text exposition format (A5).

The local half of roadmap R4.3a: an exporter, internal only, with no alert destination. Where
alerts go is the open decision of docs/roadmap-v2.0-execution.md §3.12; nothing here sends
anything anywhere. A scraper on the internal network pulls ``GET /metrics``
(``app.api.routes.metrics``); nginx has no route to it.

Two metric families, and nothing else:

* ``http_requests_total`` (counter): requests answered, by ``method``, ``route`` and ``status``.
* ``http_request_duration_seconds`` (histogram): the time from the request reaching the
  application to the end of its response, by ``method`` and ``route``, in fixed buckets.

**Bounded labels.** ``route`` is the matched route's *template* (``/api/v1/hotels/{hotel_id}``),
never the requested path, so no identifier, e-mail address or query string can become a label
value, and a scanner probing random paths adds one series (``unmatched``), not one per path.
``method`` is one of the seven methods the API can route, or ``OTHER``. ``status`` is the
three-digit code. The series count is bounded by routes x methods x statuses.

**In process.** The figures live in the worker's memory and start at zero with it, as a
Prometheus client's do; the image runs one uvicorn worker, so one scrape sees every request.
No database is read, so the endpoint answers when PostgreSQL does not.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable

#: What Prometheus expects of a text exposition (format 0.0.4).
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

REQUESTS_TOTAL = "http_requests_total"
REQUEST_DURATION = "http_request_duration_seconds"

#: Upper bounds of the latency histogram's buckets, in seconds; ``+Inf`` is implied.
BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

#: The methods a route can declare. Anything else is counted as ``OTHER``, so a client cannot
#: mint a series by sending an invented method.
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
OTHER_METHOD = "OTHER"

#: The route label of a request no route matched: a 404, a CORS preflight answered by the
#: middleware, a body refused before routing.
UNMATCHED = "unmatched"

_HELP = {
    REQUESTS_TOTAL: "HTTP requests answered, by method, route template and status code.",
    REQUEST_DURATION: "Time to answer an HTTP request, in seconds, by method and route template.",
}


def method_label(method: str) -> str:
    upper = method.upper()
    return upper if upper in METHODS else OTHER_METHOD


def status_label(status: int) -> str:
    return str(status) if 100 <= status <= 599 else "other"


def escape_label_value(value: str) -> str:
    """The exposition format's three escapes: backslash, double quote and line feed."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_value(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    return repr(value)


def _labels(pairs: Iterable[tuple[str, str]]) -> str:
    return "{" + ",".join(f'{name}="{escape_label_value(value)}"' for name, value in pairs) + "}"


class _Histogram:
    __slots__ = ("buckets", "count", "total")

    def __init__(self) -> None:
        self.buckets = [0] * len(BUCKETS)
        self.count = 0
        self.total = 0.0

    def observe(self, seconds: float) -> None:
        for index, bound in enumerate(BUCKETS):
            if seconds <= bound:
                self.buckets[index] += 1
        self.count += 1
        self.total += seconds


class RequestMetrics:
    """The application's request counters and latency histograms, safe across threads.

    Sync endpoints run in a thread pool, so two requests can finish at once; one lock guards
    every update and every render, which is cheap next to a request.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str, str], int] = {}
        self._durations: dict[tuple[str, str], _Histogram] = {}

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        method = method_label(method)
        seconds = max(0.0, seconds)
        with self._lock:
            key = (method, route, status_label(status))
            self._counts[key] = self._counts.get(key, 0) + 1
            histogram = self._durations.get((method, route))
            if histogram is None:
                histogram = self._durations[(method, route)] = _Histogram()
            histogram.observe(seconds)

    def render(self) -> str:
        """The exposition text. With nothing observed yet, the two families and no samples."""
        with self._lock:
            counts = sorted(self._counts.items())
            durations = sorted(
                (key, (list(h.buckets), h.count, h.total)) for key, h in self._durations.items()
            )
        lines = [
            f"# HELP {REQUESTS_TOTAL} {_HELP[REQUESTS_TOTAL]}",
            f"# TYPE {REQUESTS_TOTAL} counter",
        ]
        for (method, route, status), count in counts:
            labels = _labels((("method", method), ("route", route), ("status", status)))
            lines.append(f"{REQUESTS_TOTAL}{labels} {count}")
        lines += [
            f"# HELP {REQUEST_DURATION} {_HELP[REQUEST_DURATION]}",
            f"# TYPE {REQUEST_DURATION} histogram",
        ]
        for (method, route), (buckets, count, total) in durations:
            base = (("method", method), ("route", route))
            for bound, cumulative in zip(BUCKETS, buckets, strict=True):
                labels = _labels((*base, ("le", format_value(bound))))
                lines.append(f"{REQUEST_DURATION}_bucket{labels} {cumulative}")
            lines.append(f"{REQUEST_DURATION}_bucket{_labels((*base, ('le', '+Inf')))} {count}")
            lines.append(f"{REQUEST_DURATION}_sum{_labels(base)} {format_value(total)}")
            lines.append(f"{REQUEST_DURATION}_count{_labels(base)} {count}")
        return "\n".join(lines) + "\n"
