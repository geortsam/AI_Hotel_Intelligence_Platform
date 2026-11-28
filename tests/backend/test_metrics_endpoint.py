"""A5: the internal metrics exporter -- request counts and latencies in Prometheus format.

What is held here, all without a database (``tests/backend`` has none; the stub session below
is never used, which is itself the proof that nothing measured needs one):

* the endpoint answers in the text exposition format, every line of which parses;
* before any request it declares the two families and holds no sample;
* requests are counted by route TEMPLATE, never by path, so no identifier, e-mail address or
  query string becomes a label value, and unknown paths share one series;
* the histogram is cumulative and agrees with the counter;
* an exception is counted as the 500 it becomes, a refused body as its 413;
* a proxied request is refused with the application's ordinary 404;
* it is outside the OpenAPI document, nginx has no route to it, and the API port is unpublished;
* the output carries no secret, no database URL and no environment value.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.routes.metrics import FORWARDING_HEADERS
from app.core import metrics as m
from app.core.config import Settings
from app.main import create_app
from app.middleware.metrics import RequestMetricsMiddleware, route_label

ROOT = Path(__file__).resolve().parents[2]
SECRET = "metrics-suite-signing-secret-not-real-7f3a"
DATABASE_URL = "postgresql+psycopg://metrics_user:metrics-db-password-9c1e@127.0.0.1:1/metrics_db"
HOTEL = "3f0e7a52-9c1d-4d7b-8a43-2b6f8e1c5d90"
BOOKINGS_TEMPLATE = "/api/v1/hotels/{hotel_public_id}/bookings"

SAMPLE = re.compile(
    r"(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r'(?:\{(?P<labels>(?:[^"}]|"(?:[^"\\]|\\.)*")*)\})?'
    r" (?P<value>[-+]?(?:[0-9]+(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?|Inf|NaN))"
)
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\[\\"n])*)"')


def parse(text: str) -> tuple[dict[str, str], list[tuple[str, dict[str, str], float]]]:
    """A strict reader of the 0.0.4 text format: TYPE lines, samples, nothing malformed."""
    assert text.endswith("\n")
    types: dict[str, str] = {}
    samples: list[tuple[str, dict[str, str], float]] = []
    for line in text[:-1].split("\n"):
        if line.startswith("# HELP "):
            assert re.fullmatch(r"# HELP [a-zA-Z_:][a-zA-Z0-9_:]* \S.*", line), line
        elif line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert kind in {"counter", "gauge", "histogram", "summary", "untyped"}, line
            assert name not in types, f"{name} declared twice"
            types[name] = kind
        else:
            match = SAMPLE.fullmatch(line)
            assert match, f"not a sample line: {line!r}"
            raw = match.group("labels") or ""
            labels = dict(LABEL.findall(raw))
            assert ",".join(f'{k}="{v}"' for k, v in LABEL.findall(raw)) == raw, line
            family = re.sub(r"_(bucket|sum|count)$", "", match.group("name"))
            assert family in types or match.group("name") in types, f"undeclared: {line}"
            samples.append((match.group("name"), labels, float(match.group("value"))))
    return types, samples


def _stub_session() -> Iterator[Session]:
    yield cast(Session, None)


@pytest.fixture
def app() -> FastAPI:
    application = create_app(
        Settings(environment="test", secret_key=SECRET, database_url=DATABASE_URL)
    )
    application.dependency_overrides[get_db] = _stub_session

    @application.get("/probe/boom", response_model=None)
    def _boom() -> NoReturn:
        raise RuntimeError("deliberate")

    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def scrape(client: TestClient) -> tuple[dict[str, str], list[tuple[str, dict[str, str], float]]]:
    response = client.get("/metrics")
    assert response.status_code == 200
    return parse(response.text)


def count(samples: list[tuple[str, dict[str, str], float]], **labels: str) -> float:
    return sum(
        value
        for name, found, value in samples
        if name == m.REQUESTS_TOTAL and all(found.get(k) == v for k, v in labels.items())
    )


# --- the endpoint -------------------------------------------------------------------------------


def test_the_endpoint_serves_the_prometheus_text_format(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    # Spelled out, not read from the constant: a changed constant must fail here.
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    types, _ = parse(response.text)
    assert types == {m.REQUESTS_TOTAL: "counter", m.REQUEST_DURATION: "histogram"}


def test_before_any_request_the_families_are_declared_and_hold_no_sample() -> None:
    assert m.RequestMetrics().render() == (
        f"# HELP {m.REQUESTS_TOTAL} HTTP requests answered, by method, route template and"
        " status code.\n"
        f"# TYPE {m.REQUESTS_TOTAL} counter\n"
        f"# HELP {m.REQUEST_DURATION} Time to answer an HTTP request, in seconds, by method and"
        " route template.\n"
        f"# TYPE {m.REQUEST_DURATION} histogram\n"
    )


def test_the_first_scrape_sees_no_request_but_its_own_is_counted_by_the_next(
    client: TestClient,
) -> None:
    _, first = scrape(client)
    assert first == []
    _, second = scrape(client)
    assert count(second, route="/metrics", method="GET", status="200") == 1


def test_it_needs_no_credential_and_no_database(client: TestClient) -> None:
    # The settings name an unreachable database and the request carries no token: a scrape
    # must work when PostgreSQL does not, and the scraper holds no account.
    assert client.get("/metrics").status_code == 200


# --- what is counted, and under which labels ----------------------------------------------------


def test_requests_are_counted_by_route_template_never_by_path(client: TestClient) -> None:
    for _ in range(3):
        response = client.get(
            f"/api/v1/hotels/{HOTEL}/bookings", params={"guest": "jane.doe@example.com"}
        )
        assert response.status_code == 401
    text = client.get("/metrics").text
    _, samples = parse(text)
    assert count(samples, method="GET", route=BOOKINGS_TEMPLATE, status="401") == 3
    assert HOTEL not in text
    assert "jane.doe" not in text
    assert "guest=" not in text


def test_every_unknown_path_shares_one_series(client: TestClient) -> None:
    paths = [f"/scan/{n}/wp-login.php" for n in range(25)]
    for path in paths:
        assert client.get(path).status_code == 404
    text = client.get("/metrics").text
    _, samples = parse(text)
    assert count(samples, route=m.UNMATCHED, status="404") == 25
    assert "wp-login" not in text
    routes = {labels["route"] for name, labels, _ in samples if name == m.REQUESTS_TOTAL}
    assert routes == {m.UNMATCHED}


def test_an_invented_method_cannot_mint_a_series(client: TestClient) -> None:
    client.request("BREW", "/health")
    client.request("PROPFIND", "/health")
    _, samples = parse(client.get("/metrics").text)
    methods = {labels["method"] for name, labels, _ in samples if name == m.REQUESTS_TOTAL}
    assert methods == {m.OTHER_METHOD}
    assert m.method_label("get") == "GET"


def test_an_exception_is_counted_as_the_500_it_becomes(client: TestClient) -> None:
    assert client.get("/probe/boom").status_code == 500
    _, samples = parse(client.get("/metrics").text)
    assert count(samples, route="/probe/boom", status="500") == 1


def test_a_body_refused_for_size_is_counted_too(client: TestClient, app: FastAPI) -> None:
    limit = app.state.settings.max_request_body_bytes
    response = client.post(
        "/api/v1/auth/login",
        content=b"x" * (limit + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    _, samples = parse(client.get("/metrics").text)
    assert count(samples, method="POST", status="413") == 1


def test_the_latency_histogram_is_cumulative_and_agrees_with_the_counter() -> None:
    registry = m.RequestMetrics()
    for seconds in (0.003, 0.2, 0.2, 7.0, 30.0):
        registry.observe("GET", "/x", 200, seconds)
    _, samples = parse(registry.render())
    buckets = {
        labels["le"]: value
        for name, labels, value in samples
        if name == f"{m.REQUEST_DURATION}_bucket"
    }
    assert buckets == {
        "0.005": 1, "0.01": 1, "0.025": 1, "0.05": 1, "0.1": 1, "0.25": 3, "0.5": 3,
        "1.0": 3, "2.5": 3, "5.0": 3, "10.0": 4, "+Inf": 5,
    }  # fmt: skip
    values = list(buckets.values())
    assert values == sorted(values)
    (total,) = [v for n, _, v in samples if n == f"{m.REQUEST_DURATION}_sum"]
    (seen,) = [v for n, _, v in samples if n == f"{m.REQUEST_DURATION}_count"]
    assert total == pytest.approx(37.403)
    assert seen == 5 == count(samples, route="/x")


def test_the_middleware_times_the_request_with_its_clock(client: TestClient) -> None:
    ticks = iter([10.0, 10.75])
    registry = m.RequestMetrics()
    inner = FastAPI()

    @inner.get("/t")
    def _t() -> dict[str, bool]:
        return {"ok": True}

    timed = TestClient(RequestMetricsMiddleware(inner, registry, clock=lambda: next(ticks)))
    assert timed.get("/t").json() == {"ok": True}
    _, samples = parse(registry.render())
    (total,) = [v for n, _, v in samples if n == f"{m.REQUEST_DURATION}_sum"]
    assert total == 0.75


def test_concurrent_observations_are_all_counted() -> None:
    registry = m.RequestMetrics()

    def work() -> None:
        for _ in range(500):
            registry.observe("GET", "/c", 200, 0.001)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _, samples = parse(registry.render())
    assert count(samples, route="/c") == 4000


def test_label_values_are_escaped() -> None:
    assert m.escape_label_value('a\\b"c\nd') == 'a\\\\b\\"c\\nd'
    registry = m.RequestMetrics()
    registry.observe("GET", '/odd/"quoted"\n', 200, 0.0)
    parse(registry.render())


def test_the_route_label_falls_back_to_unmatched() -> None:
    assert route_label({"type": "http"}) == m.UNMATCHED
    assert route_label({"type": "http", "route": object()}) == m.UNMATCHED


def test_status_codes_outside_http_are_bucketed() -> None:
    assert m.status_label(204) == "204"
    assert m.status_label(42) == "other"
    assert m.status_label(1000) == "other"


# --- who can reach it ---------------------------------------------------------------------------


@pytest.mark.parametrize("header", FORWARDING_HEADERS)
def test_a_proxied_request_gets_the_ordinary_404(client: TestClient, header: str) -> None:
    refused = client.get("/metrics", headers={header: "203.0.113.7"})
    unknown = client.get("/no-such-route")
    assert refused.status_code == 404 == unknown.status_code
    assert refused.json() == unknown.json()
    assert "http_requests_total" not in refused.text


def test_the_headers_nginx_adds_are_all_refused() -> None:
    conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    added = {name.lower() for name in re.findall(r"proxy_set_header\s+([A-Za-z-]+)", conf)} - {
        "host"
    }
    assert added
    assert added <= set(FORWARDING_HEADERS)


def test_it_is_not_in_the_openapi_document(app: FastAPI) -> None:
    assert "/metrics" not in app.openapi()["paths"]


def test_nginx_forwards_only_api_so_it_has_no_route_to_metrics() -> None:
    conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in conf.splitlines())
    assert "metrics" not in code
    locations = re.findall(r"^\s*location\s+(.+?)\s*\{", code, re.MULTILINE)
    proxied = [
        block.split("{", 1)[0].strip()
        for block in re.split(r"(?=^\s*location\s)", code, flags=re.MULTILINE)
        if "proxy_pass" in block.split("}", 1)[0]
    ]
    assert proxied == ["location /api/"]
    assert "/api/" in locations


def test_the_api_port_is_not_published() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    services = re.split(r"(?m)^  (?=[a-z]+:\n)", compose.split("\nservices:\n", 1)[1])
    (api,) = [block for block in services if block.startswith("api:\n")]
    api = api.split("\nvolumes:\n", 1)[0]
    assert not re.search(r"(?m)^    ports:", api)


# --- what it must never say ---------------------------------------------------------------------


def test_the_output_carries_no_secret_and_no_configuration(client: TestClient) -> None:
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "hunter2-xyz"})
    client.get(f"/api/v1/hotels/{HOTEL}/bookings", headers={"Authorization": "Bearer abc.def.ghi"})
    text = client.get("/metrics").text
    for value in (
        SECRET, DATABASE_URL, "metrics-db-password-9c1e", "metrics_user", "hunter2-xyz",
        "a@example.com", "abc.def.ghi", "Bearer",
    ):  # fmt: skip
        assert value not in text
    _, samples = parse(text)
    keys = {key for _, labels, _ in samples for key in labels}
    assert keys <= {"method", "route", "status", "le"}
